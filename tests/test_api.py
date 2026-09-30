"""End-to-end API tests against a real PostgreSQL."""
from __future__ import annotations

import asyncio

from conftest import upload


# ---------- basic meta ----------


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_unknown_race(client):
    resp = client.get("/races/999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "race_not_found"


# ---------- standings computation ----------


def _seed_scans(client, rid):
    """Out-of-order upload; Alice & Bob tie, Carol wins, Dave misses CP1,
    Erin's CP1 predates her START (no strictly increasing chain)."""
    scans = [
        # message_id, bib, checkpoint, event_time (uploaded out of order)
        ("m-a-fin", "001", "FINISH", "2026-09-30T11:00:00Z"),
        ("m-a-start", "001", "START", "2026-09-30T10:00:00Z"),
        ("m-a-cp1", "001", "CP1", "2026-09-30T10:30:00Z"),
        ("m-b-cp1", "002", "CP1", "2026-09-30T10:31:00Z"),
        ("m-b-fin", "002", "FINISH", "2026-09-30T11:00:00Z"),
        ("m-b-start", "002", "START", "2026-09-30T10:00:00Z"),
        ("m-c-fin", "003", "FINISH", "2026-09-30T10:50:00Z"),
        ("m-c-start", "003", "START", "2026-09-30T10:05:00Z"),
        ("m-c-cp1", "003", "CP1", "2026-09-30T10:20:00Z"),
        ("m-d-start", "004", "START", "2026-09-30T10:00:00Z"),
        ("m-d-fin", "004", "FINISH", "2026-09-30T10:40:00Z"),
        ("m-e-start", "005", "START", "2026-09-30T10:00:00Z"),
        ("m-e-cp1", "005", "CP1", "2026-09-30T09:59:00Z"),  # before her START
        ("m-e-fin", "005", "FINISH", "2026-09-30T10:30:00Z"),
    ]
    for m, bib, code, t in scans:
        resp = upload(client, rid, m, bib, code, t)
        assert resp.status_code == 201, resp.text


def test_standings_ranking_ties_and_dnf(client, race):
    _seed_scans(client, race)
    resp = client.get(f"/races/{race}/standings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["checkpoint_order"] == ["START", "CP1", "FINISH"]

    entries = body["entries"]
    finished = [e for e in entries if e["status"] == "finished"]
    dnf = [e for e in entries if e["status"] == "dnf"]

    # Carol 45:00 first; Alice & Bob tie at 60:00 and share rank 2
    assert [(e["bib"], e["rank"], e["elapsed_ms"]) for e in finished] == [
        ("003", 1, 45 * 60 * 1000),
        ("001", 2, 60 * 60 * 1000),
        ("002", 2, 60 * 60 * 1000),
    ]

    dnf_by_bib = {e["bib"]: e for e in dnf}
    assert set(dnf_by_bib) == {"004", "005"}
    assert "missing checkpoint 'CP1'" in dnf_by_bib["004"]["reason"]
    assert "strictly after" in dnf_by_bib["005"]["reason"]

    # basis records are the accepted scans in checkpoint order
    carol = finished[0]
    assert [b["checkpoint_code"] for b in carol["basis"]] == [
        "START",
        "CP1",
        "FINISH",
    ]
    assert [b["message_id"] for b in carol["basis"]] == [
        "m-c-start",
        "m-c-cp1",
        "m-c-fin",
    ]


def test_first_valid_pass_selected(client, race):
    """A second, later scan at the same checkpoint must not replace the
    first valid pass; an early scan that breaks strict increase is skipped."""
    upload(client, race, "x1", "001", "START", "2026-09-30T10:00:00Z")
    upload(client, race, "x2", "001", "CP1", "2026-09-30T10:30:00Z")
    # duplicate pass at CP1 one minute later - must be ignored
    upload(client, race, "x3", "001", "CP1", "2026-09-30T10:31:00Z")
    upload(client, race, "x4", "001", "FINISH", "2026-09-30T11:00:00Z")

    entries = client.get(f"/races/{race}/standings").json()["entries"]
    alice = next(e for e in entries if e["bib"] == "001")
    assert alice["status"] == "finished"
    assert alice["elapsed_ms"] == 3600_000
    assert [b["message_id"] for b in alice["basis"]] == ["x1", "x2", "x4"]


# ---------- idempotency & conflicts ----------


def test_scan_idempotent_replay_and_conflict(client, race):
    resp = upload(client, race, "dup-1", "001", "START", "2026-09-30T10:00:00Z")
    assert resp.status_code == 201
    assert resp.json()["status"] == "created"

    # identical retransmission -> idempotent no-op
    resp = upload(client, race, "dup-1", "001", "START", "2026-09-30T10:00:00Z")
    assert resp.status_code == 200
    assert resp.json()["status"] == "duplicate"

    # same message_id, different content -> rejected
    resp = upload(client, race, "dup-1", "001", "START", "2026-09-30T10:00:01Z")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "scan_conflict"
    resp = upload(client, race, "dup-1", "002", "START", "2026-09-30T10:00:00Z")
    assert resp.status_code == 409

    # only one record stored
    scans = client.get(f"/races/{race}/scans").json()
    assert len([s for s in scans if s["message_id"] == "dup-1"]) == 1


def test_scan_unknown_participant_and_checkpoint(client, race):
    resp = upload(client, race, "u1", "999", "START", "2026-09-30T10:00:00Z")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "participant_not_found"

    resp = upload(client, race, "u2", "001", "NOPE", "2026-09-30T10:00:00Z")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "checkpoint_not_found"


def test_scan_requires_timezone(client, race):
    resp = upload(client, race, "tz1", "001", "START", "2026-09-30T10:00:00")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_batch_upload_mixed(client, race):
    resp = client.post(
        f"/races/{race}/scans/batch",
        json={
            "scans": [
                {
                    "message_id": "b1",
                    "participant_bib": "001",
                    "checkpoint_code": "START",
                    "event_time": "2026-09-30T10:00:00Z",
                },
                {
                    "message_id": "b1",  # duplicate within the same batch
                    "participant_bib": "001",
                    "checkpoint_code": "START",
                    "event_time": "2026-09-30T10:00:00Z",
                },
                {
                    "message_id": "b2",
                    "participant_bib": "999",
                    "checkpoint_code": "START",
                    "event_time": "2026-09-30T10:00:00Z",
                },
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert (body["created"], body["duplicates"], body["errors"]) == (1, 1, 1)
    assert body["results"][2]["error"]["code"] == "participant_not_found"


# ---------- revocation ----------


def test_revoke_recomputes_standings(client, race):
    _seed_scans(client, race)

    resp = client.post(f"/races/{race}/scans/m-c-fin/revoke")
    assert resp.status_code == 200
    assert resp.json()["already_revoked"] is False

    entries = client.get(f"/races/{race}/standings").json()["entries"]
    carol = next(e for e in entries if e["bib"] == "003")
    assert carol["status"] == "dnf"
    assert "missing checkpoint 'FINISH'" in carol["reason"]

    # revoking again is an idempotent no-op
    resp = client.post(f"/races/{race}/scans/m-c-fin/revoke")
    assert resp.status_code == 200
    assert resp.json()["already_revoked"] is True


def test_revoke_unknown_scan(client, race):
    resp = client.post(f"/races/{race}/scans/no-such-message/revoke")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "scan_not_found"


# ---------- publishing & versions ----------


def test_publish_freezes_and_late_changes_go_to_next_version(client, race):
    _seed_scans(client, race)

    v1 = client.post(f"/races/{race}/results/publish")
    assert v1.status_code == 201
    v1 = v1.json()
    assert v1["version_no"] == 1
    assert [e["bib"] for e in v1["entries"][:3]] == ["003", "001", "002"]

    # late record + a revocation after the publish
    upload(client, race, "late-1", "004", "CP1", "2026-09-30T10:20:00Z")
    client.post(f"/races/{race}/scans/m-b-fin/revoke")

    # live standings moved on ...
    live = client.get(f"/races/{race}/standings").json()
    dave = next(e for e in live["entries"] if e["bib"] == "004")
    bob = next(e for e in live["entries"] if e["bib"] == "002")
    assert dave["status"] == "finished"
    assert bob["status"] == "dnf"

    # ... but version 1 is frozen and still queryable
    again = client.get(f"/races/{race}/results/versions/1").json()
    assert again == client.get(f"/races/{race}/results").json()
    dave_v1 = next(e for e in again["entries"] if e["bib"] == "004")
    bob_v1 = next(e for e in again["entries"] if e["bib"] == "002")
    assert dave_v1["status"] == "dnf"
    assert bob_v1["status"] == "finished"

    # next publish picks the changes up as version 2
    v2 = client.post(f"/races/{race}/results/publish").json()
    assert v2["version_no"] == 2
    dave_v2 = next(e for e in v2["entries"] if e["bib"] == "004")
    bob_v2 = next(e for e in v2["entries"] if e["bib"] == "002")
    assert dave_v2["status"] == "finished"
    assert bob_v2["status"] == "dnf"

    versions = client.get(f"/races/{race}/results/versions").json()
    assert [v["version_no"] for v in versions] == [1, 2]


def test_version_not_found(client, race):
    resp = client.get(f"/races/{race}/results")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "no_published_results"
    resp = client.get(f"/races/{race}/results/versions/7")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "version_not_found"


def test_concurrent_publishes_are_consecutive_and_consistent(client, race, app):
    _seed_scans(client, race)

    import httpx

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            return await asyncio.gather(
                *[ac.post(f"/races/{race}/results/publish") for _ in range(5)]
            )

    responses = asyncio.run(run())
    assert all(r.status_code == 201 for r in responses)
    assert sorted(r.json()["version_no"] for r in responses) == [1, 2, 3, 4, 5]

    # every version is individually consistent and identical in content
    bodies = [client.get(f"/races/{race}/results/versions/{n}").json() for n in range(1, 6)]
    first = [(e["bib"], e["rank"], e["status"]) for e in bodies[0]["entries"]]
    for b in bodies[1:]:
        assert [(e["bib"], e["rank"], e["status"]) for e in b["entries"]] == first


# ---------- checkpoint management ----------


def test_checkpoint_validation_and_removal_guard(client):
    rid = client.post("/races", json={"name": "R"}).json()["id"]

    # fewer than two checkpoints rejected by schema
    resp = client.put(
        f"/races/{rid}/checkpoints", json={"checkpoints": [{"code": "START"}]}
    )
    assert resp.status_code == 422

    # duplicate codes rejected
    resp = client.put(
        f"/races/{rid}/checkpoints",
        json={"checkpoints": [{"code": "A"}, {"code": "A"}]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "duplicate_checkpoint_code"

    client.put(
        f"/races/{rid}/checkpoints",
        json={"checkpoints": [{"code": "START"}, {"code": "FINISH"}]},
    )
    client.post(f"/races/{rid}/participants", json={"bib": "1", "name": "Solo"})
    upload(client, rid, "s1", "1", "START", "2026-09-30T10:00:00Z")

    # adding a new checkpoint is fine
    resp = client.put(
        f"/races/{rid}/checkpoints",
        json={"checkpoints": [{"code": "START"}, {"code": "MID"}, {"code": "FINISH"}]},
    )
    assert resp.status_code == 200
    # but removing a checkpoint that has scans is rejected
    resp = client.put(
        f"/races/{rid}/checkpoints",
        json={"checkpoints": [{"code": "MID"}, {"code": "FINISH"}]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "checkpoint_in_use"


def test_participant_re_registration(client, race):
    resp = client.post(
        f"/races/{race}/participants", json={"bib": "001", "name": "Alice"}
    )
    assert resp.status_code == 200  # idempotent
    resp = client.post(
        f"/races/{race}/participants", json={"bib": "001", "name": "Alicia"}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "participant_conflict"
