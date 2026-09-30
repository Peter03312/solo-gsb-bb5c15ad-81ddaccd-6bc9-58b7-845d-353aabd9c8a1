"""FastAPI 入口：赛事配置、扫描录入、成绩核算、版本发布全部路由。"""
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Query
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import db, repository
from .config import HOST, PORT, RACE_ID
from .errors import ApiError
from .schemas import (
    CheckpointIn,
    CheckpointReplace,
    ParticipantBatch,
    ParticipantIn,
    ScanBatch,
    ScanIn,
)

app = FastAPI(
    title="线下越野赛计时核算服务",
    description=(
        "录入检查点顺序、参赛者与离线扫描报文；乱序可传、重传幂等、"
        "冲突拒绝；支持撤销重算与成绩版本固化发布。"
    ),
    version="1.0.0",
)


# ------------------------------------------------------------- 生命周期

@app.on_event("startup")
def _startup() -> None:
    db.open_pool()
    db.init_db()


@app.on_event("shutdown")
def _shutdown() -> None:
    db.close_pool()


# ------------------------------------------------------------- 错误处理

@app.exception_handler(ApiError)
async def _api_error_handler(_request, exc: ApiError):
    return JSONResponse(status_code=exc.status_code, content=exc.to_body())


@app.exception_handler(RequestValidationError)
async def _validation_handler(_request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder(
            {
                "error": {
                    "code": "invalid_request",
                    "message": "请求参数不合法",
                    "details": exc.errors(),
                }
            }
        ),
    )


def _scan_dict(row: dict) -> dict:
    return {
        "message_id": str(row["message_id"]),
        "participant_id": row["participant_id"],
        "checkpoint_code": row["checkpoint_code"],
        "event_time": row["event_time"].astimezone(timezone.utc).isoformat(),
        "received_at": row["received_at"].astimezone(timezone.utc).isoformat(),
        "revoked": row["revoked"],
        "revoked_at": row["revoked_at"].astimezone(timezone.utc).isoformat()
        if row["revoked_at"]
        else None,
    }


# ------------------------------------------------------------- 基础接口

@app.get("/", tags=["meta"])
def root():
    return {
        "service": "trail-race-timing",
        "race_id": RACE_ID,
        "docs": "/docs",
        "openapi": "/openapi.json",
    }


@app.get("/health", tags=["meta"])
def health():
    with db.connection() as cx, cx.cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        ok = cur.fetchone()["ok"] == 1
    return {"status": "ok" if ok else "degraded", "database": ok}


# ------------------------------------------------------------- 检查点

@app.get("/checkpoints", tags=["checkpoints"])
def get_checkpoints():
    with db.connection() as cx, cx.cursor() as cur:
        return {"checkpoints": repository.list_checkpoints(cur, RACE_ID)}


@app.post("/checkpoints", status_code=201, tags=["checkpoints"])
def add_checkpoint(body: CheckpointIn):
    with db.connection() as cx, cx.cursor() as cur:
        try:
            position = body.position
            if position is None:
                position = repository.max_checkpoint_position(cur, RACE_ID) + 1
            repository.insert_checkpoint(cur, RACE_ID, body.code, body.name, position)
            cx.commit()
        except Exception:
            cx.rollback()
            raise
    return {"code": body.code, "name": body.name, "position": position}


@app.put("/checkpoints", tags=["checkpoints"])
def replace_checkpoints(body: CheckpointReplace):
    """整体替换检查点顺序。已有生效扫描时拒绝（避免核算依据被悄悄改写）。"""
    positions = [c.position for c in body.checkpoints if c.position is not None]
    codes = [c.code for c in body.checkpoints]
    if len(set(codes)) != len(codes):
        raise ApiError(422, "duplicate_checkpoint_code", "检查点代码重复")
    if len(positions) != len(body.checkpoints):
        # 全部省略：按数组顺序从 1 自动编号
        auto = all(c.position is None for c in body.checkpoints)
        if not auto:
            raise ApiError(422, "positions_required",
                           "position 须全部提供或全部省略")
        normalized = [
            (c.code, c.name, idx)
            for idx, c in enumerate(body.checkpoints, start=1)
        ]
    else:
        if len(set(positions)) != len(positions):
            raise ApiError(422, "duplicate_position", "检查点顺序号重复")
        if sorted(positions) != list(range(1, len(positions) + 1)):
            raise ApiError(422, "positions_not_contiguous",
                           "顺序号必须是从 1 开始的连续整数")
        normalized = [(c.code, c.name, c.position) for c in body.checkpoints]

    with db.connection() as cx, cx.cursor() as cur:
        try:
            if repository.active_scan_exists(cur, RACE_ID):
                raise ApiError(
                    409, "scans_exist",
                    "已有生效扫描记录，不能整体替换检查点；请先撤销相关扫描",
                )
            cur.execute("DELETE FROM checkpoints WHERE race_id = %s", (RACE_ID,))
            for code, name, position in normalized:
                repository.insert_checkpoint(cur, RACE_ID, code, name, position)
            cx.commit()
        except Exception:
            cx.rollback()
            raise
    return {
        "checkpoints": [
            {"code": code, "name": name, "position": position}
            for code, name, position in normalized
        ]
    }


