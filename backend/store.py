"""Storage layer: Supabase (production) or SQLite (local dev).

Table names and key columns match the existing Supabase schema
(meow_members / meow_accounts / meow_jobs / meow_events / meow_runtime_state)
so your current data keeps working after you re-run supabase_schema.sql.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

OWNER_ID = "owner"
_IDENT = re.compile(r"^[a-z_]+$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value) -> datetime | None:
    if value is None:
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class StoreError(RuntimeError):
    pass


def session_key(account_id: str) -> str:
    """Legacy key name used only to remove incompatible pre-Desktop sessions."""
    return f"tg_session:{account_id}"


_SQLITE = [
    """create table if not exists meow_members (
        id text primary key, username text not null unique, password_hash text not null,
        active integer not null default 1, created_at text not null, last_login_at text)""",
    """create table if not exists meow_accounts (
        id text primary key, owner_id text not null, platform text not null default 'telegram',
        label text not null, status text not null default 'pending', owner_access integer not null default 0,
        tg_user_id text, tg_name text, tg_username text, tg_phone text, last_error text,
        last_seen_at text, created_at text not null, updated_at text not null)""",
    """create table if not exists meow_jobs (
        id text primary key, name text not null, target text not null, action text not null,
        payload text not null, interval_seconds integer not null, status text not null default 'active',
        next_run_at text not null, last_run_at text, runs_count integer not null default 0, max_runs integer,
        last_error text, created_at text not null, updated_at text not null,
        owner_id text not null default 'owner', account_id text,
        mode text not null default 'random', cursor integer not null default 0,
        active_from integer, active_to integer)""",
    """create table if not exists meow_events (
        id integer primary key autoincrement, level text not null default 'info', message text not null,
        created_at text not null, owner_id text not null default 'owner')""",
    """create table if not exists meow_runtime_state (
        key text primary key, value text not null, updated_at text not null)""",
]


class Store:
    def __init__(self, data_dir: str | Path):
        self._lock = threading.RLock()
        self._client = None
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        require = os.getenv("REQUIRE_SUPABASE", "false").strip().lower() in {"1", "true", "yes", "on"}
        if bool(url) != bool(key):
            raise StoreError("هر دو مقدار SUPABASE_URL و SUPABASE_SERVICE_ROLE_KEY باید تنظیم شوند")
        if url and key:
            from supabase import create_client
            self._client = create_client(url, key)
            self.backend = "supabase"
        elif require:
            raise StoreError("Supabase الزامی است ولی SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY تنظیم نشده")
        else:
            base = Path(data_dir)
            base.mkdir(parents=True, exist_ok=True)
            self._db_path = base / "borhan-local.sqlite3"
            self.backend = "sqlite-local"
            with self._lock, self._connect() as con:
                for stmt in _SQLITE:
                    con.execute(stmt)
                con.commit()

    # ---------------------------------------------------------------- primitives
    def _connect(self):
        con = sqlite3.connect(self._db_path, timeout=20, check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _check(*names: str):
        for n in names:
            if not _IDENT.match(n):
                raise StoreError(f"invalid identifier: {n}")

    @staticmethod
    def _where(where):
        clauses, params = [], []
        for k, v in (where or {}).items():
            Store._check(k)
            if v is None:
                clauses.append(f"{k} is null")
            else:
                clauses.append(f"{k}=?")
                params.append(v)
        return (" where " + " and ".join(clauses)) if clauses else "", params

    def _insert(self, table, row):
        self._check(table, *row)
        if self._client:
            data = self._client.table(table).insert(row).execute().data
            if not data:
                raise StoreError("insert returned no data")
            return dict(data[0])
        with self._lock, self._connect() as con:
            con.execute(f"insert into {table} ({','.join(row)}) values ({','.join('?' for _ in row)})",
                        tuple(row.values()))
            con.commit()
        return dict(row)

    def _select(self, table, where=None, order=None, desc=False, limit=None):
        self._check(table)
        if self._client:
            q = self._client.table(table).select("*")
            for k, v in (where or {}).items():
                q = q.is_(k, "null") if v is None else q.eq(k, v)
            if order:
                q = q.order(order, desc=desc)
            if limit:
                q = q.limit(limit)
            return [dict(r) for r in (q.execute().data or [])]
        wsql, params = self._where(where)
        sql = f"select * from {table}{wsql}"
        if order:
            self._check(order)
            sql += f" order by {order} {'desc' if desc else 'asc'}"
        if limit:
            sql += f" limit {int(limit)}"
        with self._lock, self._connect() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

    def _update(self, table, where, values) -> int:
        self._check(table, *values)
        if not values:
            return 0
        if self._client:
            q = self._client.table(table).update(values)
            for k, v in where.items():
                q = q.is_(k, "null") if v is None else q.eq(k, v)
            return len(q.execute().data or [])
        wsql, wparams = self._where(where)
        sets = ",".join(f"{k}=?" for k in values)
        with self._lock, self._connect() as con:
            cur = con.execute(f"update {table} set {sets}{wsql}", list(values.values()) + wparams)
            con.commit()
            return cur.rowcount

    def _delete(self, table, where) -> int:
        self._check(table)
        if self._client:
            q = self._client.table(table).delete()
            for k, v in where.items():
                q = q.is_(k, "null") if v is None else q.eq(k, v)
            return len(q.execute().data or [])
        wsql, wparams = self._where(where)
        with self._lock, self._connect() as con:
            cur = con.execute(f"delete from {table}{wsql}", wparams)
            con.commit()
            return cur.rowcount

    def _upsert(self, table, row, key):
        self._check(table, key, *row)
        if self._client:
            self._client.table(table).upsert(row, on_conflict=key).execute()
            return
        sets = ",".join(f"{c}=excluded.{c}" for c in row if c != key)
        with self._lock, self._connect() as con:
            con.execute(f"insert into {table} ({','.join(row)}) values ({','.join('?' for _ in row)}) "
                        f"on conflict({key}) do update set {sets}", tuple(row.values()))
            con.commit()

    # ---------------------------------------------------------------- members (admins)
    @staticmethod
    def _member_out(row, with_hash=False):
        if not row:
            return None
        out = dict(row)
        out["active"] = bool(out.get("active", True)) if out.get("active") is not None else True
        if not with_hash:
            out.pop("password_hash", None)
        return out

    def create_member(self, username: str, password_hash: str) -> dict[str, Any]:
        username = username.strip().lower()
        if self.get_member(username):
            raise StoreError("این نام کاربری قبلاً ثبت شده است")
        row = {"id": str(uuid.uuid4()), "username": username, "password_hash": password_hash,
               "created_at": iso(utcnow())}
        if not self._client:
            row["active"] = 1
        return self._member_out(self._insert("meow_members", row))  # type: ignore[return-value]

    def get_member(self, username: str, with_hash=False):
        rows = self._select("meow_members", {"username": username.strip().lower()}, limit=1)
        return self._member_out(rows[0], with_hash) if rows else None

    def get_member_by_id(self, member_id: str, with_hash=False):
        rows = self._select("meow_members", {"id": member_id}, limit=1)
        return self._member_out(rows[0], with_hash) if rows else None

    def list_members(self):
        return [self._member_out(r) for r in self._select("meow_members", order="created_at", desc=True)]

    def update_member(self, member_id: str, **values) -> bool:
        if "active" in values and not self._client:
            values["active"] = int(bool(values["active"]))
        return self._update("meow_members", {"id": member_id}, values) > 0

    def touch_member_login(self, member_id: str):
        try:
            self._update("meow_members", {"id": member_id}, {"last_login_at": iso(utcnow())})
        except Exception:
            pass

    def delete_member(self, member_id: str) -> bool:
        return self._delete("meow_members", {"id": member_id}) > 0

    # ---------------------------------------------------------------- accounts
    @staticmethod
    def _account_out(row):
        out = dict(row)
        out["owner_access"] = bool(out.get("owner_access"))
        return out

    def create_account(self, owner_id: str, label: str, platform: str = "telegram") -> dict[str, Any]:
        now = iso(utcnow())
        row = {"id": str(uuid.uuid4()), "owner_id": owner_id, "platform": platform, "label": label.strip()[:80],
               "status": "pending", "owner_access": False if self._client else 0,
               "created_at": now, "updated_at": now}
        return self._account_out(self._insert("meow_accounts", row))

    def list_accounts(self, owner_id: str | None = None):
        where = {"owner_id": owner_id} if owner_id is not None else None
        return [self._account_out(r) for r in self._select("meow_accounts", where, order="created_at")]

    def get_account(self, account_id: str):
        rows = self._select("meow_accounts", {"id": account_id}, limit=1)
        return self._account_out(rows[0]) if rows else None

    def update_account(self, account_id: str, **values) -> bool:
        if "owner_access" in values and not self._client:
            values["owner_access"] = int(bool(values["owner_access"]))
        values["updated_at"] = iso(utcnow())
        return self._update("meow_accounts", {"id": account_id}, values) > 0

    def delete_account(self, account_id: str) -> bool:
        self._delete("meow_jobs", {"account_id": account_id})
        self._delete("meow_runtime_state", {"key": session_key(account_id)})
        return self._delete("meow_accounts", {"id": account_id}) > 0

    # ---------------------------------------------------------------- jobs (Self module)
    def create_job(self, *, name, target, action, payload, interval_seconds, owner_id=OWNER_ID, account_id=None,
                   max_runs=None, mode="random", active_from=None, active_to=None):
        now = utcnow()
        row = {"id": str(uuid.uuid4()), "name": name.strip(), "target": target.strip(), "action": action,
               "payload": payload, "interval_seconds": int(interval_seconds), "status": "active",
               "next_run_at": iso(now + timedelta(seconds=int(interval_seconds))), "last_run_at": None,
               "runs_count": 0, "max_runs": max_runs, "last_error": None,
               "created_at": iso(now), "updated_at": iso(now), "owner_id": owner_id, "account_id": account_id,
               "mode": mode, "cursor": 0, "active_from": active_from, "active_to": active_to}
        return self._insert("meow_jobs", row)

    def list_jobs(self, active_only=False, owner_id=None, account_id=None):
        where: dict[str, Any] = {}
        if active_only:
            where["status"] = "active"
        if owner_id is not None:
            where["owner_id"] = owner_id
        if account_id is not None:
            where["account_id"] = account_id
        return self._select("meow_jobs", where, order="created_at", desc=True)

    def get_job(self, job_id: str):
        rows = self._select("meow_jobs", {"id": job_id}, limit=1)
        return rows[0] if rows else None

    def set_job_status(self, job_id: str, status: str) -> bool:
        if status not in {"active", "paused"}:
            raise ValueError("invalid status")
        values: dict[str, Any] = {"status": status, "updated_at": iso(utcnow())}
        if status == "active":
            row = self.get_job(job_id)
            if not row:
                return False
            nxt = parse_dt(row.get("next_run_at"))
            if nxt is None or nxt < utcnow():
                values["next_run_at"] = iso(utcnow() + timedelta(seconds=int(row["interval_seconds"])))
        return self._update("meow_jobs", {"id": job_id}, values) > 0

    def trigger_job(self, job_id: str) -> bool:
        now = iso(utcnow())
        return self._update("meow_jobs", {"id": job_id, "status": "active"},
                            {"next_run_at": now, "updated_at": now}) > 0

    def delete_job(self, job_id: str) -> bool:
        return self._delete("meow_jobs", {"id": job_id}) > 0

    def claim_due_job(self, job, new_cursor=None):
        """Advance the schedule BEFORE doing I/O: prefer a missed run over a duplicate."""
        now = utcnow()
        runs = int(job.get("runs_count") or 0) + 1
        max_runs = job.get("max_runs")
        values = {"last_run_at": iso(now), "next_run_at": iso(now + timedelta(seconds=int(job["interval_seconds"]))),
                  "runs_count": runs, "status": "paused" if max_runs and runs >= int(max_runs) else "active",
                  "updated_at": iso(now), "last_error": None}
        if new_cursor is not None:
            values["cursor"] = int(new_cursor)
        n = self._update("meow_jobs", {"id": job["id"], "status": "active"}, values)
        return {**job, **values} if n else None

    def set_job_error(self, job_id: str, error: str):
        self._update("meow_jobs", {"id": job_id}, {"last_error": str(error)[:1000], "updated_at": iso(utcnow())})

    # ---------------------------------------------------------------- runtime state (encrypted blobs)
    def get_runtime_state(self, key: str):
        rows = self._select("meow_runtime_state", {"key": key}, limit=1)
        return str(rows[0]["value"]) if rows else None

    def set_runtime_state(self, key: str, value: str):
        self._upsert("meow_runtime_state", {"key": key, "value": value, "updated_at": iso(utcnow())}, "key")

    def delete_runtime_state(self, key: str):
        self._delete("meow_runtime_state", {"key": key})

    # ---------------------------------------------------------------- events
    def add_event(self, level: str, message: str, owner_id: str = OWNER_ID):
        self._insert("meow_events", {"level": level, "message": str(message)[:1500],
                                     "created_at": iso(utcnow()), "owner_id": owner_id})

    def list_events(self, limit=80, owner_id=None):
        limit = max(1, min(int(limit), 200))
        where = {"owner_id": owner_id} if owner_id is not None else None
        return self._select("meow_events", where, order="created_at" if self._client else "id", desc=True, limit=limit)
