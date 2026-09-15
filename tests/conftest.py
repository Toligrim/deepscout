import dataclasses
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    """Point app.db at an isolated SQLite file for the duration of a test."""
    from app import db as store

    isolated = dataclasses.replace(store.settings, db_path=tmp_path / "test.db")
    monkeypatch.setattr(store, "settings", isolated)
    store.init_db()
    return isolated.db_path


@pytest.fixture
def client(temp_db):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
