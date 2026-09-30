"""Test fixtures: a real PostgreSQL (via pgserver) + FastAPI TestClient."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="session")
def database_url(tmp_path_factory):
    pgserver = pytest.importorskip("pgserver")
    server = pgserver.get_server(str(tmp_path_factory.mktemp("pgdata")))
    uri = server.get_uri()
    if uri.startswith("postgresql://"):
        uri = uri.replace("postgresql://", "postgresql+psycopg2://", 1)
    os.environ["DATABASE_URL"] = uri
    return uri


@pytest.fixture(scope="session")
def app(database_url):
    from app.db import Base, engine
    from app.main import app as fastapi_app

    Base.metadata.create_all(engine)
    return fastapi_app


@pytest.fixture(scope="session")
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_db(app):
    from sqlalchemy import text

    from app.db import engine

    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE result_entries, result_versions, scans, "
                "participants, checkpoints, races RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture()
def race(client):
    """A race with START/CP1/FINISH checkpoints and 5 participants."""
    rid = client.post("/races", json={"name": "Mountain 50K"}).json()["id"]
    resp = client.put(
        f"/races/{rid}/checkpoints",
        json={"checkpoints": [{"code": c} for c in ["START", "CP1", "FINISH"]]},
    )
    assert resp.status_code == 200, resp.text
    for bib, name in [
        ("001", "Alice"),
        ("002", "Bob"),
        ("003", "Carol"),
        ("004", "Dave"),
        ("005", "Erin"),
    ]:
        resp = client.post(
            f"/races/{rid}/participants", json={"bib": bib, "name": name}
        )
        assert resp.status_code == 201, resp.text
    return rid


def upload(client, rid, message_id, bib, code, event_time):
    return client.post(
        f"/races/{rid}/scans",
        json={
            "message_id": message_id,
            "participant_bib": bib,
            "checkpoint_code": code,
            "event_time": event_time,
        },
    )