# ------------------------------------------------------------- 参赛者

@app.get("/participants", tags=["participants"])
def get_participants():
    with db.connection() as cx, cx.cursor() as cur:
        return {"participants": repository.list_participants(cur, RACE_ID)}


@app.post("/participants", status_code=201, tags=["participants"])
def add_participant(body: ParticipantIn):
    with db.connection() as cx, cx.cursor() as cur:
        try:
            if repository.get_participant(cur, RACE_ID, body.id) is not None:
                raise ApiError(409, "participant_exists",
                               f"参赛者已存在: {body.id}")
            repository.upsert_participant(cur, RACE_ID, body.id, body.bib, body.name)
            cx.commit()
        except Exception:
            cx.rollback()
            raise
    return {"id": body.id, "bib": body.bib, "name": body.name}


@app.put("/participants", tags=["participants"])
def upsert_participants(body: ParticipantBatch):
    """批量录入/更新参赛者（按 id upsert，离线报名场景）。"""
    ids = [p.id for p in body.participants]
    if len(set(ids)) != len(ids):
        raise ApiError(422, "duplicate_participant_id", "参赛者 id 在请求中重复")
    with db.connection() as cx, cx.cursor() as cur:
        try:
            for p in body.participants:
                repository.upsert_participant(cur, RACE_ID, p.id, p.bib, p.name)
            cx.commit()
        except Exception:
            cx.rollback()
            raise
    return {"upserted": len(body.participants)}


# ------------------------------------------------------------- 扫描录入

def _put_scan(cur, message_id: uuid.UUID, body: ScanIn) -> str:
    """在当前事务内写入单条报文，返回 'created' 或 'idempotent'。"""
    repository.require_participant(cur, RACE_ID, body.participant_id)
    repository.require_checkpoint(cur, RACE_ID, body.checkpoint_code)

    existing = repository.get_scan(cur, message_id)
    if existing is not None:
        if existing["revoked"]:
            raise ApiError(
                409, "message_revoked",
                f"报文 {message_id} 已被撤销，不能以相同 ID 重传；"
                "请使用新的报文 ID",
            )
        same = (
            existing["participant_id"] == body.participant_id
            and existing["checkpoint_code"] == body.checkpoint_code
            and existing["event_time"] == body.event_time
        )
        if not same:
            raise ApiError(
                409, "message_conflict",
                f"报文 ID {message_id} 已存在但内容不同；"
                f"已存 participant={existing['participant_id']}, "
                f"checkpoint={existing['checkpoint_code']}, "
                f"event_time={existing['event_time'].isoformat()}",
            )
        return "idempotent"

    repository.insert_scan(
        cur, RACE_ID, message_id,
        body.participant_id, body.checkpoint_code, body.event_time,
    )
    return "created"


@app.put("/scans/{message_id}", tags=["scans"])
def upload_scan(message_id: uuid.UUID, body: ScanIn):
    """录入（或幂等重传）一条扫描报文。

    - 201：首次写入；200：同 ID 同内容重传（幂等）
    - 409：同 ID 内容冲突，或该 ID 已被撤销
    - 404：未知参赛者 / 未知检查点
    """
    with db.connection() as cx, cx.cursor() as cur:
        try:
            outcome = _put_scan(cur, message_id, body)
            cx.commit()
        except Exception:
            cx.rollback()
            raise
    return JSONResponse(
        status_code=201 if outcome == "created" else 200,
        content={
            "message_id": str(message_id),
            "status": outcome,
            "participant_id": body.participant_id,
            "checkpoint_code": body.checkpoint_code,
            "event_time": body.event_time.isoformat(),
        },
    )


