"""Core timing computation, pure and DB-independent.

Rules implemented here:
- A participant's *valid pass* at each checkpoint is selected in checkpoint
  order: among the scans at that checkpoint whose event time is strictly
  later than the previously accepted pass, the earliest one wins
  ("first valid pass with checkpoint order consistent and times strictly
  increasing").
- A participant passing every checkpoint is ranked by start->finish elapsed
  time; equal elapsed times share the same rank (competition ranking).
- A participant who cannot complete the ordered chain is DNF and gets a
  human-readable reason.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class ScanView:
    message_id: str
    checkpoint_code: str
    event_time: datetime
    received_at: datetime


@dataclass
class ParticipantResult:
    bib: str
    name: str
    status: str  # "finished" | "dnf"
    rank: int | None = None
    elapsed_ms: int | None = None
    reason: str | None = None
    basis: list[ScanView] = field(default_factory=list)


def _evaluate_participant(
    bib: str,
    name: str,
    checkpoint_order: list[str],
    scans: list[ScanView],
) -> ParticipantResult:
    # deterministic processing order for the participant's scans
    ordered = sorted(scans, key=lambda s: (s.event_time, s.received_at, s.message_id))

    basis: list[ScanView] = []
    last_time: datetime | None = None
    prev_code: str | None = None

    for code in checkpoint_order:
        candidates = [
            s
            for s in ordered
            if s.checkpoint_code == code
            and (last_time is None or s.event_time > last_time)
        ]
        if not candidates:
            if not ordered:
                reason = "no scan records"
            elif not any(s.checkpoint_code == code for s in ordered):
                reason = f"missing checkpoint '{code}'"
            else:
                reason = (
                    f"checkpoint '{code}' has no scan strictly after "
                    f"previous checkpoint '{prev_code}'"
                )
            return ParticipantResult(
                bib=bib, name=name, status="dnf", reason=reason, basis=basis
            )
        pick = candidates[0]  # earliest valid pass at this checkpoint
        basis.append(pick)
        last_time = pick.event_time
        prev_code = code

    elapsed = basis[-1].event_time - basis[0].event_time
    return ParticipantResult(
        bib=bib,
        name=name,
        status="finished",
        elapsed_ms=int(elapsed.total_seconds() * 1000),
        basis=basis,
    )


def compute_standings(
    checkpoint_order: list[str],
    participants: list[tuple[str, str]],  # (bib, name)
    scans_by_bib: dict[str, list[ScanView]],
) -> list[ParticipantResult]:
    """Ranked finishers first (ties share a rank), then DNF sorted by bib."""
    results = [
        _evaluate_participant(bib, name, checkpoint_order, scans_by_bib.get(bib, []))
        for bib, name in participants
    ]

    finished = [r for r in results if r.status == "finished"]
    dnf = [r for r in results if r.status == "dnf"]

    finished.sort(key=lambda r: (r.elapsed_ms, r.bib))
    for r in finished:
        # competition ranking: rank = 1 + number of strictly faster results
        r.rank = 1 + sum(1 for o in finished if o.elapsed_ms < r.elapsed_ms)

    dnf.sort(key=lambda r: r.bib)
    return finished + dnf
