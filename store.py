from __future__ import annotations

import json
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
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class StoreError(RuntimeError):
    pass


class Store:
    """Supabase-first storage with a local SQLite fallback for development."""

    def __init__(self, data_dir: str | Path):
        self._lock = threading.RLock()
        self._client = None
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        if url and key:
            try:
                from supabase import create_client
                self._client = create_client(url, key)
                self.backend = "supabase"
            except Exception as exc:
                raise StoreError(f"Supabase initialization failed: {exc}") from exc
        else:
            base = Path(data_dir)
            base.mkdir(parents=True, exist_ok=True)
            self._db_path = base / "meow-local.sqlite3"
            self.backend = "sqlite-local"
            self._init_sqlite()

    def _connect(self):
        con = sqlite3.connect(self._db_path, timeout=20, check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con

    def _init_sqlite(self):
        with self._connect() as con:
            con.execute("""create table if not exists jobs (
                id text primary key, name text not null, target text not null,
                action text not null, payload text not null,
                interval_seconds integer not null, status text not null,
                next_run_at text not null, last_run_at text,
                runs_count integer not null default 0, max_runs integer,
                last_error text, created_at text not null, updated_at text not null
            )""")
            con.execute("""create table if not exists events (
                id integer primary key autoincrement, level text not null,
                message text not null, created_at text not null
            )""")
            con.commit()

    def create_job(self, *, name: str, target: str, action: str, payload: str,
                   interval_seconds: int, max_runs: int | None = None) -> dict[str, Any]:
        now = utcnow()
        row = {
            "id": str(uuid.uuid4()), "name": name.strip(), "target": target.strip(),
            "action": action, "payload": payload, "interval_seconds": int(interval_seconds),
            "status": "active", "next_run_at": iso(now + timedelta(seconds=interval_seconds)),
            "last_run_at": None, "runs_count": 0, "max_runs": max_runs,
            "last_error": None, "created_at": iso(now), "updated_at": iso(now),
        }
        if self._client:
            data = self._client.table("meow_jobs").insert(row).execute().data
            if not data:
                raise StoreError("Supabase did not return the inserted job")
            return data[0]
        with self._lock, self._connect() as con:
            con.execute("""insert into jobs values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        tuple(row[k] for k in ("id", "name", "target", "action", "payload", "interval_seconds",
                                               "status", "next_run_at", "last_run_at", "runs_count", "max_runs",
                                               "last_error", "created_at", "updated_at")))
            con.commit()
        return row

    def list_jobs(self, active_only: bool = False) -> list[dict[str, Any]]:
        if self._client:
            query = self._client.table("meow_jobs").select("*").order("created_at", desc=True)
            if active_only:
                query = query.eq("status", "active")
            return list(query.execute().data or [])
        sql = "select * from jobs"
        if active_only:
            sql += " where status='active'"
        sql += " order by created_at desc"
        with self._lock, self._connect() as con:
            return [dict(r) for r in con.execute(sql).fetchall()]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        if self._client:
            data = self._client.table("meow_jobs").select("*").eq("id", job_id).limit(1).execute().data
            return data[0] if data else None
        with self._lock, self._connect() as con:
            row = con.execute("select * from jobs where id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def set_job_status(self, job_id: str, status: str) -> bool:
        if status not in {"active", "paused"}:
            raise ValueError("Invalid status")
        values = {"status": status, "updated_at": iso(utcnow())}
        if status == "active":
            row = self.get_job(job_id)
            if not row:
                return False
            if parse_dt(row.get("next_run_at")) < utcnow():
                values["next_run_at"] = iso(utcnow() + timedelta(seconds=int(row["interval_seconds"])))
        if self._client:
            data = self._client.table("meow_jobs").update(values).eq("id", job_id).execute().data
            return bool(data)
        with self._lock, self._connect() as con:
            cur = con.execute("update jobs set status=?, updated_at=?, next_run_at=coalesce(?, next_run_at) where id=?",
                              (values["status"], values["updated_at"], values.get("next_run_at"), job_id))
            con.commit()
            return cur.rowcount > 0

    def delete_job(self, job_id: str) -> bool:
        if self._client:
            data = self._client.table("meow_jobs").delete().eq("id", job_id).execute().data
            return bool(data)
        with self._lock, self._connect() as con:
            cur = con.execute("delete from jobs where id=?", (job_id,))
            con.commit()
            return cur.rowcount > 0

    def claim_due_job(self, job: dict[str, Any]) -> dict[str, Any] | None:
        """Advance the schedule before performing I/O, preventing bursts after restarts.

        With one Render instance this is sufficient. It intentionally prefers a missed run
        over duplicating a message if the process crashes between claim and send.
        """
        now = utcnow()
        next_runs = int(job.get("runs_count") or 0) + 1
        max_runs = job.get("max_runs")
        status = "paused" if max_runs and next_runs >= int(max_runs) else "active"
        values = {
            "last_run_at": iso(now),
            "next_run_at": iso(now + timedelta(seconds=int(job["interval_seconds"]))),
            "runs_count": next_runs,
            "status": status,
            "updated_at": iso(now),
            "last_error": None,
        }
        if self._client:
            data = self._client.table("meow_jobs").update(values).eq("id", job["id"]).eq("status", "active").execute().data
            return data[0] if data else None
        with self._lock, self._connect() as con:
            cur = con.execute("""update jobs set last_run_at=?, next_run_at=?, runs_count=?, status=?,
                    updated_at=?, last_error=NULL where id=? and status='active'""",
                    (values["last_run_at"], values["next_run_at"], values["runs_count"],
                     values["status"], values["updated_at"], job["id"]))
            con.commit()
            if cur.rowcount == 0:
                return None
        return {**job, **values}

    def set_job_error(self, job_id: str, error: str):
        values = {"last_error": error[:1000], "updated_at": iso(utcnow())}
        if self._client:
            self._client.table("meow_jobs").update(values).eq("id", job_id).execute()
            return
        with self._lock, self._connect() as con:
            con.execute("update jobs set last_error=?, updated_at=? where id=?",
                        (values["last_error"], values["updated_at"], job_id))
            con.commit()

    def add_event(self, level: str, message: str):
        now = iso(utcnow())
        clean = str(message)[:1500]
        if self._client:
            self._client.table("meow_events").insert({"level": level, "message": clean, "created_at": now}).execute()
            return
        with self._lock, self._connect() as con:
            con.execute("insert into events(level, message, created_at) values (?, ?, ?)", (level, clean, now))
            con.commit()

    def list_events(self, limit: int = 60) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        if self._client:
            return list(self._client.table("meow_events").select("*").order("created_at", desc=True).limit(limit).execute().data or [])
        with self._lock, self._connect() as con:
            return [dict(r) for r in con.execute("select * from events order by id desc limit ?", (limit,)).fetchall()]