@app.post("/scans/batch", tags=["scans"])
def upload_scan_batch(body: ScanBatch):
    """批量上传（离线设备补传）。报文 ID 由采集端随条目携带。

    逐条独立事务，互不影响；同 ID 同内容仍计为幂等，冲突仍拒绝。
    """
    results = []
    for raw in body.scans:
        message_id = raw.message_id
        with db.connection() as cx, cx.cursor() as cur:
            try:
                outcome = _put_scan(cur, message_id, raw)
                cx.commit()
                results.append(
                    {"message_id": str(message_id), "status": outcome}
                )
            except ApiError as exc:
                cx.rollback()
                results.append(
                    {
                        "message_id": str(message_id),
                        "status": "rejected",
                        "error": {"code": exc.code, "message": exc.message},
                    }
                )
            except Exception as exc:  # 防御：单条异常不拖垮整批
                cx.rollback()
                results.append(
                    {
                        "message_id": str(message_id),
                        "status": "rejected",
                        "error": {"code": "internal_error", "message": str(exc)},
                    }
                )
    created = sum(1 for r in results if r["status"] == "created")
    idempotent = sum(1 for r in results if r["status"] == "idempotent")
    rejected = sum(1 for r in results if r["status"] == "rejected")
    return {
        "created": created,
        "idempotent": idempotent,
        "rejected": rejected,
        "results": results,
    }


@app.post("/scans/{message_id}/revoke", tags=["scans"])
def revoke_scan(message_id: uuid.UUID):
    """撤销指定扫描并触发下次查询/发布时重算。重复撤销幂等。"""
    with db.connection() as cx, cx.cursor() as cur:
        try:
            row = repository.revoke_scan(cur, message_id)
            cx.commit()
        except Exception:
            cx.rollback()
            raise
    return {
        "message_id": str(row["message_id"]),
        "revoked": row["revoked"],
        "revoked_at": row["revoked_at"].astimezone(timezone.utc).isoformat(),
    }


@app.get("/scans", tags=["scans"])
def get_scans(
    revoked: bool | None = Query(default=None, description="按撤销状态过滤"),
    participant_id: str | None = Query(default=None, description="按参赛者过滤"),
):
    with db.connection() as cx, cx.cursor() as cur:
        rows = repository.list_scans(cur, RACE_ID, revoked, participant_id)
    return {"scans": [_scan_dict(r) for r in rows]}


# ------------------------------------------------------------- 成绩与版本

@app.get("/results", tags=["results"])
def current_results():
    """实时（草稿）成绩：基于当前全部生效扫描即时核算，尚未固化。"""
    with db.connection() as cx, cx.cursor() as cur:
        result = db.load_draft_snapshot(cur)
    return db.snapshot_to_payload(result, version=None, frozen=False)


@app.post("/results/publish", tags=["results"])
def publish_results():
    """固化当前成绩为新版本。

    并发发布通过赛事行级锁串行化：版本号连续（1,2,3…无空洞），
    每个版本在自己的事务快照上保持一致；迟到记录与撤销只影响下一版。
    """
    published_at = datetime.now(timezone.utc)
    with db.connection() as cx, cx.cursor() as cur:
        try:
            # 同赛事的发布者在此排队；锁在整个事务（含写入）期间持有
            cur.execute("SELECT id FROM races WHERE id = %s FOR UPDATE", (RACE_ID,))
            if cur.fetchone() is None:
                raise ApiError(500, "race_missing", f"赛事不存在: {RACE_ID}")

            cur.execute(
                "SELECT COALESCE(max(version), 0) + 1 AS next_version "
                "FROM result_versions WHERE race_id = %s",
                (RACE_ID,),
            )
            version = int(cur.fetchone()["next_version"])

            result = db.load_draft_snapshot(cur)
            payload = db.snapshot_to_payload(result, version=version, frozen=True)
            db.save_version(cur, version, payload, published_at)
            cx.commit()
        except Exception:
            cx.rollback()
            raise
    payload["published_at"] = published_at.isoformat()
    return payload


@app.get("/results/versions", tags=["results"])
def get_versions():
    with db.connection() as cx, cx.cursor() as cur:
        return {"versions": db.list_versions(cur)}


@app.get("/results/versions/{version}", tags=["results"])
def get_version(version: int):
    if version < 1:
        raise ApiError(404, "version_not_found", f"版本不存在: {version}")
    with db.connection() as cx, cx.cursor() as cur:
        payload = db.load_version_payload(cur, version)
    if payload is None:
        raise ApiError(404, "version_not_found", f"版本不存在: {version}")
    return payload


def main() -> None:  # 供 python -m app.main 直接运行
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
