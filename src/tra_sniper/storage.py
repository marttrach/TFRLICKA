from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    token_version: int


# Monitoring is a poll loop, not a promise of seats: TDX exposes no TRA seat
# availability (PLAN.md 7.1), so a check can report a timetable change or that a
# task is ready for a person, never "there is a seat". See PLAN.md section 13.
MODE_MONITOR_ONLY = "monitor_only"
MODE_BOOK_WHEN_AVAILABLE = "book_when_available"
TASK_MODES = frozenset({MODE_MONITOR_ONLY, MODE_BOOK_WHEN_AVAILABLE})

DEFAULT_POLL_INTERVAL_SECONDS = 300
MIN_POLL_INTERVAL_SECONDS = 60
MAX_BACKOFF_MULTIPLIER = 8

# Statuses the poll loop may claim from. Everything else — a live booking
# session, a finished task — is deliberately excluded so polling can never
# reopen a browser behind the person's back.
POLLABLE_STATUSES = ("scheduled", "monitoring")

TASK_COLUMNS = (
    "id, user_id, status, scheduled_at, route, ride_date, order_type, "
    "created_at, updated_at, last_error, booking_code, mode, "
    "poll_interval_seconds, monitor_until, last_checked_at, next_check_at, "
    "check_failures"
)


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
    booking_code: str | None = None
    mode: str = MODE_BOOK_WHEN_AVAILABLE
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    monitor_until: str | None = None
    last_checked_at: str | None = None
    next_check_at: str | None = None
    check_failures: int = 0

    @property
    def monitor_start_at(self) -> str:
        """`scheduled_at` has always meant "when to start"; this names it."""
        return self.scheduled_at


