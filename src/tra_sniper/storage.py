from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .models import BookingRequest


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class UserRecord:
    id: int
    email: str
    password_hash: str
    created_at: str


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: str
    user_id: int
    status: str
    scheduled_at: str
    route: str
    ride_date: str
    order_type: str
    created_at: str
    updated_at: str
    last_error: str | None


class PayloadCipher:
    def __init__(self, data_dir: Path, configured_key: str | None = None) -> None:
        key = configured_key or os.getenv("TRA_ENCRYPTION_KEY")
        if key:
            self._fernet = Fernet(key.encode("ascii"))
            return

        key_path = data_dir / "fernet.key"
        if key_path.exists():
            key_bytes = key_path.read_bytes().strip()
        else:
            data_dir.mkdir(parents=True, exist_ok=True)
            key_bytes = Fernet.generate_key()
            key_path.write_bytes(key_bytes + b"\n")
            try:
                key_path.chmod(0o600)
            except OSError:
                pass
        self._fernet = Fernet(key_bytes)

    def encrypt(self, data: dict[str, Any]) -> bytes:
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(raw)

    def decrypt(self, value: bytes) -> dict[str, Any]:
        try:
            return json.loads(self._fernet.decrypt(value))
        except (InvalidToken, json.JSONDecodeError) as exc:
            raise ValueError("task payload could not be decrypted") from exc


class Database:
    def __init__(self, path: str | Path | None = None, *, encryption_key: str | None = None) -> None:
        data_dir = Path(os.getenv("TRA_DATA_DIR", "./data")).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = Path(path).resolve() if path else data_dir / "tra_sniper.db"
        self.cipher = PayloadCipher(self.path.parent, encryption_key)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    email TEXT NOT NULL COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    route TEXT NOT NULL,
                    ride_date TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_user_created "
                "ON tasks(user_id, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_due "
                "ON tasks(status, scheduled_at) WHERE status = 'scheduled'"
            )
            connection.execute("PRAGMA optimize")

    def create_user(self, email: str, password_hash: str) -> UserRecord:
        created_at = utc_now()
        try:
            with self.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO users(email, password_hash, created_at) VALUES (?, ?, ?)",
                    (email.strip().lower(), password_hash, created_at),
                )
                user_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValueError("email is already registered") from exc
        return UserRecord(user_id, email.strip().lower(), password_hash, created_at)

    def get_user_by_email(self, email: str) -> UserRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, email, password_hash, created_at FROM users WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
        return UserRecord(**dict(row)) if row else None

    def get_user(self, user_id: int) -> UserRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, email, password_hash, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return UserRecord(**dict(row)) if row else None

    def create_task(
        self,
        user_id: int,
        request: BookingRequest,
        scheduled_at: str,
        payload: dict[str, Any],
    ) -> TaskRecord:
        task_id = str(uuid.uuid4())
        now = utc_now()
        route = f"{request.start_station} → {request.end_station}"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks(
                    id, user_id, status, scheduled_at, payload, route, ride_date,
                    order_type, created_at, updated_at
                ) VALUES (?, ?, 'scheduled', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    user_id,
                    scheduled_at,
                    self.cipher.encrypt(payload),
                    route,
                    request.outbound.ride_date,
                    request.order_type.value,
                    now,
                    now,
                ),
            )
        task = self.get_task(task_id, user_id)
        if task is None:
            raise RuntimeError("task was not persisted")
        return task

    def list_tasks(self, user_id: int) -> list[TaskRecord]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, status, scheduled_at, route, ride_date, order_type,
                       created_at, updated_at, last_error
                FROM tasks WHERE user_id = ? ORDER BY created_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [TaskRecord(**dict(row)) for row in rows]

    def get_task(self, task_id: str, user_id: int) -> TaskRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, status, scheduled_at, route, ride_date, order_type,
                       created_at, updated_at, last_error
                FROM tasks WHERE id = ? AND user_id = ?
                """,
                (task_id, user_id),
            ).fetchone()
        return TaskRecord(**dict(row)) if row else None

    def get_task_payload(self, task_id: str, user_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM tasks WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            ).fetchone()
        if not row:
            raise KeyError(task_id)
        return self.cipher.decrypt(row["payload"])

    def update_task_status(
        self,
        task_id: str,
        user_id: int,
        status: str,
        *,
        last_error: str | None = None,
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks SET status = ?, updated_at = ?, last_error = ?
                WHERE id = ? AND user_id = ?
                """,
                (status, utc_now(), last_error, task_id, user_id),
            )
        return cursor.rowcount == 1

    def promote_due_tasks(self, now: str | None = None) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks SET status = 'waiting_human', updated_at = ?
                WHERE status = 'scheduled' AND scheduled_at <= ?
                """,
                (utc_now(), now or utc_now()),
            )
        return cursor.rowcount
