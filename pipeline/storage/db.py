"""SQLite state: the history cursor, dedup claims, and an event log.

A fresh connection per operation, not a shared module-level one: the webhook
runs on a threadpool and SQLite objects are not portable across threads.

processed_messages.issue_number carries a three-way convention:
    NULL  -> claimed, still in flight (may have died mid-run; recoverable)
    0     -> DROPPED: terminally seen but not an issue (gate/LLM reject, a
             deleted message, or a failed one) — never reprocess it
    >= 1  -> the number of the issue this message opened
Overlapping Gmail history windows re-list the same ids repeatedly, so a durable
terminal marker is what lets the bridge skip a decided message with one indexed
lookup instead of re-fetching and re-scanning GitHub every time.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from pipeline.config import get_settings

_LAST_HISTORY_ID = "last_history_id"

# Sentinel issue_number for a message that is terminally decided but produced no
# issue. GitHub issue numbers start at 1, so 0 can never collide with a real one.
DROPPED = 0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id   TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    issue_number INTEGER
);
CREATE TABLE IF NOT EXISTS pipeline_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT NOT NULL,
    stage          TEXT NOT NULL,
    ts             TEXT NOT NULL,
    message_id     TEXT,
    issue_number   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_corr ON events(correlation_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _diagnose(db_path: str, parent: str, exc: Exception) -> str:
    """Turn an opaque open failure into something you can act on."""
    target = parent or "."
    facts = [
        f"DB_PATH={db_path!r}",
        f"dir={target!r}",
        f"dir_exists={os.path.isdir(target)}",
        f"dir_writable={os.access(target, os.W_OK)}",
        f"running_as_uid={os.getuid()}",
    ]
    if os.path.isdir(target):
        st = os.stat(target)
        facts.append(f"dir_owner_uid={st.st_uid}")
        facts.append(f"dir_mode={oct(st.st_mode & 0o777)}")
    return f"cannot open database ({exc}). " + " ".join(facts)


@contextmanager
def connect(settings=None):
    settings = settings or get_settings()
    parent = os.path.dirname(settings.db_path)
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(settings.db_path, timeout=10, check_same_thread=False)
    except (sqlite3.OperationalError, OSError) as exc:
        # SQLite says only "unable to open database file" whether the directory
        # is missing, unwritable, or read-only. On a mounted volume it is nearly
        # always a permissions problem, so report what we can actually see —
        # otherwise the deploy log gives you nothing to act on.
        raise sqlite3.OperationalError(_diagnose(settings.db_path, parent, exc)) from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(settings=None) -> None:
    with connect(settings) as conn:
        conn.executescript(_SCHEMA)


def get_state(key: str, settings=None) -> str | None:
    with connect(settings) as conn:
        row = conn.execute(
            "SELECT value FROM pipeline_state WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else None


def set_state(key: str, value: str, settings=None) -> None:
    with connect(settings) as conn:
        conn.execute(
            "INSERT INTO pipeline_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def set_state_if_absent(key: str, value: str, settings=None) -> bool:
    """Write only when unset. True if this call wrote it.

    Used for the history cursor on startup — clobbering a live cursor on every
    restart would silently skip whatever arrived in between.
    """
    with connect(settings) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO pipeline_state(key, value) VALUES(?, ?)",
            (key, str(value)),
        )
        return cur.rowcount == 1


def get_last_history_id(settings=None) -> int | None:
    raw = get_state(_LAST_HISTORY_ID, settings)
    return int(raw) if raw is not None else None


def set_last_history_id(value: int, settings=None) -> None:
    set_state(_LAST_HISTORY_ID, str(value), settings)


def claim_message(message_id: str, settings=None) -> bool:
    """Atomically claim a message. True if this caller won it.

    INSERT OR IGNORE rather than a SELECT-then-INSERT: two overlapping Pub/Sub
    deliveries would both pass a read check and both open an issue. The row is
    written BEFORE the issue exists, so issue_number stays NULL until
    mark_processed — see get_claim() for how a stale claim is recovered.
    """
    with connect(settings) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO processed_messages(message_id, processed_at) "
            "VALUES(?, ?)",
            (message_id, _now()),
        )
        return cur.rowcount == 1


def get_claim(message_id: str, settings=None) -> sqlite3.Row | None:
    with connect(settings) as conn:
        return conn.execute(
            "SELECT message_id, processed_at, issue_number FROM processed_messages "
            "WHERE message_id = ?",
            (message_id,),
        ).fetchone()


def mark_processed(message_id: str, issue_number: int | None = None, settings=None) -> None:
    with connect(settings) as conn:
        conn.execute(
            "INSERT INTO processed_messages(message_id, processed_at, issue_number) "
            "VALUES(?, ?, ?) ON CONFLICT(message_id) DO UPDATE SET "
            "processed_at = excluded.processed_at, issue_number = excluded.issue_number",
            (message_id, _now(), issue_number),
        )


def mark_dropped(message_id: str, settings=None) -> None:
    """Terminally mark a message as seen-but-not-an-issue.

    Used for gate/LLM rejects and for messages we cannot fetch or process (e.g. a
    deleted message that 404s). Writes the DROPPED sentinel so it is distinct from
    a claimed-but-pending row (NULL issue_number), which keeps overlapping history
    windows from re-fetching and re-scanning it forever.
    """
    mark_processed(message_id, DROPPED, settings)


def log_event(
    correlation_id: str,
    stage: str,
    message_id: str | None = None,
    issue_number: int | None = None,
    settings=None,
) -> None:
    with connect(settings) as conn:
        conn.execute(
            "INSERT INTO events(correlation_id, stage, ts, message_id, issue_number) "
            "VALUES(?, ?, ?, ?, ?)",
            (correlation_id, stage, _now(), message_id, issue_number),
        )