@dataclass(frozen=True, slots=True)
class TravelerRecord:
    id: int
    user_id: int
    label: str
    identity: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class MemberProfileRecord:
    user_id: int
    identity: str
    member_account: str
    member_password: str
    updated_at: str


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
                    created_at TEXT NOT NULL,
                    token_version INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            user_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "token_version" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0"
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
                    last_error TEXT,
                    booking_code TEXT
                )
                """
            )
            task_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "booking_code" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN booking_code TEXT")
            # Monitoring columns. `scheduled_at` keeps its original meaning as
            # the start time, so existing one-shot tasks stay correct: they get
            # a NULL monitor_until and simply stop after their first check.
            for column, ddl in (
                ("mode", f"TEXT NOT NULL DEFAULT '{MODE_BOOK_WHEN_AVAILABLE}'"),
                (
                    "poll_interval_seconds",
                    f"INTEGER NOT NULL DEFAULT {DEFAULT_POLL_INTERVAL_SECONDS}",
                ),
                ("monitor_until", "TEXT"),
                ("last_checked_at", "TEXT"),
                ("next_check_at", "TEXT"),
                ("check_failures", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in task_columns:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {column} {ddl}")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_next_check "
                "ON tasks(next_check_at) WHERE status IN ('scheduled', 'monitoring')"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_user_created "
                "ON tasks(user_id, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_due "
                "ON tasks(status, scheduled_at) WHERE status = 'scheduled'"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS login_attempts (
                    email TEXT NOT NULL COLLATE NOCASE,
                    attempted_at TEXT NOT NULL,
                    succeeded INTEGER NOT NULL CHECK (succeeded IN (0, 1))
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_login_attempts "
                "ON login_attempts(email, attempted_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS member_profiles (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    payload BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # One member account per user (member_profiles, unchanged) but many
            # named identities to book for. Detect first creation so the
            # migration below runs exactly once, and never resurrects rows the
            # user has deliberately deleted.
            travelers_existed = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'travelers'"
                ).fetchone()
                is not None
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS travelers (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    label TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_travelers_user_label "
                "ON travelers(user_id, label)"
            )
            if not travelers_existed:
                self._seed_travelers_from_profiles(connection)
            connection.execute("PRAGMA optimize")

    def _seed_travelers_from_profiles(self, connection: sqlite3.Connection) -> None:
        """Carry each existing member profile's identity into the new table.

        Runs once, when `travelers` is first created. Without it, upgrading
        would silently drop the identity users already saved.
        """
        rows = connection.execute(
            "SELECT user_id, payload, updated_at FROM member_profiles"
        ).fetchall()
        for row in rows:
            try:
                identity = str(self.cipher.decrypt(row["payload"]).get("identity", "")).strip()
            except ValueError:
                continue
            if not identity:
                continue
            connection.execute(
                """
                INSERT INTO travelers(user_id, label, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row["user_id"],
                    "預設",
                    self.cipher.encrypt({"identity": identity}),
                    row["updated_at"],
                    row["updated_at"],
                ),
            )

    def list_travelers(self, user_id: int) -> list[TravelerRecord]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, user_id, label, payload, updated_at FROM travelers "
                "WHERE user_id = ? ORDER BY label",
                (user_id,),
            ).fetchall()
        return [self._traveler(row) for row in rows]

    def get_traveler(self, traveler_id: int, user_id: int) -> TravelerRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, user_id, label, payload, updated_at FROM travelers "
                "WHERE id = ? AND user_id = ?",
                (traveler_id, user_id),
            ).fetchone()
        return self._traveler(row) if row else None

    def _traveler(self, row: sqlite3.Row) -> TravelerRecord:
        payload = self.cipher.decrypt(row["payload"])
        return TravelerRecord(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            label=str(row["label"]),
            identity=str(payload.get("identity", "")),
            updated_at=str(row["updated_at"]),
        )

    def create_traveler(self, user_id: int, *, label: str, identity: str) -> TravelerRecord:
        now = utc_now()
        try:
            with self.connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO travelers(user_id, label, payload, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        label.strip(),
                        self.cipher.encrypt({"identity": identity.strip()}),
                        now,
                        now,
                    ),
                )
                traveler_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValueError("已經有同名的常用資料") from exc
        traveler = self.get_traveler(traveler_id, user_id)
        if traveler is None:
            raise RuntimeError("traveler was not persisted")
        return traveler

    def update_traveler(
        self, traveler_id: int, user_id: int, *, label: str, identity: str
    ) -> TravelerRecord | None:
        try:
            with self.connect() as connection:
                cursor = connection.execute(
                    "UPDATE travelers SET label = ?, payload = ?, updated_at = ? "
                    "WHERE id = ? AND user_id = ?",
                    (
                        label.strip(),
                        self.cipher.encrypt({"identity": identity.strip()}),
                        utc_now(),
                        traveler_id,
                        user_id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("已經有同名的常用資料") from exc
        if cursor.rowcount != 1:
            return None
        return self.get_traveler(traveler_id, user_id)

    def delete_traveler(self, traveler_id: int, user_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM travelers WHERE id = ? AND user_id = ?", (traveler_id, user_id)
            )
        return cursor.rowcount == 1

    def get_member_profile(self, user_id: int) -> MemberProfileRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT user_id, payload, updated_at FROM member_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        profile = self.cipher.decrypt(row["payload"])
        return MemberProfileRecord(
            user_id=int(row["user_id"]),
            identity=str(profile.get("identity", "")),
            member_account=str(profile.get("member_account", "")),
            member_password=str(profile.get("member_password", "")),
            updated_at=str(row["updated_at"]),
        )

    def save_member_profile(
        self,
        user_id: int,
        *,
        identity: str,
        member_account: str,
        member_password: str,
    ) -> MemberProfileRecord:
        now = utc_now()
        payload = self.cipher.encrypt(
            {
                "identity": identity.strip(),
                "member_account": member_account.strip(),
                "member_password": member_password,
            }
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO member_profiles(user_id, payload, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET payload = excluded.payload,
                                                   updated_at = excluded.updated_at
                """,
                (user_id, payload, now),
            )
        profile = self.get_member_profile(user_id)
        if profile is None:
            raise RuntimeError("member profile was not persisted")
        return profile

    def delete_member_profile(self, user_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM member_profiles WHERE user_id = ?", (user_id,)
            )
        return cursor.rowcount == 1

    def clear_member_login(self, user_id: int) -> MemberProfileRecord | None:
        profile = self.get_member_profile(user_id)
        if not profile:
            return None
        return self.save_member_profile(
            user_id,
            identity=profile.identity,
            member_account="",
            member_password="",
        )

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
        return UserRecord(user_id, email.strip().lower(), password_hash, created_at, 0)

    def get_user_by_email(self, email: str) -> UserRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, email, password_hash, created_at, token_version "
                "FROM users WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
        return UserRecord(**dict(row)) if row else None

    def get_user(self, user_id: int) -> UserRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, email, password_hash, created_at, token_version "
                "FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return UserRecord(**dict(row)) if row else None

    def revoke_user_tokens(self, user_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET token_version = token_version + 1 WHERE id = ?",
                (user_id,),
            )
        return cursor.rowcount == 1

    def is_login_locked(
        self,
        email: str,
        *,
        now: datetime | None = None,
        max_failures: int = 5,
        window_minutes: int = 15,
    ) -> bool:
        current = now or datetime.now(UTC)
        cutoff = (current - timedelta(minutes=window_minutes)).isoformat()
        with self.connect() as connection:
            count = connection.execute(
                """
                SELECT COUNT(*) FROM login_attempts
                WHERE email = ? AND succeeded = 0 AND attempted_at >= ?
                """,
                (email.strip().lower(), cutoff),
            ).fetchone()[0]
        return int(count) >= max_failures

    def record_login_attempt(
        self,
        email: str,
        *,
        succeeded: bool,
        attempted_at: datetime | None = None,
    ) -> None:
        normalized = email.strip().lower()
        timestamp = (attempted_at or datetime.now(UTC)).isoformat()
        with self.connect() as connection:
            if succeeded:
                connection.execute("DELETE FROM login_attempts WHERE email = ?", (normalized,))
            else:
                connection.execute(
                    "INSERT INTO login_attempts(email, attempted_at, succeeded) VALUES (?, ?, 0)",
                    (normalized, timestamp),
                )
                cleanup_before = (datetime.now(UTC) - timedelta(days=1)).isoformat()
                connection.execute(
                    "DELETE FROM login_attempts WHERE attempted_at < ?",
                    (cleanup_before,),
                )

    def create_task(
        self,
        user_id: int,
        request: BookingRequest,
        scheduled_at: str,
        payload: dict[str, Any],
        *,
        mode: str = MODE_BOOK_WHEN_AVAILABLE,
        poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
        monitor_until: str | None = None,
    ) -> TaskRecord:
        task_id = str(uuid.uuid4())
        now = utc_now()
        route = f"{request.start_station} → {request.end_station}"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks(
                    id, user_id, status, scheduled_at, payload, route, ride_date,
                    order_type, created_at, updated_at,
                    mode, poll_interval_seconds, monitor_until, next_check_at
                ) VALUES (?, ?, 'scheduled', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    mode,
                    max(int(poll_interval_seconds), MIN_POLL_INTERVAL_SECONDS),
                    monitor_until,
                    # The first check is due when monitoring starts, not now.
                    scheduled_at if monitor_until else None,
                ),
            )
        task = self.get_task(task_id, user_id)
        if task is None:
            raise RuntimeError("task was not persisted")
        return task

    def list_tasks(self, user_id: int) -> list[TaskRecord]:
        with self.connect() as connection:
            rows = connection.execute(
                                f"SELECT {TASK_COLUMNS} FROM tasks "
                "WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [TaskRecord(**dict(row)) for row in rows]

    def get_task(self, task_id: str, user_id: int) -> TaskRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                                f"SELECT {TASK_COLUMNS} FROM tasks WHERE id = ? AND user_id = ?",
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
        booking_code: str | None = None,
    ) -> bool:
        # COALESCE keeps a code that was already recorded: a later status update
        # must never erase the one output the whole booking flow exists to produce.
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks SET status = ?, updated_at = ?, last_error = ?,
                                 booking_code = COALESCE(?, booking_code)
                WHERE id = ? AND user_id = ?
                """,
                (status, utc_now(), last_error, booking_code, task_id, user_id),
            )
        return cursor.rowcount == 1

    def claim_due_checks(self, now: str | None = None, *, limit: int = 20) -> list[TaskRecord]:
        """Take ownership of every task due for a check, atomically.

        The same statement that selects a task also pushes `next_check_at`
        forward, so a second caller arriving concurrently matches nothing. That
        is what stops a task from being checked twice at once, and it holds
        across a restart because the guard lives in the row, not in memory.
        """
        moment = now or utc_now()
        # Timestamps are computed here rather than with SQLite's datetime(),
        # which returns "YYYY-MM-DD HH:MM:SS" with no offset. Mixing that with
        # the ISO-8601 strings the rest of the schema stores makes the string
        # comparisons in the WHERE clause silently wrong.
        base = datetime.fromisoformat(moment)
        claimed: list[TaskRecord] = []
        with self.connect() as connection:
            candidates = connection.execute(
                f"""
                SELECT {TASK_COLUMNS} FROM tasks
                WHERE status IN ('scheduled', 'monitoring')
                  AND scheduled_at <= ?
                  AND (next_check_at IS NULL OR next_check_at <= ?)
                  AND monitor_until IS NOT NULL
                  AND monitor_until > ?
                ORDER BY next_check_at
                LIMIT ?
                """,
                (moment, moment, moment, limit),
            ).fetchall()
            for row in candidates:
                interval = int(row["poll_interval_seconds"])
                following = (base + timedelta(seconds=interval)).isoformat()
                # Compare-and-swap: the UPDATE repeats the claim condition, so a
                # caller that lost the race updates nothing and skips the task.
                cursor = connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'monitoring', last_checked_at = ?, next_check_at = ?,
                        updated_at = ?
                    WHERE id = ? AND status IN ('scheduled', 'monitoring')
                      AND (next_check_at IS NULL OR next_check_at <= ?)
                    """,
                    (moment, following, moment, row["id"], moment),
                )
                if cursor.rowcount == 1:
                    data = dict(row)
                    data.update(
                        status="monitoring", last_checked_at=moment, next_check_at=following
                    )
                    claimed.append(TaskRecord(**data))
        return claimed

    def record_check_failure(self, task_id: str, user_id: int, error: str) -> int:
        """Back off exponentially so a broken upstream is not hammered."""
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT check_failures, poll_interval_seconds FROM tasks "
                "WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            ).fetchone()
            if row is None:
                return 0
            failures = min(int(row["check_failures"]) + 1, MAX_BACKOFF_MULTIPLIER)
            delay = int(row["poll_interval_seconds"]) * (2**failures)
            connection.execute(
                "UPDATE tasks SET check_failures = ?, last_error = ?, next_check_at = ?, "
                "updated_at = ? WHERE id = ? AND user_id = ?",
                (
                    failures,
                    error[:500],
                    (datetime.fromisoformat(now) + timedelta(seconds=delay)).isoformat(),
                    now,
                    task_id,
                    user_id,
                ),
            )
        return failures

    def clear_check_failures(self, task_id: str, user_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE tasks SET check_failures = 0 WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            )

    def expire_finished_monitors(self, now: str | None = None) -> list[TaskRecord]:
        """Stop tasks whose monitor window has closed."""
        moment = now or utc_now()
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                UPDATE tasks SET status = 'expired', updated_at = ?
                WHERE status IN ('scheduled', 'monitoring')
                  AND monitor_until IS NOT NULL AND monitor_until <= ?
                RETURNING {TASK_COLUMNS}
                """,
                (moment, moment),
            ).fetchall()
        return [TaskRecord(**dict(row)) for row in rows]

    def pause_monitoring(self, task_id: str, user_id: int, status: str) -> bool:
        """Move a task out of the pollable states so no check can claim it."""
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ? AND status IN ('scheduled', 'monitoring')",
                (status, utc_now(), task_id, user_id),
            )
        return cursor.rowcount == 1

    def resume_monitoring(self, task_id: str, user_id: int) -> bool:
        """Put a task back in the poll loop after an abandoned attempt."""
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status = 'monitoring', next_check_at = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ? AND monitor_until > ?",
                (utc_now(), utc_now(), task_id, user_id, utc_now()),
            )
        return cursor.rowcount == 1

    def promote_due_tasks(self, now: str | None = None) -> int:
        return len(self.promote_due_task_records(now))

    def promote_due_task_records(self, now: str | None = None) -> list[TaskRecord]:
        """One-shot promotion, kept for tasks created before monitoring existed."""
        updated_at = utc_now()
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                UPDATE tasks SET status = 'waiting_human', updated_at = ?
                WHERE status = 'scheduled' AND scheduled_at <= ?
                      AND next_check_at IS NULL AND monitor_until IS NULL
                RETURNING {TASK_COLUMNS}
                """,
                (updated_at, now or updated_at),
            ).fetchall()
        return [TaskRecord(**dict(row)) for row in rows]
