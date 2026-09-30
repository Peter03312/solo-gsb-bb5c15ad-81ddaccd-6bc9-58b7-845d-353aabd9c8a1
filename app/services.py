"""Business logic: races, checkpoints, participants, scans, publishing."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, timing
from .errors import bad_request, conflict, not_found
from .schemas import CheckpointItem, ParticipantCreate, ScanCreate


# ---------- helpers ----------


def get_race_or_404(db: Session, race_id: int) -> models.Race:
    race = db.get(models.Race, race_id)
    if race is None:
        raise not_found("race_not_found", f"race {race_id} does not exist")
    return race


def _scan_payload_hash(race_id: int, bib: str, code: str, event_time: datetime) -> str:
    canonical = "|".join(
        [str(race_id), bib, code, event_time.astimezone(timezone.utc).isoformat()]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _checkpoint_order(db: Session, race_id: int) -> list[str]:
    return list(
        db.scalars(
            select(models.Checkpoint.code)
            .where(models.Checkpoint.race_id == race_id)
            .order_by(models.Checkpoint.seq)
        )
    )


# ---------- races & checkpoints & participants ----------


def create_race(db: Session, name: str) -> models.Race:
    race = models.Race(name=name)
    db.add(race)
    db.flush()
    return race


def set_checkpoints(
    db: Session, race: models.Race, items: list[CheckpointItem]
) -> list[models.Checkpoint]:
    """Replace the race's ordered checkpoint list (first=start, last=finish)."""
    codes = [i.code for i in items]
    if len(set(codes)) != len(codes):
        raise bad_request("duplicate_checkpoint_code", "checkpoint codes must be unique")

    existing = {cp.code: cp for cp in race.checkpoints}

    for code, cp in existing.items():
        if code not in codes:
            scan_count = db.scalar(
                select(func.count(models.Scan.id)).where(
                    models.Scan.checkpoint_id == cp.id
                )
            )
            if scan_count:
                raise conflict(
                    "checkpoint_in_use",
                    f"checkpoint '{code}' has scan records and cannot be removed",
                )
            db.delete(cp)

    for seq, code in enumerate(codes, start=1):
        if code in existing:
            existing[code].seq = seq
        else:
            db.add(models.Checkpoint(race_id=race.id, code=code, seq=seq))

    db.flush()
    db.expire(race, ["checkpoints"])
    return list(race.checkpoints)


def add_participant(
    db: Session, race: models.Race, payload: ParticipantCreate
) -> tuple[models.Participant, bool]:
    """Returns (participant, created). Same bib + same name is idempotent."""
    existing = db.scalar(
        select(models.Participant).where(
            models.Participant.race_id == race.id,
            models.Participant.bib == payload.bib,
        )
    )
    if existing is not None:
        if existing.name == payload.name:
            return existing, False
        raise conflict(
            "participant_conflict",
            f"bib '{payload.bib}' is already registered with a different name",
        )
    participant = models.Participant(
        race_id=race.id, bib=payload.bib, name=payload.name
    )
    db.add(participant)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:  # concurrent registration of the same bib
        existing = db.scalar(
            select(models.Participant).where(
                models.Participant.race_id == race.id,
                models.Participant.bib == payload.bib,
            )
        )
        if existing is not None and existing.name == payload.name:
            return existing, False
        raise conflict(
            "participant_conflict",
            f"bib '{payload.bib}' is already registered with a different name",
        )
    return participant, True


# ---------- scans ----------


def record_scan(
    db: Session, race: models.Race, payload: ScanCreate
) -> tuple[models.Scan, bool]:
    """Insert a scan. Returns (scan, created).

    - same message_id + same content  -> idempotent replay (created=False)
    - same message_id + other content -> 409 conflict
    - unknown participant/checkpoint  -> 404
    """
    participant = db.scalar(
        select(models.Participant).where(
            models.Participant.race_id == race.id,
            models.Participant.bib == payload.participant_bib,
        )
    )
    if participant is None:
        raise not_found(
            "participant_not_found",
            f"participant '{payload.participant_bib}' is not registered in race {race.id}",
        )
    checkpoint = db.scalar(
        select(models.Checkpoint).where(
            models.Checkpoint.race_id == race.id,
            models.Checkpoint.code == payload.checkpoint_code,
        )
    )
    if checkpoint is None:
        raise not_found(
            "checkpoint_not_found",
            f"checkpoint '{payload.checkpoint_code}' is not defined in race {race.id}",
        )

    event_time = payload.event_time.astimezone(timezone.utc)
    digest = _scan_payload_hash(race.id, participant.bib, checkpoint.code, event_time)

    def _replay_or_conflict(existing: models.Scan) -> tuple[models.Scan, bool]:
        if existing.payload_hash == digest:
            return existing, False
        raise conflict(
            "scan_conflict",
            f"message_id '{payload.message_id}' was already uploaded with "
            "different content",
        )

    existing = db.scalar(
        select(models.Scan).where(models.Scan.message_id == payload.message_id)
    )
    if existing is not None:
        return _replay_or_conflict(existing)

    scan = models.Scan(
        message_id=payload.message_id,
        race_id=race.id,
        participant_id=participant.id,
        checkpoint_id=checkpoint.id,
        event_time=event_time,
        payload_hash=digest,
    )
    db.add(scan)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:  # concurrent upload of the same message_id
        existing = db.scalar(
            select(models.Scan).where(models.Scan.message_id == payload.message_id)
        )
        if existing is not None:
            return _replay_or_conflict(existing)
        raise
    return scan, True


