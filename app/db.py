"""PostgreSQL 访问层：连接池、启动建表、成绩快照读写。"""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from . import scoring
from .config import DATABASE_URL, RACE_ID

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

_pool: ConnectionPool | None = None


def open_pool() -> None:
    global _pool
    _pool = ConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
        # 借用前轻量校验，自动丢弃数据库重启/网络抖动产生的失效连接
        check=ConnectionPool.check_connection,
        kwargs={"row_factory": dict_row, "autocommit": False},
    )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection():
    """从池中借出连接并在退出时归还（含异常归还）。

    事务由调用方显式 commit；未提交即归还的（如只读查询产生的
    隐式事务）在此统一回滚，避免连接池告警并保证连接干净复用。
    """
    assert _pool is not None, "连接池尚未初始化"
    cx = _pool.getconn()
    try:
        yield cx
    finally:
        if cx.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            cx.rollback()
        _pool.putconn(cx)


def init_db() -> None:
    """启动时幂等执行建表脚本并插入默认赛事。"""
    ddl = SCHEMA_FILE.read_text(encoding="utf-8")
    # RACE_ID 为环境变量解析出的整数，替换占位符安全；
    # 多语句脚本不能走带参数的扩展查询协议。
    ddl = ddl.replace("%(race_id)s", str(int(RACE_ID)))
    with connection() as cx, cx.cursor() as cur:
        cur.execute(ddl)
        cx.commit()


# ---------------------------------------------------------------- 序列化

def _pass_to_dict(p: scoring.Pass) -> dict:
    return {
        "checkpoint_code": p.checkpoint_code,
        "position": p.position,
        "event_time": p.event_time.astimezone(timezone.utc).isoformat(),
        "message_id": p.message_id,
    }


def _participant_to_dict(p: scoring.Participant) -> dict:
    return {"id": p.id, "bib": p.bib, "name": p.name}


def snapshot_to_payload(result: dict, *, version: int | None, frozen: bool) -> dict:
    """把 scoring.compute 的结果转为可 JSON 序列化 / 可入库的响应结构。"""
    checkpoints = [
        {"code": c.code, "name": c.name, "position": c.position}
        for c in result["checkpoints"]
    ]
    finished = []
    for row in result["finished"]:
        finished.append(
            {
                "rank": row["rank"],
                "participant": _participant_to_dict(row["participant"]),
                "elapsed_seconds": float(row["elapsed_seconds"]),
                "start_time": row["start_time"].astimezone(timezone.utc).isoformat(),
                "finish_time": row["finish_time"].astimezone(timezone.utc).isoformat(),
                "passes": [_pass_to_dict(p) for p in row["passes"]],
            }
        )
    unfinished = []
    for row in result["unfinished"]:
        unfinished.append(
            {
                "participant": _participant_to_dict(row["participant"]),
                "reason": row["reason"],
                "passed_count": row["passed_count"],
                "missing_checkpoint": row["missing_checkpoint"],
                "passes": [_pass_to_dict(p) for p in row["passes"]],
            }
        )
    return {
        "version": version,
        "frozen": frozen,
        "race_id": RACE_ID,
        "checkpoints": checkpoints,
        "finished": finished,
        "unfinished": unfinished,
    }


# ---------------------------------------------------------------- 读取快照

def load_draft_snapshot(cur: psycopg.Cursor) -> dict:
    """读取当前生效（未撤销）扫描并核算。调用方负责事务边界。"""
    cur.execute(
        "SELECT code, name, position FROM checkpoints "
        "WHERE race_id = %s ORDER BY position",
        (RACE_ID,),
    )
    cps = [
        scoring.Checkpoint(r["code"], r["name"], r["position"])
        for r in cur.fetchall()
    ]
    cur.execute(
        "SELECT id, bib, name FROM participants WHERE race_id = %s ORDER BY bib",
        (RACE_ID,),
    )
    participants = [
        scoring.Participant(r["id"], r["bib"], r["name"]) for r in cur.fetchall()
    ]
    cur.execute(
        """
        SELECT s.message_id, s.participant_id, s.checkpoint_code, s.event_time
        FROM scans s
        WHERE s.race_id = %s AND s.revoked = FALSE
        """,
        (RACE_ID,),
    )
    scans = [
        scoring.Scan(
            str(r["message_id"]),
            r["participant_id"],
            r["checkpoint_code"],
            r["event_time"].astimezone(timezone.utc),
        )
        for r in cur.fetchall()
    ]
    return scoring.compute(cps, participants, scans)


# ---------------------------------------------------------------- 版本固化

def save_version(cur: psycopg.Cursor, version: int, payload: dict,
                 published_at: datetime) -> None:
    cur.execute(
        """
        INSERT INTO result_versions
            (race_id, version, published_at, checkpoints_json, result_json)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            RACE_ID,
            version,
            published_at,
            Jsonb(payload["checkpoints"]),
            Jsonb({"finished": payload["finished"], "unfinished": payload["unfinished"]}),
        ),
    )


def load_version_payload(cur: psycopg.Cursor, version: int) -> dict | None:
    cur.execute(
        "SELECT version, published_at, checkpoints_json, result_json "
        "FROM result_versions WHERE race_id = %s AND version = %s",
        (RACE_ID, version),
    )
    row = cur.fetchone()
    if row is None:
        return None
    result_json = row["result_json"]
    return {
        "version": row["version"],
        "frozen": True,
        "race_id": RACE_ID,
        "published_at": row["published_at"].astimezone(timezone.utc).isoformat(),
        "checkpoints": row["checkpoints_json"],
        "finished": result_json["finished"],
        "unfinished": result_json["unfinished"],
    }


def list_versions(cur: psycopg.Cursor) -> list[dict]:
    cur.execute(
        """
        SELECT v.version, v.published_at,
               (SELECT count(*) FROM result_rows r
                 WHERE r.version_id = v.id AND r.rank IS NOT NULL) AS finished_count,
               (SELECT count(*) FROM result_rows r
                 WHERE r.version_id = v.id AND r.rank IS NULL) AS unfinished_count
        FROM result_versions v
        WHERE v.race_id = %s
        ORDER BY v.version
        """,
        (RACE_ID,),
    )
    return [
        {
            "version": r["version"],
            "published_at": r["published_at"].astimezone(timezone.utc).isoformat(),
            "finished_count": r["finished_count"],
            "unfinished_count": r["unfinished_count"],
        }
        for r in cur.fetchall()
    ]
