"""HTTP API routes."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models, schemas, services
from .db import get_db

router = APIRouter()


# ---------- serializers ----------


def _scan_out(scan: models.Scan) -> schemas.ScanOut:
    return schemas.ScanOut(
        message_id=scan.message_id,
        participant_bib=scan.participant.bib,
        checkpoint_code=scan.checkpoint.code,
        event_time=scan.event_time,
        received_at=scan.received_at,
        revoked=scan.revoked_at is not None,
        revoked_at=scan.revoked_at,
    )


def _race_out(db: Session, race: models.Race) -> schemas.RaceOut:
    checkpoints = db.scalars(
        select(models.Checkpoint)
        .where(models.Checkpoint.race_id == race.id)
        .order_by(models.Checkpoint.seq)
    ).all()
    participant_count = db.scalar(
        select(func.count(models.Participant.id)).where(
            models.Participant.race_id == race.id
        )
    )
    return schemas.RaceOut(
        id=race.id,
        name=race.name,
        created_at=race.created_at,
        checkpoints=[schemas.CheckpointOut(seq=c.seq, code=c.code) for c in checkpoints],
        participant_count=participant_count or 0,
    )


def _entries_out(results) -> list[schemas.EntryOut]:
    return [
        schemas.EntryOut(
            rank=r.rank,
            bib=r.bib,
            name=r.name,
            status=r.status,
            elapsed_ms=r.elapsed_ms,
            reason=r.reason,
            basis=[
                schemas.BasisItem(
                    message_id=s.message_id,
                    checkpoint_code=s.checkpoint_code,
                    event_time=s.event_time,
                )
                for s in r.basis
            ],
        )
        for r in results
    ]


def _version_out(db: Session, version: models.ResultVersion) -> schemas.VersionOut:
    entries = db.scalars(
        select(models.ResultEntry).where(
            models.ResultEntry.version_id == version.id
        )
    ).all()
    # finished first by rank, then dnf by bib
    finished = sorted(
        (e for e in entries if e.status == "finished"),
        key=lambda e: (e.rank, e.participant.bib),
    )
    dnf = sorted(
        (e for e in entries if e.status == "dnf"), key=lambda e: e.participant.bib
    )
    ordered = finished + dnf
    return schemas.VersionOut(
        race_id=version.race_id,
        version_no=version.version_no,
        published_at=version.published_at,
        checkpoint_order=list(version.checkpoint_order),
        entries=[
            schemas.EntryOut(
                rank=e.rank,
                bib=e.participant.bib,
                name=e.participant.name,
                status=e.status,
                elapsed_ms=e.elapsed_ms,
                reason=e.reason,
                basis=[schemas.BasisItem(**b) for b in e.basis],
            )
            for e in ordered
        ],
    )


# ---------- races ----------


@router.post(
    "/races", response_model=schemas.RaceOut, status_code=status.HTTP_201_CREATED
)
def create_race(payload: schemas.RaceCreate, db: Session = Depends(get_db)):
    race = services.create_race(db, payload.name)
    db.commit()
    return _race_out(db, race)


@router.get("/races/{race_id}", response_model=schemas.RaceOut)
def get_race(race_id: int, db: Session = Depends(get_db)):
    race = services.get_race_or_404(db, race_id)
    return _race_out(db, race)


@router.put("/races/{race_id}/checkpoints", response_model=schemas.RaceOut)
def set_checkpoints(
    race_id: int, payload: schemas.CheckpointsSet, db: Session = Depends(get_db)
):
    """Define (or replace) the ordered checkpoint list. First = start, last = finish."""
    race = services.get_race_or_404(db, race_id)
    services.set_checkpoints(db, race, payload.checkpoints)
    db.commit()
    return _race_out(db, race)


# ---------- participants ----------


@router.post("/races/{race_id}/participants", response_model=schemas.ParticipantOut)
def add_participant(
    race_id: int,
    payload: schemas.ParticipantCreate,
    response: Response,
    db: Session = Depends(get_db),
):
    race = services.get_race_or_404(db, race_id)
    participant, created = services.add_participant(db, race, payload)
    db.commit()
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return schemas.ParticipantOut(bib=participant.bib, name=participant.name)


@router.get(
    "/races/{race_id}/participants", response_model=list[schemas.ParticipantOut]
)
def list_participants(race_id: int, db: Session = Depends(get_db)):
    services.get_race_or_404(db, race_id)
    participants = db.scalars(
        select(models.Participant)
        .where(models.Participant.race_id == race_id)
        .order_by(models.Participant.bib)
    ).all()
    return [schemas.ParticipantOut(bib=p.bib, name=p.name) for p in participants]


# ---------- scans ----------


@router.post("/races/{race_id}/scans", response_model=schemas.ScanAccepted)
def upload_scan(
    race_id: int,
    payload: schemas.ScanCreate,
    response: Response,
    db: Session = Depends(get_db),
):
    """Upload one scan record.

    Records may arrive out of order. Re-uploading the same message_id with
    identical content is an idempotent no-op (200); the same message_id with
    different content is rejected (409).
    """
    race = services.get_race_or_404(db, race_id)
    scan, created = services.record_scan(db, race, payload)
    db.commit()
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return schemas.ScanAccepted(
        status="created" if created else "duplicate", scan=_scan_out(scan)
    )


@router.post("/races/{race_id}/scans/batch", response_model=schemas.ScanBatchResult)
def upload_scans_batch(
    race_id: int, payload: schemas.ScanBatchCreate, db: Session = Depends(get_db)
):
    """Bulk upload for offline devices. Each record is processed independently
    with the same idempotency/conflict rules as the single-record endpoint."""
    race = services.get_race_or_404(db, race_id)
    results: list[schemas.ScanBatchItemResult] = []
    created = duplicates = errors = 0
    for item in payload.scans:
        try:
            _, was_created = services.record_scan(db, race, item)
            db.commit()
            if was_created:
                created += 1
                results.append(
                    schemas.ScanBatchItemResult(
                        message_id=item.message_id, status="created"
                    )
                )
            else:
                duplicates += 1
                results.append(
                    schemas.ScanBatchItemResult(
                        message_id=item.message_id, status="duplicate"
                    )
                )
        except Exception as exc:  # per-record isolation
            db.rollback()
            errors += 1
            if hasattr(exc, "code") and hasattr(exc, "message"):
                err = {"code": exc.code, "message": exc.message}
            else:  # pragma: no cover - defensive
                err = {"code": "internal_error", "message": str(exc)}
            results.append(
                schemas.ScanBatchItemResult(
                    message_id=item.message_id, status="error", error=err
                )
            )
    return schemas.ScanBatchResult(
        total=len(payload.scans),
        created=created,
        duplicates=duplicates,
        errors=errors,
        results=results,
    )


@router.get("/races/{race_id}/scans", response_model=list[schemas.ScanOut])
def list_scans(race_id: int, db: Session = Depends(get_db)):
    services.get_race_or_404(db, race_id)
    scans = db.scalars(
        select(models.Scan)
        .where(models.Scan.race_id == race_id)
        .order_by(models.Scan.event_time, models.Scan.message_id)
    ).all()
    return [_scan_out(s) for s in scans]


@router.post(
    "/races/{race_id}/scans/{message_id}/revoke", response_model=schemas.RevokeOut
)
def revoke_scan(race_id: int, message_id: str, db: Session = Depends(get_db)):
    """Revoke a scan record. Live standings change immediately; already
    published versions are unaffected."""
    race = services.get_race_or_404(db, race_id)
    scan, already = services.revoke_scan(db, race, message_id)
    db.commit()
    return schemas.RevokeOut(
        message_id=scan.message_id,
        revoked=True,
        already_revoked=already,
        revoked_at=scan.revoked_at,
    )


# ---------- standings & published results ----------


@router.get("/races/{race_id}/standings", response_model=schemas.StandingsOut)
def live_standings(race_id: int, db: Session = Depends(get_db)):
    """Current (unpublished) standings computed from active scans."""
    race = services.get_race_or_404(db, race_id)
    order, results = services.compute_standings(db, race)
    return schemas.StandingsOut(
        race_id=race.id,
        generated_at=datetime.now(timezone.utc),
        checkpoint_order=order,
        entries=_entries_out(results),
    )


@router.post(
    "/races/{race_id}/results/publish",
    response_model=schemas.VersionOut,
    status_code=status.HTTP_201_CREATED,
)
def publish_results(race_id: int, db: Session = Depends(get_db)):
    """Freeze the current standings as the next consecutive result version."""
    race = services.get_race_or_404(db, race_id)
    version = services.publish_results(db, race)
    db.commit()
    return _version_out(db, version)


@router.get("/races/{race_id}/results", response_model=schemas.VersionOut)
def latest_results(race_id: int, db: Session = Depends(get_db)):
    """The most recently published result version."""
    race = services.get_race_or_404(db, race_id)
    return _version_out(db, services.get_latest_version(db, race))


@router.get(
    "/races/{race_id}/results/versions", response_model=list[schemas.VersionSummary]
)
def list_versions(race_id: int, db: Session = Depends(get_db)):
    services.get_race_or_404(db, race_id)
    versions = db.scalars(
        select(models.ResultVersion)
        .where(models.ResultVersion.race_id == race_id)
        .order_by(models.ResultVersion.version_no)
    ).all()
    out = []
    for v in versions:
        finished = sum(1 for e in v.entries if e.status == "finished")
        out.append(
            schemas.VersionSummary(
                version_no=v.version_no,
                published_at=v.published_at,
                finished_count=finished,
                dnf_count=len(v.entries) - finished,
            )
        )
    return out


@router.get(
    "/races/{race_id}/results/versions/{version_no}", response_model=schemas.VersionOut
)
def get_version(race_id: int, version_no: int, db: Session = Depends(get_db)):
    """Any previously published version stays queryable forever."""
    race = services.get_race_or_404(db, race_id)
    return _version_out(db, services.get_version_or_404(db, race, version_no))
