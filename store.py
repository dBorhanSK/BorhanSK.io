from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class StoreError(RuntimeError):
    pass


class Store:
    """Supabase-first persistence with SQLite for local development only."""

    def __init__(self, data_dir: str | Path):
        self._lock = threading.RLock()
        self._client = None
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        required = os.getenv("REQUIRE_SUPABASE", "false").strip().lower() in {"1", "true", "yes", "on"}
        if bool(url) != bool(key):
            raise StoreError("برای Supabase هر دو متغیر SUPABASE_URL و SUPABASE_SERVICE_ROLE_KEY لازم‌اند")
        if url and key:
            try:
                from supabase import create_client
                self._client = create_client(url, key)
                self.backend = "supabase"
            except Exception as exc:
                raise StoreError(f"Supabase initialization failed: {exc}") from exc
        elif required:
            raise StoreError("این سرویس به Supabase نیاز دارد؛ متغیرهای اتصال را تنظیم کن")
        else:
            base = Path(data_dir)
            base.mkdir(parents=True, exist_ok=True)
            self._db_path = base / "borhan-sk-local.sqlite3"
            self.backend = "sqlite-local"
            self._init_sqlite()

    def _connect(self):
        con = sqlite3.connect(self._db_path, timeout=20, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _init_sqlite(self):
        with self._connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, target TEXT NOT NULL,
                action TEXT NOT NULL, payload TEXT NOT NULL,
                interval_seconds INTEGER NOT NULL, status TEXT NOT NULL,
                next_run_at TEXT NOT NULL, last_run_at TEXT,
                runs_count INTEGER NOT NULL DEFAULT 0, max_runs INTEGER,
                last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                owner_id TEXT NOT NULL DEFAULT 'owner', account_id TEXT
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT NOT NULL,
                message TEXT NOT NULL, created_at TEXT NOT NULL,
                owner_id TEXT NOT NULL DEFAULT 'owner'
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS runtime_state (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS members (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, created_at TEXT NOT NULL
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                platform TEXT NOT NULL, label TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', owner_access INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            # Migrate existing local databases without deleting existing jobs or sessions.
            job_cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)").fetchall()}
            if "owner_id" not in job_cols:
                con.execute("ALTER TABLE jobs ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'owner'")
            if "account_id" not in job_cols:
                con.execute("ALTER TABLE jobs ADD COLUMN account_id TEXT")
            event_cols = {r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()}
            if "owner_id" not in event_cols:
                con.execute("ALTER TABLE events ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'owner'")
            account_cols = {r[1] for r in con.execute("PRAGMA table_info(accounts)").fetchall()}
            if "owner_access" not in account_cols:
                con.execute("ALTER TABLE accounts ADD COLUMN owner_access INTEGER NOT NULL DEFAULT 0")
            con.execute("CREATE INDEX IF NOT EXISTS jobs_owner_idx ON jobs(owner_id, created_at)")
            con.execute("CREATE INDEX IF NOT EXISTS accounts_owner_idx ON accounts(owner_id, created_at)")
            con.commit()

    # ── Members ──────────────────────────────────────────────────────────────
    def create_member(self, username: str, password_hash: str) -> dict[str, Any]:
        username = username.strip().lower()
        row = {"id": str(uuid.uuid4()), "username": username,
               "password_hash": password_hash, "created_at": iso(utcnow())}
        if self._client:
            data = self._client.table("meow_members").insert(row).execute().data
            if not data:
                raise StoreError("عضو جدید ذخیره نشد")
            result = dict(data[0])
        else:
            try:
                with self._lock, self._connect() as con:
                    con.execute("INSERT INTO members(id, username, password_hash, created_at) VALUES(?,?,?,?)",
                                (row["id"], row["username"], row["password_hash"], row["created_at"]))
                    con.commit()
            except sqlite3.IntegrityError as exc:
                raise StoreError("این نام کاربری قبلاً ثبت شده است") from exc
            result = row
        result.pop("password_hash", None)
        return result

    def get_member(self, username: str) -> dict[str, Any] | None:
        if self._client:
            data = self._client.table("meow_members").select("*").eq("username", username.lower()).limit(1).execute().data
            return dict(data[0]) if data else None
        with self._lock, self._connect() as con:
            row = con.execute("SELECT * FROM members WHERE username=?", (username.lower(),)).fetchone()
            return dict(row) if row else None

    def list_members(self) -> list[dict[str, Any]]:
        if self._client:
            rows = self._client.table("meow_members").select("id,username,created_at").order("created_at", desc=True).execute().data or []
            return [dict(row) for row in rows]
        with self._lock, self._connect() as con:
            return [dict(r) for r in con.execute("SELECT id,username,created_at FROM members ORDER BY created_at DESC").fetchall()]

    def delete_member(self, member_id: str) -> bool:
        if self._client:
            data = self._client.table("meow_members").delete().eq("id", member_id).select("id").execute().data
            return bool(data)
        with self._lock, self._connect() as con:
            con.execute("DELETE FROM accounts WHERE owner_id=?", (member_id,))
            con.execute("DELETE FROM jobs WHERE owner_id=?", (member_id,))
            cur = con.execute("DELETE FROM members WHERE id=?", (member_id,))
            con.commit()
            return cur.rowcount > 0

    # ── Connected accounts ───────────────────────────────────────────────────
    def create_account(self, owner_id: str, platform: str, label: str) -> dict[str, Any]:
        now = iso(utcnow())
        row = {"id": str(uuid.uuid4()), "owner_id": owner_id, "platform": platform,
               "label": label.strip(), "status": "pending", "owner_access": False,
               "created_at": now, "updated_at": now}
        if self._client:
            data = self._client.table("meow_accounts").insert(row).execute().data
            if not data:
                raise StoreError("حساب جدید ذخیره نشد")
            return dict(data[0])
        with self._lock, self._connect() as con:
            con.execute("INSERT INTO accounts(id,owner_id,platform,label,status,owner_access,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (row["id"], row["owner_id"], row["platform"], row["label"], row["status"],
                         0, row["created_at"], row["updated_at"]))
            con.commit()
        return row

    def list_accounts(self, owner_id: str | None = None) -> list[dict[str, Any]]:
        if self._client:
            query = self._client.table("meow_accounts").select("*").order("created_at", desc=True)
            if owner_id is not None:
                query = query.eq("owner_id", owner_id)
            return [dict(r) for r in (query.execute().data or [])]
        sql, params = "SELECT * FROM accounts", []
        if owner_id is not None:
            sql += " WHERE owner_id=?"
            params.append(owner_id)
        sql += " ORDER BY created_at DESC"
        with self._lock, self._connect() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        if self._client:
            data = self._client.table("meow_accounts").select("*").eq("id", account_id).limit(1).execute().data
            return dict(data[0]) if data else None
        with self._lock, self._connect() as con:
            row = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            return dict(row) if row else None

    def update_account(self, account_id: str, values: dict[str, Any]) -> bool:
        allowed = {k: values[k] for k in ("label", "status", "owner_access") if k in values}
        allowed["updated_at"] = iso(utcnow())
        if self._client:
            data = self._client.table("meow_accounts").update(allowed).eq("id", account_id).select("id").execute().data
            return bool(data)
        with self._lock, self._connect() as con:
            sets = ",".join(f"{k}=?" for k in allowed)
            cur = con.execute(f"UPDATE accounts SET {sets} WHERE id=?", (*allowed.values(), account_id))
            con.commit()
            return cur.rowcount > 0

    def delete_account(self, account_id: str) -> bool:
        if self._client:
            # Supabase does not rely on an account_id FK cascade for legacy schemas.
            # Remove linked jobs first so admin deletion cannot leave owner-created
            # delegated schedules orphaned with their targets and payloads.
            self._client.table("meow_jobs").delete().eq("account_id", account_id).execute()
            data = self._client.table("meow_accounts").delete().eq("id", account_id).select("id").execute().data
            return bool(data)
        with self._lock, self._connect() as con:
            con.execute("DELETE FROM jobs WHERE account_id=?", (account_id,))
            cur = con.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            con.commit()
            return cur.rowcount > 0

    # ── Jobs ─────────────────────────────────────────────────────────────────
    def create_job(self, *, name: str, target: str, action: str, payload: str,
                   interval_seconds: int, max_runs: int | None = None,
                   owner_id: str = "owner", account_id: str | None = None) -> dict[str, Any]:
        now = utcnow()
        row = {"id": str(uuid.uuid4()), "name": name.strip(), "target": target.strip(),
               "action": action, "payload": payload, "interval_seconds": int(interval_seconds),
               "status": "active", "next_run_at": iso(now + timedelta(seconds=interval_seconds)),
               "last_run_at": None, "runs_count": 0, "max_runs": max_runs,
               "last_error": None, "created_at": iso(now), "updated_at": iso(now),
               "owner_id": owner_id, "account_id": account_id}
        if self._client:
            data = self._client.table("meow_jobs").insert(row).execute().data
            if not data:
                raise StoreError("Supabase did not return the inserted job")
            return dict(data[0])
        fields = tuple(row.keys())
        with self._lock, self._connect() as con:
            con.execute(f"INSERT INTO jobs({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                        tuple(row[k] for k in fields))
            con.commit()
        return row

    def list_jobs(self, active_only: bool = False, owner_id: str | None = None) -> list[dict[str, Any]]:
        if self._client:
            query = self._client.table("meow_jobs").select("*").order("created_at", desc=True)
            if active_only:
                query = query.eq("status", "active")
            if owner_id is not None:
                query = query.eq("owner_id", owner_id)
            return [dict(r) for r in (query.execute().data or [])]
        clauses, params = [], []
        if active_only:
            clauses.append("status='active'")
        if owner_id is not None:
            clauses.append("owner_id=?")
            params.append(owner_id)
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at DESC"
        with self._lock, self._connect() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

    def get_job(self, job_id: str, owner_id: str | None = None) -> dict[str, Any] | None:
        if self._client:
            query = self._client.table("meow_jobs").select("*").eq("id", job_id)
            if owner_id is not None:
                query = query.eq("owner_id", owner_id)
            data = query.limit(1).execute().data
            return dict(data[0]) if data else None
        sql, params = "SELECT * FROM jobs WHERE id=?", [job_id]
        if owner_id is not None:
            sql += " AND owner_id=?"
            params.append(owner_id)
        with self._lock, self._connect() as con:
            row = con.execute(sql, params).fetchone()
            return dict(row) if row else None

    def set_job_status(self, job_id: str, status: str, owner_id: str | None = None) -> bool:
        if status not in {"active", "paused"}:
            raise ValueError("Invalid status")
        values = {"status": status, "updated_at": iso(utcnow())}
        if status == "active":
            row = self.get_job(job_id, owner_id)
            if not row:
                return False
            due = parse_dt(row.get("next_run_at"))
            if due and due < utcnow():
                values["next_run_at"] = iso(utcnow() + timedelta(seconds=int(row["interval_seconds"])))
        if self._client:
            query = self._client.table("meow_jobs").update(values).eq("id", job_id)
            if owner_id is not None:
                query = query.eq("owner_id", owner_id)
            return bool(query.select("id").execute().data)
        sets = ",".join(f"{k}=?" for k in values)
        sql, params = f"UPDATE jobs SET {sets} WHERE id=?", [*values.values(), job_id]
        if owner_id is not None:
            sql += " AND owner_id=?"
            params.append(owner_id)
        with self._lock, self._connect() as con:
            cur = con.execute(sql, params)
            con.commit()
            return cur.rowcount > 0

    def delete_job(self, job_id: str, owner_id: str | None = None) -> bool:
        if self._client:
            query = self._client.table("meow_jobs").delete().eq("id", job_id)
            if owner_id is not None:
                query = query.eq("owner_id", owner_id)
            return bool(query.select("id").execute().data)
        sql, params = "DELETE FROM jobs WHERE id=?", [job_id]
        if owner_id is not None:
            sql += " AND owner_id=?"
            params.append(owner_id)
        with self._lock, self._connect() as con:
            cur = con.execute(sql, params)
            con.commit()
            return cur.rowcount > 0

    def claim_due_job(self, job: dict[str, Any]) -> dict[str, Any] | None:
        now = utcnow()
        next_runs = int(job.get("runs_count") or 0) + 1
        max_runs = job.get("max_runs")
        status = "paused" if max_runs and next_runs >= int(max_runs) else "active"
        values = {"last_run_at": iso(now), "next_run_at": iso(now + timedelta(seconds=int(job["interval_seconds"]))),
                  "runs_count": next_runs, "status": status, "updated_at": iso(now), "last_error": None}
        if self._client:
            query = self._client.table("meow_jobs").update(values).eq("id", job["id"]).eq("status", "active")
            if job.get("owner_id") is not None:
                query = query.eq("owner_id", job["owner_id"])
            data = query.select("*").execute().data
            return dict(data[0]) if data else None
        with self._lock, self._connect() as con:
            cur = con.execute("""UPDATE jobs SET last_run_at=?, next_run_at=?, runs_count=?, status=?,
                updated_at=?, last_error=NULL WHERE id=? AND status='active' AND owner_id=?""",
                (values["last_run_at"], values["next_run_at"], values["runs_count"], values["status"],
                 values["updated_at"], job["id"], job.get("owner_id", "owner")))
            con.commit()
            return {**job, **values} if cur.rowcount else None

    def set_job_error(self, job_id: str, error: str):
        values = {"last_error": str(error)[:1000], "updated_at": iso(utcnow())}
        if self._client:
            self._client.table("meow_jobs").update(values).eq("id", job_id).execute()
            return
        with self._lock, self._connect() as con:
            con.execute("UPDATE jobs SET last_error=?, updated_at=? WHERE id=?", (values["last_error"], values["updated_at"], job_id))
            con.commit()

    # ── Runtime state and event log ──────────────────────────────────────────
    def get_runtime_state(self, key: str) -> str | None:
        if self._client:
            data = self._client.table("meow_runtime_state").select("value").eq("key", key).limit(1).execute().data
            return str(data[0]["value"]) if data else None
        with self._lock, self._connect() as con:
            row = con.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else None

    def set_runtime_state(self, key: str, value: str):
        now = iso(utcnow())
        if self._client:
            self._client.table("meow_runtime_state").upsert({"key": key, "value": value, "updated_at": now}, on_conflict="key").execute()
            return
        with self._lock, self._connect() as con:
            con.execute("INSERT INTO runtime_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                        (key, value, now))
            con.commit()

    def delete_runtime_state(self, key: str):
        if self._client:
            self._client.table("meow_runtime_state").delete().eq("key", key).execute()
            return
        with self._lock, self._connect() as con:
            con.execute("DELETE FROM runtime_state WHERE key=?", (key,))
            con.commit()

    def add_event(self, level: str, message: str, owner_id: str = "owner"):
        row = {"level": str(level)[:20], "message": str(message)[:1500],
               "created_at": iso(utcnow()), "owner_id": owner_id}
        if self._client:
            self._client.table("meow_events").insert(row).execute()
            return
        with self._lock, self._connect() as con:
            con.execute("INSERT INTO events(level,message,created_at,owner_id) VALUES(?,?,?,?)",
                        (row["level"], row["message"], row["created_at"], row["owner_id"]))
            con.commit()

    def list_events(self, limit: int = 60, owner_id: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        if self._client:
            query = self._client.table("meow_events").select("*").order("created_at", desc=True).limit(limit)
            if owner_id is not None:
                query = query.eq("owner_id", owner_id)
            return [dict(r) for r in (query.execute().data or [])]
        sql, params = "SELECT * FROM events", []
        if owner_id is not None:
            sql += " WHERE owner_id=?"
            params.append(owner_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
