import sqlite3
from contextlib import contextmanager
from pathlib import Path

from schema import INDEX_STATEMENTS, SCHEMA_STATEMENTS


def connect(database_path: Path) -> sqlite3.Connection:
    # Disable sqlite3 implicit transaction management so each statement is
    # committed immediately unless we explicitly wrap it in transaction(...).
    # This reduces long-lived write locks in bot request handlers.
    connection = sqlite3.connect(str(database_path), timeout=10.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    try:
        connection.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        # If another process is briefly holding the database when we connect,
        # do not block startup on WAL switching; the connection still benefits
        # from timeout/busy_timeout and can continue in the existing mode.
        pass
    return connection


def ensure_parent_dir(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)


def initialize_schema(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    for statement in INDEX_STATEMENTS:
        connection.execute(statement)
    connection.commit()


@contextmanager
def transaction(connection: sqlite3.Connection):
    started_here = not connection.in_transaction
    if started_here:
        connection.execute("BEGIN")
    try:
        yield connection
    except Exception:
        if started_here and connection.in_transaction:
            connection.rollback()
        raise
    else:
        if started_here and connection.in_transaction:
            connection.commit()
