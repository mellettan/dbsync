import os

import psycopg
import pytest

from dbsync.config import SyncConfig
from dbsync.decisions import AutoDecider
from dbsync.runner import run_sync

TEST_DSN = os.getenv("TEST_DB_DSN")
PROD_DSN = os.getenv("PROD_DB_DSN")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (TEST_DSN and PROD_DSN), reason="TEST_DB_DSN и PROD_DB_DSN не заданы"
    ),
]

TEST_SCHEMA = """
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
CREATE TABLE users (id integer PRIMARY KEY, email text UNIQUE NOT NULL, name text NOT NULL);
CREATE TABLE orders (
    id integer PRIMARY KEY,
    user_id integer NOT NULL REFERENCES users (id),
    total numeric(10, 2) NOT NULL DEFAULT 0
);
INSERT INTO users VALUES (1, 'ann@example.com', 'Анна'), (2, 'bob@example.com', 'Борис'), (3, 'eve@example.com', 'Ева');
INSERT INTO orders VALUES (10, 2, 1500.00), (11, 3, 99.90);
"""

PROD_SCHEMA = """
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
CREATE TABLE users (id integer PRIMARY KEY, email text UNIQUE NOT NULL, legacy_flag boolean);
CREATE TABLE audit_log (id serial PRIMARY KEY, message text);
INSERT INTO users VALUES (1, 'ann@example.com', true);
"""


def execute(dsn: str, script: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(script)


def query(dsn: str, sql: str) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(sql).fetchall()


@pytest.fixture
def databases() -> SyncConfig:
    execute(TEST_DSN, TEST_SCHEMA)
    execute(PROD_DSN, PROD_SCHEMA)
    return SyncConfig(test_dsn=TEST_DSN, prod_dsn=PROD_DSN)


def test_dry_run_changes_nothing(
    databases: SyncConfig, capsys: pytest.CaptureFixture[str]
):
    run_sync(databases, AutoDecider(allow_drop=True), dry_run=True)

    assert "+ CREATE TABLE orders" in capsys.readouterr().out
    assert query(PROD_DSN, "SELECT count(*) FROM users") == [(1,)]
    assert query(PROD_DSN, "SELECT to_regclass('orders')") == [(None,)]


def test_full_sync_makes_prod_match_test(databases: SyncConfig):
    run_sync(databases, AutoDecider(allow_drop=True, not_null_default="—"))

    columns = query(
        PROD_DSN,
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position",
    )
    assert columns == [
        ("orders", "id"),
        ("orders", "user_id"),
        ("orders", "total"),
        ("users", "id"),
        ("users", "email"),
        ("users", "name"),
    ]
    assert query(PROD_DSN, "SELECT id, email, name FROM users ORDER BY id") == [
        (1, "ann@example.com", "Анна"),
        (2, "bob@example.com", "Борис"),
        (3, "eve@example.com", "Ева"),
    ]
    assert query(
        PROD_DSN, "SELECT id, user_id, total::text FROM orders ORDER BY id"
    ) == [
        (10, 2, "1500.00"),
        (11, 3, "99.90"),
    ]
    assert query(
        PROD_DSN,
        "SELECT count(*) FROM information_schema.table_constraints "
        "WHERE table_name = 'orders' AND constraint_type = 'FOREIGN KEY'",
    ) == [(1,)]


def test_without_allow_drop_extra_objects_are_kept(databases: SyncConfig):
    run_sync(databases, AutoDecider(not_null_default="—"))

    assert query(PROD_DSN, "SELECT to_regclass('audit_log')::text") == [("audit_log",)]
    assert ("legacy_flag",) in query(
        PROD_DSN,
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'",
    )


def test_second_run_has_nothing_to_change(
    databases: SyncConfig, capsys: pytest.CaptureFixture[str]
):
    run_sync(databases, AutoDecider(allow_drop=True, not_null_default="—"))
    capsys.readouterr()

    run_sync(databases, AutoDecider(allow_drop=True), dry_run=True)

    assert "Схемы совпадают" in capsys.readouterr().out
