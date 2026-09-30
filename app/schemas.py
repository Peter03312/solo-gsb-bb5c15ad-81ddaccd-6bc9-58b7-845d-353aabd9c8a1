"""HTTP 请求体模型。"""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class CheckpointIn(BaseModel):
    code: str = Field(min_length=1, max_length=64, examples=["CP1"])
    name: str = Field(min_length=1, max_length=128, examples=["1号补给站"])
    # 不传则追加到当前顺序末尾
    position: int | None = Field(default=None, ge=1)


class CheckpointReplace(BaseModel):
    """整表替换检查点顺序（全部位置必须互不相同，或全部省略）。"""

    checkpoints: list[CheckpointIn] = Field(min_length=1)


class ParticipantIn(BaseModel):
    id: str = Field(min_length=1, max_length=64, examples=["p-001"])
    bib: str = Field(min_length=1, max_length=32, examples=["A001"])
    name: str = Field(min_length=1, max_length=128, examples=["张三"])


class ParticipantBatch(BaseModel):
    participants: list[ParticipantIn] = Field(min_length=1)


class ScanIn(BaseModel):
    """一条扫描报文；message_id 也出现在路径上，二者须一致。"""

    participant_id: str = Field(min_length=1, max_length=64)
    checkpoint_code: str = Field(min_length=1, max_length=64)
    # 接受 "2026-09-29T01:00:00Z" / "+00:00" / 任意偏移，统一转 UTC
    event_time: datetime

    @field_validator("event_time")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("event_time 必须带时区（UTC 建议以 Z 结尾）")
        from datetime import timezone

        return value.astimezone(timezone.utc)


class BatchScanIn(ScanIn):
    """批量报文中的单条：报文 ID 随条目携带，重传时保持不变。"""

    message_id: uuid.UUID


class ScanBatch(BaseModel):
    scans: list[BatchScanIn] = Field(min_length=1)
