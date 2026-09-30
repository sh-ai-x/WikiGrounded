"""Mock ticket ledger — in-process + SQLite-backed.

Step 2 ships an in-memory ledger with SQLite persistence for the action_key
dedup invariant: a repeated publish with the same action_key returns the
existing outcome; mutated args raise.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class TicketRecord:
    id: str
    action_key: str
    title: str
    body: str
    created_at: str  # ISO-8601
    published_by: str  # principal_id


class DuplicateArgsError(RuntimeError):
    """Raised when a publish is attempted with mutated args after the original action_key exists."""


class TicketLedger:
    """Mock ticket service. Idempotent on action_key; rejects mutated args."""

    def __init__(self, db_path: str | None = None) -> None:
        # Default to a process-local temp file. ":memory:" would create a
        # private DB per connection, defeating the per-action dedup invariant.
        if db_path is None:
            self._db_path = str(Path(tempfile.gettempdir()) / f"tickets-{uuid.uuid4().hex[:8]}.db")
        elif db_path == ":memory:":
            self._db_path = str(Path(tempfile.gettempdir()) / f"tickets-{uuid.uuid4().hex[:8]}.db")
        else:
            self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY,
                    action_key TEXT NOT NULL UNIQUE,
                    args_canonical TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    published_by TEXT NOT NULL
                )
                """
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self._db_path)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    @staticmethod
    def canonicalize(args: dict[str, Any]) -> str:
        return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)

    def publish(
        self,
        *,
        action_key: str,
        title: str,
        body: str,
        published_by: str,
        args: dict[str, Any],
    ) -> TicketRecord:
        """Idempotent on action_key.

    Same action_key + same canonical args -> return existing record.
    Same action_key + DIFFERENT canonical args -> DuplicateArgsError.
    """
        canonical = self.canonicalize(args)
        with self._lock:
            with self._conn() as c:
                row = c.execute(
                    "SELECT id, args_canonical, title, body, created_at, published_by "
                    "FROM tickets WHERE action_key = ?",
                    (action_key,),
                ).fetchone()
                if row is not None:
                    if row["args_canonical"] != canonical:
                        raise DuplicateArgsError(
                            f"action_key {action_key!r} already exists with different args"
                        )
                    return TicketRecord(
                        id=row["id"],
                        action_key=action_key,
                        title=row["title"],
                        body=row["body"],
                        created_at=row["created_at"],
                        published_by=row["published_by"],
                    )
                rec_id = uuid.uuid4().hex[:32]
                created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                c.execute(
                    "INSERT INTO tickets (id, action_key, args_canonical, title, body, created_at, published_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (rec_id, action_key, canonical, title, body, created_at, published_by),
                )
                c.commit()
                return TicketRecord(
                    id=rec_id,
                    action_key=action_key,
                    title=title,
                    body=body,
                    created_at=created_at,
                    published_by=published_by,
                )

    def count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) AS n FROM tickets").fetchone()["n"]
