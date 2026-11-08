"""Module: Self (scheduled messages / button clicks). Fully separate from the chat client.

Add more modules the same way: a Blueprint here + a `register(app, ctx)` call in app.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request

from deps import account_for, audit, current_user, err, hub, limited, login_required, store
from store import OWNER_ID, parse_dt, utcnow

log = logging.getLogger("borhan.self")
bp = Blueprint("self_bot", __name__, url_prefix="/api/self")

MIN_INTERVAL = lambda: int(os.getenv("MIN_JOB_INTERVAL_SECONDS", "60"))
MAX_ACTIVE = lambda: int(os.getenv("MAX_ACTIVE_JOBS_PER_ACCOUNT", "20"))
MAX_TOTAL = lambda: int(os.getenv("MAX_TOTAL_JOBS_PER_ACCOUNT", "200"))


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Berlin"))
    except Exception:
        return ZoneInfo("UTC")


def variants_of(payload: str) -> list[str]:
    return [line.strip() for line in str(payload).splitlines() if line.strip()]


def in_active_window(job: dict, now_local: datetime) -> bool:
    a, b = job.get("active_from"), job.get("active_to")
    if a is None or b is None:
        return True
    h = now_local.hour
    a, b = int(a), int(b)
    return (a <= h < b) if a <= b else (h >= a or h < b)


def job_authorized(job: dict, account: dict | None) -> bool:
    if not account:
        return False
    if job.get("owner_id") == account["owner_id"]:
        return True
    return job.get("owner_id") == OWNER_ID and bool(account.get("owner_access"))


def validate(data: dict):
    name = str(data.get("name", "")).strip()[:80]
    target = str(data.get("target", "")).strip()[:160]
    action = str(data.get("action", "send"))
    payload = str(data.get("payload", "")).strip()[:3000]
    mode = str(data.get("mode", "random"))
    try:
        interval = int(float(data.get("interval_minutes", 5)) * 60)
        runs = data.get("max_runs")
        max_runs = None if runs in (None, "", 0, "0") else int(runs)
        af, at = data.get("active_from"), data.get("active_to")
        af = None if af in (None, "") else int(af)
        at = None if at in (None, "") else int(at)
    except (TypeError, ValueError):
        raise ValueError("یکی از عددها نامعتبر است")
    if not name or not target or not payload:
        raise ValueError("نام، مقصد و متن الزامی است")
    if action not in {"send", "click"}:
        raise ValueError("نوع عملیات نامعتبر است")
    if mode not in {"random", "sequential"}:
        raise ValueError("حالت انتخاب متن نامعتبر است")
    if interval < MIN_INTERVAL() or interval > 86400:
        raise ValueError(f"فاصله باید بین {max(1, MIN_INTERVAL() // 60)} دقیقه و ۲۴ ساعت باشد")
    if max_runs is not None and not 1 <= max_runs <= 100000:
        raise ValueError("تعداد اجرا باید بین ۱ تا ۱۰۰۰۰۰ باشد")
    if (af is None) != (at is None) or any(x is not None and not 0 <= x <= 24 for x in (af, at)):
        raise ValueError("ساعت شروع و پایان را هر دو (۰ تا ۲۴) وارد کن یا هر دو را خالی بگذار")
    if af is not None and af == at:
        af = at = None
    if action == "click":
        payload = variants_of(payload)[0] if variants_of(payload) else ""
        if not payload:
            raise ValueError("نام دکمه الزامی است")
    return name, target, action, payload, interval, max_runs, mode, af, at


def _visible_jobs():
    user = current_user()
    if user["role"] == "owner":
        jobs = store().list_jobs()
    else:
        jobs = store().list_jobs(owner_id=user["id"])
    accounts = {a["id"]: a for a in store().list_accounts()}
    out = []
    for j in jobs:
        acc = accounts.get(j.get("account_id"))
        if not acc:
            continue
        mine = j.get("owner_id") == user["id"]
        # The owner sees job *content* of an admin only with that admin's consent.
        can_see = mine or (user["role"] == "owner" and acc.get("owner_access"))
        row = dict(j)
        row["account_label"] = acc["label"]
        row["account_owner_id"] = acc["owner_id"]
        if not can_see:
            row["payload"] = ""
            row["target"] = "—"
            row["hidden"] = True
        out.append(row)
    return out


@bp.get("/jobs")
@login_required
def jobs():
    return jsonify(_visible_jobs())


@bp.post("/jobs")
@login_required
@limited(30, 60)
def create_job():
    data = request.get_json(silent=True) or {}
    acc, bad = account_for(str(data.get("account_id", "")), "content")
    if bad:
        return bad
    try:
        name, target, action, payload, interval, max_runs, mode, af, at = validate(data)
    except ValueError as exc:
        return err(str(exc))
    existing = store().list_jobs(account_id=acc["id"])
    if len(existing) >= MAX_TOTAL():
        return err(f"حداکثر {MAX_TOTAL()} کار برای هر حساب مجاز است")
    if sum(1 for j in existing if j["status"] == "active") >= MAX_ACTIVE():
        return err(f"حداکثر {MAX_ACTIVE()} کار فعال برای هر حساب مجاز است")
    user = current_user()
    audit(acc, f"ساخت کار زمان‌بندی‌شده «{name}»", "warning")
    row = store().create_job(name=name, target=target, action=action, payload=payload, interval_seconds=interval,
                             owner_id=user["id"], account_id=acc["id"], max_runs=max_runs, mode=mode,
                             active_from=af, active_to=at)
    store().add_event("success", f"کار جدید ساخته شد: {name}", acc["owner_id"])
    return jsonify(row), 201


@bp.post("/jobs/<job_id>/<action>")
@login_required
@limited(60, 60)
def job_action(job_id, action):
    job = store().get_job(job_id)
    user = current_user()
    if not job:
        return err("کار پیدا نشد", 404)
    acc = store().get_account(job.get("account_id") or "")
    if not acc:
        return err("حساب این کار پیدا نشد", 404)
    mine = job["owner_id"] == user["id"]
    if not mine:
        # Owner may pause/delete anything (moderation); run/resume only with consent.
        if user["role"] != "owner":
            return err("کار پیدا نشد", 404)
        if action in {"run", "resume"} and not acc.get("owner_access"):
            return err("برای اجرای کارهای این حساب، صاحب حساب باید دسترسی مالک را فعال کند", 403)
    if action == "pause":
        ok = store().set_job_status(job_id, "paused")
    elif action == "resume":
        ok = store().set_job_status(job_id, "active")
    elif action == "run":
        ok = store().trigger_job(job_id)
    elif action == "delete":
        ok = store().delete_job(job_id)
    else:
        return err("عملیات نامعتبر است")
    if not mine:
        store().add_event("warning", f"مالک کار «{job['name']}» را {action} کرد.", acc["owner_id"])
    return jsonify({"ok": bool(ok)})


@bp.post("/send-test")
@login_required
@limited(20, 60)
def send_test():
    data = request.get_json(silent=True) or {}
    acc, bad = account_for(str(data.get("account_id", "")), "content")
    if bad:
        return bad
    target, text = str(data.get("target", "")).strip(), str(data.get("text", "")).strip()
    if not target or not text:
        return err("مقصد و متن لازم است")
    audit(acc, "ارسال آزمایشی", "warning")
    hub().call(hub().send_text(acc["id"], target, text), 25)
    store().add_event("success", f"ارسال آزمایشی به {target}", acc["owner_id"])
    return jsonify({"ok": True})


# ---------------------------------------------------------------- scheduler
class Scheduler:
    """Runs inside the Telegram hub's event loop."""

    def __init__(self, st, tg):
        self.store, self.hub = st, tg
        self._warned: dict[str, float] = {}

    def start(self):
        if self.hub.loop:
            self.hub.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._loop()))

    async def _loop(self):
        poll = max(5, int(os.getenv("SCHEDULER_POLL_SECONDS", "10")))
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(poll)

    async def _tick(self):
        jobs = await asyncio.to_thread(self.store.list_jobs, True)
        now = utcnow()
        due = [j for j in jobs if (parse_dt(j.get("next_run_at")) or now) <= now]
        if not due:
            return
        accounts = {a["id"]: a for a in await asyncio.to_thread(self.store.list_accounts)}
        by_account: dict[str, list] = {}
        for j in due:
            by_account.setdefault(j.get("account_id") or "", []).append(j)
        await asyncio.gather(*(self._run_account(accounts.get(aid), js) for aid, js in by_account.items()))

    async def _warn(self, job, text, owner_id):
        last = self._warned.get(job["id"], 0)
        if asyncio.get_running_loop().time() - last > 600:
            self._warned[job["id"]] = asyncio.get_running_loop().time()
            await asyncio.to_thread(self.store.set_job_error, job["id"], text)
            await asyncio.to_thread(self.store.add_event, "warning", f"«{job['name']}»: {text}", owner_id)

    async def _run_account(self, account, jobs):
        for job in jobs:
            try:
                if not account:
                    await asyncio.to_thread(self.store.set_job_status, job["id"], "paused")
                    continue
                oid = account["owner_id"]
                if not job_authorized(job, account):
                    await self._warn(job, "دسترسی مالک به این حساب لغو شده است", oid)
                    continue
                if not self.hub.is_connected(account["id"]):
                    await self._warn(job, "حساب به تلگرام وصل نیست", oid)
                    continue
                if not in_active_window(job, datetime.now(_tz())):
                    continue
                variants = variants_of(job["payload"]) or [job["payload"]]
                cursor = int(job.get("cursor") or 0)
                if job["action"] == "send" and len(variants) > 1:
                    idx = random.randrange(len(variants)) if job.get("mode") == "random" else cursor % len(variants)
                else:
                    idx = 0
                claimed = await asyncio.to_thread(self.store.claim_due_job, job, cursor + 1)
                if not claimed:
                    continue
                try:
                    if job["action"] == "send":
                        await self.hub.send_text(account["id"], job["target"], variants[idx])
                        msg = f"پیام زمان‌بندی‌شده ارسال شد: {job['name']} → {job['target']}"
                    else:
                        await self.hub.click_text(account["id"], job["target"], variants[0])
                        msg = f"روی دکمه کلیک شد: {job['name']} → {job['target']}"
                    await asyncio.to_thread(self.store.add_event, "success", msg, oid)
                except Exception as exc:
                    text = str(exc).strip()[:300] or "خطای نامشخص در اجرای عملیات دسکتاپ"
                    await asyncio.to_thread(self.store.set_job_error, job["id"], text)
                    await asyncio.to_thread(self.store.add_event, "error", f"«{job['name']}» ناموفق بود: {text}", oid)
                await asyncio.sleep(1.5)
            except Exception:
                log.exception("job run failed")
