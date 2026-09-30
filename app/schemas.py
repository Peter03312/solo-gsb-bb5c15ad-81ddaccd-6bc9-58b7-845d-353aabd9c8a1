"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _require_tz_aware(cls, v: datetime) -> datetime:
    if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
        raise ValueError(
            "timestamp must include a timezone offset (UTC recommended, "
            "e.g. 2026-09-30T08:00:00Z)"
        )
    return v


# ---------- requests ----------


class RaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class CheckpointItem(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class CheckpointsSet(BaseModel):
    """Full ordered checkpoint list; first element is the start, last the finish."""

    checkpoints: list[CheckpointItem] = Field(min_length=2)


class ParticipantCreate(BaseModel):
    bib: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)


class ScanCreate(BaseModel):
    message_id: str = Field(min_length=1, max_length=128)
    participant_bib: str = Field(min_length=1, max_length=64)
    checkpoint_code: str = Field(min_length=1, max_length=64)
    event_time: datetime

    _tz = field_validator("event_time")(_require_tz_aware)


class ScanBatchCreate(BaseModel):
    scans: list[ScanCreate] = Field(min_length=1, max_length=5000)


# ---------- responses ----------


class CheckpointOut(BaseModel):
    seq: int
    code: str


class RaceOut(BaseModel):
    id: int
    name: str
    created_at: datetime
    checkpoints: list[CheckpointOut] = []
    participant_count: int = 0


class ParticipantOut(BaseModel):
    bib: str
    name: str


class ScanOut(BaseModel):
    message_id: str
    participant_bib: str
    checkpoint_code: str
    event_time: datetime
    received_at: datetime
    revoked: bool
    revoked_at: datetime | None


class ScanAccepted(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: str  # "created" | "duplicate"
    scan: ScanOut


class ScanBatchItemResult(BaseModel):
    message_id: str
    status: str  # "created" | "duplicate" | "error"
    error: dict | None = None


class ScanBatchResult(BaseModel):
    total: int
    created: int
    duplicates: int
    errors: int
    results: list[ScanBatchItemResult]


class RevokeOut(BaseModel):
    message_id: str
    revoked: bool
    already_revoked: bool
    revoked_at: datetime


class BasisItem(BaseModel):
    message_id: str
    checkpoint_code: str
    event_time: datetime


class EntryOut(BaseModel):
    rank: int | None
    bib: str
    name: str
    status: str  # "finished" | "dnf"
    elapsed_ms: int | None
    reason: str | None
    basis: list[BasisItem]


class StandingsOut(BaseModel):
    """Live (unpublished) standings computed from the currently active scans."""

    race_id: int
    generated_at: datetime
    checkpoint_order: list[str]
    entries: list[EntryOut]


class VersionOut(BaseModel):
    race_id: int
    version_no: int
    published_at: datetime
    checkpoint_order: list[str]
    entries: list[EntryOut]


class VersionSummary(BaseModel):
    version_no: int
    published_at: datetime
    finished_count: int
    dnf_count: int
