"""Session-wide pytest fixtures.

--run-postgres opts in to the disposable-PostgreSQL-backed multiuser/RLS
tests (tests/test_multiuser_rls.py and friends) — see
tests/support/postgres_harness.py. Without the flag those tests are skipped,
so a plain `pytest` run stays fast and needs no Docker.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-postgres",
        action="store_true",
        default=False,
        help="run tests against a newly-created disposable PostgreSQL container",
    )


@pytest.fixture(scope="session")
def multiuser_postgres(request: pytest.FixtureRequest):
    if not request.config.getoption("--run-postgres"):
        pytest.skip("requires --run-postgres disposable PostgreSQL opt-in")
    from tests.support.postgres_harness import DisposablePostgres

    harness = DisposablePostgres()
    harness.start()
    try:
        yield harness
    finally:
        harness.stop()


@pytest.fixture
def clean_multiuser_postgres(multiuser_postgres):
    multiuser_postgres.reset()
    return multiuser_postgres


@pytest.fixture(autouse=True)
def _reset_store_singleton():
    """Prevent a PostgresStore instance (and its cached SB_MULTIUSER flag)
    from crossing test cases — mirrors the equivalent fixture in
    Evo_PRISM_lab_access/tests/conftest.py."""
    from mcp_second_brain.store.factory import reset_store

    reset_store()
    yield
    reset_store()
