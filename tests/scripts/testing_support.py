from __future__ import annotations

import shutil
import os
import uuid
from contextlib import contextmanager
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
TEST_DATABASE_PATH = DATA_DIR / "testing.db"
PRODUCTION_DATABASE_PATH = DATA_DIR / "tournament_hub.sqlite3"
TEST_TEMP_ROOT = DATA_DIR / "tmp" / "tests"


def set_test_database_environment() -> None:
    os.environ["TOURNAMENT_HUB_DB_PATH"] = str(TEST_DATABASE_PATH)
    os.environ["TOURNAMENT_HUB_TEST_DB_PATH"] = str(TEST_DATABASE_PATH)


def ensure_test_database_parent() -> None:
    TEST_DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)


def ensure_test_temp_root() -> None:
    TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)


def reset_test_database() -> None:
    ensure_test_database_parent()
    if TEST_DATABASE_PATH.exists():
        try:
            TEST_DATABASE_PATH.unlink()
        except PermissionError as exc:
            raise RuntimeError(
                "testing.db is currently in use. Run only one test entrypoint at a time."
            ) from exc


def bootstrap_test_database() -> None:
    from bootstrap import bootstrap_all
    from db import connect, initialize_schema, transaction

    ensure_test_database_parent()
    connection = connect(TEST_DATABASE_PATH)
    try:
        with transaction(connection):
            initialize_schema(connection)
            bootstrap_all(connection)
    finally:
        connection.close()


@contextmanager
def test_temporary_directory(prefix: str):
    ensure_test_temp_root()
    temp_dir = TEST_TEMP_ROOT / "{}{}".format(prefix, uuid.uuid4().hex)
    temp_dir.mkdir(parents=True, exist_ok=False)
    try:
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@contextmanager
def fresh_test_connection():
    from db import connect

    reset_test_database()
    bootstrap_test_database()
    connection = connect(TEST_DATABASE_PATH)
    try:
        yield connection
    finally:
        connection.close()
