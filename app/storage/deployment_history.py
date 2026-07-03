"""SQLite storage for deployment records and webhook delivery tracking."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class DeploymentHistoryStorage:
    """Manages persistent SQLite storage with WAL mode for concurrency and deduplication."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._initialize_database()

    def _get_connection(self) -> sqlite3.Connection:
        """Create a connection with timeout and foreign keys enabled."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.database_path.as_posix(),
            timeout=30.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Cursor, None, None]:
        """Provide a transactional cursor context."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize_database(self) -> None:
        """Initialize database schema with WAL mode and required tables."""
        with self.transaction() as cur:
            cur.execute("PRAGMA journal_mode = WAL;")
            cur.execute("PRAGMA synchronous = NORMAL;")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    repository_name TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    status TEXT NOT NULL
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY,
                    delivery_id TEXT,
                    repository_id TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    requested_commit TEXT NOT NULL,
                    deployed_commit TEXT,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    duration_seconds REAL,
                    docker_exit_code INTEGER,
                    health_status TEXT,
                    error_summary TEXT,
                    logs TEXT
                );
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_deployments_repo_created
                ON deployments(repository_id, created_at DESC);
            """)

    def record_delivery(
        self,
        delivery_id: str,
        event_type: str,
        repository_name: str,
        status: str,
    ) -> bool:
        """Record an incoming delivery.

        Returns True if newly recorded, False if already exists (duplicate).
        """
        now = datetime.now(UTC).isoformat()
        try:
            with self.transaction() as cur:
                cur.execute(
                    """
                    INSERT INTO webhook_deliveries
                        (delivery_id, event_type, repository_name, received_at, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (delivery_id, event_type, repository_name, now, status),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def is_duplicate_delivery(self, delivery_id: str) -> bool:
        """Check whether a delivery ID has already been recorded."""
        with self.transaction() as cur:
            cur.execute(
                "SELECT 1 FROM webhook_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            )
            return cur.fetchone() is not None

    def update_delivery_status(self, delivery_id: str, status: str) -> None:
        """Update the status of a recorded delivery."""
        with self.transaction() as cur:
            cur.execute(
                "UPDATE webhook_deliveries SET status = ? WHERE delivery_id = ?",
                (status, delivery_id),
            )

    def prune_old_deliveries(self, max_records: int = 1000) -> int:
        """Keep database size bounded by removing old deliveries beyond limit."""
        with self.transaction() as cur:
            cur.execute(
                """
                DELETE FROM webhook_deliveries
                WHERE delivery_id NOT IN (
                    SELECT delivery_id FROM webhook_deliveries
                    ORDER BY received_at DESC
                    LIMIT ?
                )
                """,
                (max_records,),
            )
            return cur.rowcount
