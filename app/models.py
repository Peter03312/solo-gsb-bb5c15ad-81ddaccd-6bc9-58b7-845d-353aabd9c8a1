"""SQLAlchemy models (PostgreSQL)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Race(Base):
    __tablename__ = "races"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    checkpoints: Mapped[list["Checkpoint"]] = relationship(
        back_populates="race",
        cascade="all, delete-orphan",
        order_by="Checkpoint.seq",
    )
    participants: Mapped[list["Participant"]] = relationship(
        back_populates="race", cascade="all, delete-orphan"
    )


class Checkpoint(Base):
    """A checkpoint of a race. seq starts at 1; first = start, last = finish."""

    __tablename__ = "checkpoints"
    __table_args__ = (
        UniqueConstraint("race_id", "code", name="uq_checkpoint_race_code"),
        # deferrable so re-ordering existing checkpoints in one transaction
        # does not hit transient (race_id, seq) collisions at flush time
        UniqueConstraint(
            "race_id",
            "seq",
            name="uq_checkpoint_race_seq",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    race_id: Mapped[int] = mapped_column(
        ForeignKey("races.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(Integer)

    race: Mapped[Race] = relationship(back_populates="checkpoints")


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (UniqueConstraint("race_id", "bib", name="uq_participant_race_bib"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    race_id: Mapped[int] = mapped_column(
        ForeignKey("races.id", ondelete="CASCADE"), index=True
    )
    bib: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))

    race: Mapped[Race] = relationship(back_populates="participants")


class Scan(Base):
    """One raw scan record uploaded from the field.

    message_id is globally unique: re-uploading the same message_id with the
    same content is an idempotent no-op; the same message_id with different
    content is rejected as a conflict.
    """

    __tablename__ = "scans"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    race_id: Mapped[int] = mapped_column(
        ForeignKey("races.id", ondelete="CASCADE"), index=True
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), index=True
    )
    checkpoint_id: Mapped[int] = mapped_column(
        ForeignKey("checkpoints.id", ondelete="CASCADE"), index=True
    )
    # UTC event time stamped by the timing device
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # sha256 over the canonical content, used for idempotency/conflict checks
    payload_hash: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    participant: Mapped[Participant] = relationship()
    checkpoint: Mapped[Checkpoint] = relationship()


class ResultVersion(Base):
    """A frozen, immutable publication of the results of one race."""

    __tablename__ = "result_versions"
    __table_args__ = (
        UniqueConstraint("race_id", "version_no", name="uq_version_race_no"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    race_id: Mapped[int] = mapped_column(
        ForeignKey("races.id", ondelete="CASCADE"), index=True
    )
    version_no: Mapped[int] = mapped_column(Integer)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # checkpoint order used for the computation, frozen with the version
    checkpoint_order: Mapped[list] = mapped_column(JSONB)

    entries: Mapped[list["ResultEntry"]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )


class ResultEntry(Base):
    """One participant's frozen result inside a published version."""

    __tablename__ = "result_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("result_versions.id", ondelete="CASCADE"), index=True
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16))  # "finished" | "dnf"
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # the scan records this result is based on:
    # [{message_id, checkpoint_code, event_time}, ...] in checkpoint order
    basis: Mapped[list] = mapped_column(JSONB)

    version: Mapped[ResultVersion] = relationship(back_populates="entries")
    participant: Mapped[Participant] = relationship()