def revoke_scan(db: Session, race: models.Race, message_id: str) -> tuple[models.Scan, bool]:
    """Revoke a scan. Returns (scan, already_revoked)."""
    scan = db.scalar(
        select(models.Scan).where(
            models.Scan.message_id == message_id,
            models.Scan.race_id == race.id,
        )
    )
    if scan is None:
        raise not_found(
            "scan_not_found",
            f"scan '{message_id}' does not exist in race {race.id}",
        )
    if scan.revoked_at is not None:
        return scan, True
    scan.revoked_at = datetime.now(timezone.utc)
    db.flush()
    return scan, False


# ---------- standings & publishing ----------


def compute_standings(db: Session, race: models.Race) -> tuple[list[str], list[timing.ParticipantResult]]:
    """Live standings from all currently active (non-revoked) scans."""
    order = _checkpoint_order(db, race.id)
    participants = [
        (p.bib, p.name)
        for p in db.scalars(
            select(models.Participant)
            .where(models.Participant.race_id == race.id)
            .order_by(models.Participant.bib)
        )
    ]
    rows = db.execute(
        select(
            models.Participant.bib,
            models.Scan.message_id,
            models.Checkpoint.code,
            models.Scan.event_time,
            models.Scan.received_at,
        )
        .join(models.Participant, models.Scan.participant_id == models.Participant.id)
        .join(models.Checkpoint, models.Scan.checkpoint_id == models.Checkpoint.id)
        .where(models.Scan.race_id == race.id, models.Scan.revoked_at.is_(None))
    ).all()

    scans_by_bib: dict[str, list[timing.ScanView]] = {}
    for bib, message_id, code, event_time, received_at in rows:
        scans_by_bib.setdefault(bib, []).append(
            timing.ScanView(
                message_id=message_id,
                checkpoint_code=code,
                event_time=event_time.astimezone(timezone.utc),
                received_at=received_at.astimezone(timezone.utc),
            )
        )
    return order, timing.compute_standings(order, participants, scans_by_bib)


def publish_results(db: Session, race: models.Race) -> models.ResultVersion:
    """Freeze the current standings as the next consecutive version.

    The race row is locked (SELECT ... FOR UPDATE) so concurrent publishes
    of the same race are serialized: version numbers are consecutive and
    every version is internally consistent. Scans or revocations committed
    after a publish only show up in the next version.
    """
    db.execute(
        select(models.Race.id).where(models.Race.id == race.id).with_for_update()
    )

    next_no = (
        db.scalar(
            select(func.coalesce(func.max(models.ResultVersion.version_no), 0)).where(
                models.ResultVersion.race_id == race.id
            )
        )
        + 1
    )

    order, results = compute_standings(db, race)

    version = models.ResultVersion(
        race_id=race.id, version_no=next_no, checkpoint_order=order
    )
    db.add(version)
    db.flush()

    for r in results:
        db.add(
            models.ResultEntry(
                version_id=version.id,
                participant_id=db.scalar(
                    select(models.Participant.id).where(
                        models.Participant.race_id == race.id,
                        models.Participant.bib == r.bib,
                    )
                ),
                status=r.status,
                rank=r.rank,
                elapsed_ms=r.elapsed_ms,
                reason=r.reason,
                basis=[
                    {
                        "message_id": s.message_id,
                        "checkpoint_code": s.checkpoint_code,
                        "event_time": s.event_time.isoformat(),
                    }
                    for s in r.basis
                ],
            )
        )
    db.flush()
    db.refresh(version)
    return version


def get_version_or_404(
    db: Session, race: models.Race, version_no: int
) -> models.ResultVersion:
    version = db.scalar(
        select(models.ResultVersion).where(
            models.ResultVersion.race_id == race.id,
            models.ResultVersion.version_no == version_no,
        )
    )
    if version is None:
        raise not_found(
            "version_not_found",
            f"race {race.id} has no published version {version_no}",
        )
    return version


def get_latest_version(db: Session, race: models.Race) -> models.ResultVersion:
    version = db.scalar(
        select(models.ResultVersion)
        .where(models.ResultVersion.race_id == race.id)
        .order_by(models.ResultVersion.version_no.desc())
        .limit(1)
    )
    if version is None:
        raise not_found(
            "no_published_results", f"race {race.id} has no published results yet"
        )
    return version
