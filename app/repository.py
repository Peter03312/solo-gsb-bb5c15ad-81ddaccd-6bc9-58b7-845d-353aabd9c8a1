"""针对 checkpoints / participants / scans 的数据访问与写入校验。"""
import uuid
from datetime import datetime

import psycopg

from .errors import ApiError


# ---------------------------------------------------------------- 检查点

def list_checkpoints(cur, race_id: int) -> list[dict]:
    cur.execute(
        "SELECT code, name, position FROM checkpoints "
        "WHERE race_id = %s ORDER BY position",
        (race_id,),
    )
    return list(cur.fetchall())


def active_scan_exists(cur, race_id: int) -> bool:
    cur.execute(
        "SELECT 1 FROM scans WHERE race_id = %s AND revoked = FALSE LIMIT 1",
        (race_id,),
    )
    return cur.fetchone() is not None


def get_checkpoint(cur, race_id: int, code: str) -> dict | None:
    cur.execute(
        "SELECT code, name, position FROM checkpoints "
        "WHERE race_id = %s AND code = %s",
        (race_id, code),
    )
    return cur.fetchone()


def max_checkpoint_position(cur, race_id: int) -> int:
    cur.execute(
        "SELECT COALESCE(max(position), 0) AS m FROM checkpoints WHERE race_id = %s",
        (race_id,),
    )
    return int(cur.fetchone()["m"])


def insert_checkpoint(cur, race_id: int, code: str, name: str, position: int) -> None:
    try:
        cur.execute(
            "INSERT INTO checkpoints (race_id, code, name, position) "
            "VALUES (%s, %s, %s, %s)",
            (race_id, code, name, position),
        )
    except psycopg.IntegrityError:
        raise ApiError(409, "checkpoint_conflict",
                       f"检查点代码或顺序已存在: {code}")


# ---------------------------------------------------------------- 参赛者

def list_participants(cur, race_id: int) -> list[dict]:
    cur.execute(
        "SELECT id, bib, name FROM participants WHERE race_id = %s ORDER BY bib",
        (race_id,),
    )
    return list(cur.fetchall())


def get_participant(cur, race_id: int, participant_id: str) -> dict | None:
    cur.execute(
        "SELECT id, bib, name FROM participants "
        "WHERE race_id = %s AND id = %s",
        (race_id, participant_id),
    )
    return cur.fetchone()


def upsert_participant(cur, race_id: int, pid: str, bib: str, name: str) -> None:
    try:
        cur.execute(
            """
            INSERT INTO participants (race_id, id, bib, name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (race_id, id) DO UPDATE
              SET bib = EXCLUDED.bib, name = EXCLUDED.name
            """,
            (race_id, pid, bib, name),
        )
    except psycopg.IntegrityError:
        raise ApiError(409, "participant_conflict",
                       f"号码布 {bib} 已被其他参赛者占用")


# ---------------------------------------------------------------- 扫描

def require_participant(cur, race_id: int, participant_id: str) -> None:
    if get_participant(cur, race_id, participant_id) is None:
        raise ApiError(404, "unknown_participant",
                       f"未知参赛者: {participant_id}")


def require_checkpoint(cur, race_id: int, code: str) -> None:
    if get_checkpoint(cur, race_id, code) is None:
        raise ApiError(404, "unknown_checkpoint",
                       f"未知检查点: {code}")


def get_scan(cur, message_id: uuid.UUID) -> dict | None:
    cur.execute(
        "SELECT message_id, race_id, participant_id, checkpoint_code, "
        "event_time, revoked, revoked_at FROM scans WHERE message_id = %s",
        (message_id,),
    )
    return cur.fetchone()


def insert_scan(cur, race_id: int, message_id: uuid.UUID, participant_id: str,
                checkpoint_code: str, event_time: datetime) -> None:
    # FK 在此兜底未知参赛者/检查点（正常已先显式校验给出明确错误）
    cur.execute(
        """
        INSERT INTO scans
            (message_id, race_id, participant_id, checkpoint_code, event_time)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (message_id, race_id, participant_id, checkpoint_code, event_time),
    )


def revoke_scan(cur, message_id: uuid.UUID) -> dict:
    """把扫描标记为撤销。返回扫描最新状态（含 revoked 标志）。

    已撤销是幂等成功：重复撤销同一目标不报错。
    """
    cur.execute(
        """
        UPDATE scans
           SET revoked = TRUE, revoked_at = now()
         WHERE message_id = %s
        RETURNING message_id, race_id, participant_id, checkpoint_code,
                  event_time, revoked, revoked_at
        """,
        (message_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ApiError(404, "scan_not_found",
                       f"撤销目标不存在: {message_id}")
    return row


def list_scans(cur, race_id: int, revoked: bool | None,
               participant_id: str | None) -> list[dict]:
    sql = (
        "SELECT message_id, participant_id, checkpoint_code, event_time, "
        "received_at, revoked, revoked_at FROM scans WHERE race_id = %s"
    )
    params: list = [race_id]
    if revoked is not None:
        sql += " AND revoked = %s"
        params.append(revoked)
    if participant_id is not None:
        sql += " AND participant_id = %s"
        params.append(participant_id)
    sql += " ORDER BY event_time, message_id"
    cur.execute(sql, params)
    return list(cur.fetchall())
