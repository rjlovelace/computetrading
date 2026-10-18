import os

import pytest

from ctrade import services
from ctrade.db import Base, get_engine, init_db, session_scope


@pytest.fixture
def url(tmp_path):
    """SQLite by default; set TEST_DATABASE_URL to run against Postgres (CI does)."""
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    Base.metadata.drop_all(get_engine(url))
    init_db(url)
    with session_scope(url) as s:
        services.seed_products(s)
    return url
