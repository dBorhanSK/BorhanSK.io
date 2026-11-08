"""Borhan_SK backend (Flask). Serves JSON only; the UI lives on Netlify (../frontend)."""
from __future__ import annotations

import atexit
import hmac
import logging
import os
import secrets
import threading
import time
from datetime import timedelta

from flask import Blueprint, Flask, jsonify, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from deps import (account_for, client_ip, csrf_check, current_user, err, hub, invalidate_member, limited,
                  login_required, owner_required, rate_limit, store)
from modules import desktop, self_bot
from store import OWNER_ID, Store, utcnow
from desktop_hub import DesktopHub, HubError

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("borhan.web")

core = Blueprint("core", __name__)
_started = utcnow()
_login_fail: dict[str, list[float]] = {}


# ---------------------------------------------------------------- health / wake (public, no secrets)
@core.get("/ping")
@core.get("/api/ping")
def ping():
    return jsonify({"ok": True, "t": int(time.time())})


@core.get("/health")
def health():
    tg = hub()
    return jsonify({"ok": True, "engine": "ready" if tg.configured else "desktop-not-ready",
                    "uptime_s": int((utcnow() - _started).total_seconds())})


# ---------------------------------------------------------------- auth
def _locked(key: str, limit: int) -> bool:
    window = int(os.getenv("LOGIN_LOCK_SECONDS", "900"))
    now = time.monotonic()
    arr = [t for t in _login_fail.get(key, []) if now - t < window]
    _login_fail[key] = arr
    return len(arr) >= limit


def _fail(key: str):
    _login_fail.setdefault(key, []).append(time.monotonic())


@core.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip().lower()[:64]
    password = str(data.get("password", ""))[:256]
    ip = client_ip()
    max_try = int(os.getenv("LOGIN_MAX_ATTEMPTS", "7"))
    if _locked(f"ip:{ip}", max_try) or _locked(f"u:{username}", max_try * 5):
        return err("تعداد تلاش‌های ناموفق زیاد است؛ کمی بعد دوباره امتحان کن", 429)
    owner_name = os.getenv("OWNER_USERNAME", "owner").strip().lower()
    owner_pw = os.getenv("DASHBOARD_PASSWORD", "")
    user = None
    if username in {"", owner_name}:
        if not owner_pw:
            return err("رمز مالک (DASHBOARD_PASSWORD) روی سرور تنظیم نشده است", 503)
        if hmac.compare_digest(owner_pw.encode(), password.encode()):
            user = {"id": OWNER_ID, "role": "owner", "name": owner_name}
    if user is None and username and username != owner_name:
        m = store().get_member(username, with_hash=True)
        ok = bool(m and m.get("active", True) and check_password_hash(m["password_hash"], password))
        if not m:
            check_password_hash(generate_password_hash("x"), password)  # equalise timing
        if ok:
            user = {"id": m["id"], "role": "admin", "name": m["username"]}
            store().touch_member_login(m["id"])
    if user is None:
        _fail(f"ip:{ip}")
        _fail(f"u:{username}")
        return err("نام کاربری یا رمز عبور نادرست است", 401)
    session.clear()
    session.update(uid=user["id"], role=user["role"], name=user["name"], csrf=secrets.token_urlsafe(32))
    session.permanent = True
    store().add_event("info", f"ورود موفق: {user['name']}", user["id"])
    return jsonify(_me())


@core.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


def _me():
    u = current_user()
    return {"id": u["id"], "role": u["role"], "name": u["name"], "csrf": session.get("csrf"),
            "engine_ready": hub().configured, "timezone": os.getenv("APP_TIMEZONE", "Europe/Berlin")}


@core.get("/api/me")
def me():
    if not current_user():
        return err("وارد نشده‌ای", 401)
    return jsonify(_me())


@core.post("/api/me/password")
@login_required
@limited(5, 300)
def change_password():
    u = current_user()
    if u["role"] == "owner":
        return err("رمز مالک را باید در Environment سرور Oracle (DASHBOARD_PASSWORD) تغییر بدهی", 400)
    data = request.get_json(silent=True) or {}
    m = store().get_member_by_id(u["id"], with_hash=True)
    if not m or not check_password_hash(m["password_hash"], str(data.get("current", ""))):
        return err("رمز فعلی نادرست است", 403)
    new = str(data.get("new", ""))
    if len(new) < 12:
        return err("رمز جدید حداقل ۱۲ نویسه باشد")
    store().update_member(u["id"], password_hash=generate_password_hash(new, method="scrypt"))
    return jsonify({"ok": True})


# ---------------------------------------------------------------- accounts
def _public_account(a: dict, viewer: dict) -> dict:
    h = hub()
    holder = a["owner_id"] == viewer["id"]
    is_owner = viewer["role"] == "owner"
    connected = h.is_connected(a["id"])
    stage = h.stage_of(a["id"])
    members = _members_map() if is_owner else {}
    out = {
        "id": a["id"], "label": a["label"], "platform": a["platform"],
        "connected": connected, "desktop_running": h.running(a["id"]), "stage": stage,
        "status": "connected" if connected else a.get("status", "pending"),
        "tg_name": a.get("tg_name") or "", "tg_username": a.get("tg_username") or "",
        "tg_phone": a.get("tg_phone") or "", "tg_user_id": a.get("tg_user_id") or "",
        "last_seen_at": a.get("last_seen_at"), "created_at": a.get("created_at"),
        "last_error": a.get("last_error") or h.last_error.get(a["id"], ""),
        "owner_access": bool(a.get("owner_access")),
        "mine": holder,
        "holder": "owner" if a["owner_id"] == OWNER_ID else members.get(a["owner_id"], "ادمین"),
        "can_login": holder,
        "can_use": holder or (is_owner and bool(a.get("owner_access"))),
        "can_manage": holder or is_owner,
        "can_toggle_access": holder and viewer["role"] == "admin",
    }
    return out


def _members_map() -> dict:
    return {m["id"]: m["username"] for m in store().list_members()}


@core.get("/api/accounts")
@login_required
def accounts():
    u = current_user()
    rows = store().list_accounts() if u["role"] == "owner" else store().list_accounts(u["id"])
    return jsonify([_public_account(a, u) for a in rows])


@core.post("/api/accounts")
@login_required
@limited(20, 60)
def create_account():
    u = current_user()
    data = request.get_json(silent=True) or {}
    label = str(data.get("label", "")).strip()[:80]
    if not label:
        return err("برای تشخیص حساب یک نام دلخواه بنویس")
    limit = int(os.getenv("MAX_ACCOUNTS_PER_USER", "0"))
    if limit and len(store().list_accounts(u["id"])) >= limit:
        return err(f"حداکثر {limit} حساب برای هر کاربر مجاز است")
    acc = store().create_account(u["id"], label)
    store().add_event("info", f"حساب جدید اضافه شد: {label}", u["id"])
    return jsonify(_public_account(acc, u)), 201


@core.patch("/api/accounts/<account_id>")
@login_required
def rename_account(account_id):
    acc, bad = account_for(account_id, "login")
    if bad:
        return bad
    label = str((request.get_json(silent=True) or {}).get("label", "")).strip()[:80]
    if not label:
        return err("نام نمی‌تواند خالی باشد")
    store().update_account(acc["id"], label=label)
    return jsonify({"ok": True})


@core.delete("/api/accounts/<account_id>")
@login_required
@limited(10, 60)
def delete_account(account_id):
    acc, bad = account_for(account_id, "meta")
    if bad:
        return bad
    try:
        hub().call(hub().remove(acc["id"], revoke=True), 20)
    except HubError:
        pass
    store().delete_account(acc["id"])
    u = current_user()
    if acc["owner_id"] != u["id"]:
        store().add_event("warning", f"مالک حساب «{acc['label']}» را حذف کرد.", acc["owner_id"])
    store().add_event("info", f"حساب حذف شد: {acc['label']}", u["id"])
    return jsonify({"ok": True})


@core.post("/api/accounts/<account_id>/restart")
@login_required
@limited(10, 60)
def restart_account(account_id):
    acc, bad = account_for(account_id, "meta")
    if bad:
        return bad
    hub().call(hub().disconnect_keep(acc["id"]), 40)
    return jsonify({"ok": True, "connected": hub().is_connected(acc["id"])})


@core.post("/api/accounts/<account_id>/owner-access")
@login_required
@limited(10, 60)
def owner_access(account_id):
    acc, bad = account_for(account_id, "login")
    if bad:
        return bad
    enabled = (request.get_json(silent=True) or {}).get("enabled")
    if not isinstance(enabled, bool):
        return err("مقدار نامعتبر است")
    if current_user()["role"] != "admin":
        return err("مالک نیازی به این مجوز روی حساب‌های خودش ندارد")
    store().update_account(acc["id"], owner_access=enabled)
    if not enabled:
        # Close current remote desktop viewers as well as invalidating unused links.
        hub().call(hub().revoke_view_tokens(acc["id"]), 15)
    store().add_event("warning" if enabled else "info",
                      f"دسترسی مالک به حساب «{acc['label']}» {'فعال' if enabled else 'لغو'} شد.", acc["owner_id"])
    return jsonify({"ok": True, "owner_access": enabled})


# ---------------------------------------------------------------- admins (owner only)
@core.get("/api/admins")
@owner_required
def admins():
    accts = store().list_accounts()
    out = []
    for m in store().list_members():
        mine = [a for a in accts if a["owner_id"] == m["id"]]
        out.append({"id": m["id"], "username": m["username"], "active": m["active"], "created_at": m["created_at"],
                    "last_login_at": m.get("last_login_at"), "accounts": len(mine),
                    "connected": sum(1 for a in mine if hub().is_connected(a["id"]))})
    return jsonify(out)


@core.post("/api/admins")
@owner_required
@limited(10, 60)
def create_admin():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip().lower()
    password = str(data.get("password", ""))
    owner_name = os.getenv("OWNER_USERNAME", "owner").strip().lower()
    if not 3 <= len(username) <= 32 or not username.replace("_", "").replace(".", "").replace("-", "").isalnum() \
            or not username.isascii():
        return err("نام کاربری باید ۳ تا ۳۲ نویسهٔ انگلیسی (حرف، عدد، _ . -) باشد")
    if username in {owner_name, OWNER_ID}:
        return err("این نام کاربری برای مالک رزرو شده است")
    if len(password) < 12:
        return err("رمز ادمین حداقل ۱۲ نویسه باشد")
    try:
        row = store().create_member(username, generate_password_hash(password, method="scrypt"))
    except Exception as exc:
        return err(str(exc)[:200])
    store().add_event("info", f"ادمین جدید ساخته شد: {username}", OWNER_ID)
    return jsonify(row), 201


@core.patch("/api/admins/<member_id>")
@owner_required
@limited(20, 60)
def update_admin(member_id):
    data = request.get_json(silent=True) or {}
    m = store().get_member_by_id(member_id)
    if not m:
        return err("ادمین پیدا نشد", 404)
    if "active" in data:
        store().update_member(member_id, active=bool(data["active"]))
        invalidate_member(member_id)
        store().add_event("warning", f"ادمین {m['username']} {'فعال' if data['active'] else 'غیرفعال'} شد.", OWNER_ID)
    if data.get("password"):
        if len(str(data["password"])) < 12:
            return err("رمز ادمین حداقل ۱۲ نویسه باشد")
        store().update_member(member_id, password_hash=generate_password_hash(str(data["password"]), method="scrypt"))
        store().add_event("warning", f"رمز ادمین {m['username']} تغییر کرد.", OWNER_ID)
    return jsonify({"ok": True})


@core.delete("/api/admins/<member_id>")
@owner_required
@limited(10, 60)
def delete_admin(member_id):
    m = store().get_member_by_id(member_id)
    if not m:
        return err("ادمین پیدا نشد", 404)
    for a in store().list_accounts(member_id):
        try:
            hub().call(hub().remove(a["id"], revoke=True), 20)
        except HubError:
            pass
        store().delete_account(a["id"])
    for j in store().list_jobs(owner_id=member_id):
        store().delete_job(j["id"])
    store().delete_member(member_id)
    invalidate_member(member_id)
    store().add_event("warning", f"ادمین حذف شد: {m['username']}", OWNER_ID)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- overview / events
@core.get("/api/overview")
@login_required
def overview():
    u = current_user()
    owner = u["role"] == "owner"
    accts = store().list_accounts() if owner else store().list_accounts(u["id"])
    jobs = store().list_jobs() if owner else store().list_jobs(owner_id=u["id"])
    out = {"accounts": len(accts), "connected": sum(1 for a in accts if hub().is_connected(a["id"])),
           "jobs": len(jobs), "jobs_active": sum(1 for j in jobs if j["status"] == "active"),
           "storage": store().backend, "engine_ready": hub().configured}
    if owner:
        out["admins"] = len(store().list_members())
    return jsonify(out)


@core.get("/api/events")
@login_required
def events():
    u = current_user()
    rows = store().list_events(100) if u["role"] == "owner" else store().list_events(100, u["id"])
    return jsonify(rows)


# ---------------------------------------------------------------- webhook for external tools (owner token)
@core.post("/hooks/send")
@limited(30, 60)
def hook_send():
    expected = os.getenv("WEBHOOK_TOKEN", "").strip()
    supplied = request.headers.get("X-Webhook-Token", "").strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        return err("unauthorized", 401)
    data = request.get_json(silent=True) or {}
    target, text = str(data.get("target", "")).strip(), str(data.get("text", "")).strip()
    account_id = str(data.get("account_id", "")).strip()
    if not target or not text or len(text) > 4096:
        return err("target و text لازم است (حداکثر ۴۰۹۶ نویسه)")
    owner_accounts = [a for a in store().list_accounts(OWNER_ID) if hub().is_connected(a["id"])]
    acc = next((a for a in owner_accounts if a["id"] == account_id), None) if account_id else \
        (owner_accounts[0] if len(owner_accounts) == 1 else None)
    if not acc:
        return err("account_id یکی از حساب‌های متصل مالک را مشخص کن", 400)
    try:
        hub().call(hub().send_text(acc["id"], target, text), 25)
    except HubError as exc:
        return err(str(exc), exc.status)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- keepalive (best effort)
# ---------------------------------------------------------------- app factory
def _validate_production_config():
    """Fail closed instead of exposing the dashboard with sample credentials."""
    required = {
        "API_HOST": os.getenv("API_HOST", ""),
        "SUPABASE_URL": os.getenv("SUPABASE_URL", ""),
        "SUPABASE_SERVICE_ROLE_KEY": os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
        "DASHBOARD_PASSWORD": os.getenv("DASHBOARD_PASSWORD", ""),
        "SECRET_KEY": os.getenv("SECRET_KEY", ""),
        "WEBHOOK_TOKEN": os.getenv("WEBHOOK_TOKEN", ""),
    }
    placeholders = ("YOUR-", "YOUR_", "CHANGE-", "REPLACE_", "YOUR.")
    bad = [name for name, value in required.items()
           if not value.strip() or any(mark in value.upper() for mark in placeholders)]
    if len(required["DASHBOARD_PASSWORD"]) < 16:
        bad.append("DASHBOARD_PASSWORD (حداقل ۱۶ نویسه)")
    if len(required["SECRET_KEY"]) < 32:
        bad.append("SECRET_KEY (حداقل ۳۲ نویسه تصادفی)")
    if len(required["WEBHOOK_TOKEN"]) < 32:
        bad.append("WEBHOOK_TOKEN (حداقل ۳۲ نویسه تصادفی)")
    if bad:
        raise RuntimeError("تنظیمات امنیتی/استقرار کامل نیست: " + ", ".join(dict.fromkeys(bad)))


def create_app(testing: bool = False, store_obj: Store | None = None, hub_obj=None) -> Flask:
    if not testing:
        _validate_production_config()
    app = Flask(__name__)
    app.secret_key = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)
    app.config.update(
        SESSION_COOKIE_NAME="borhan_sk",
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "true").lower() == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=int(os.getenv("SESSION_HOURS", "12"))),
        MAX_CONTENT_LENGTH=26 * 1024 * 1024, JSON_AS_ASCII=False,
    )
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=2, x_proto=2, x_host=1)  # type: ignore[assignment]
    st = store_obj or Store(os.getenv("DATA_DIR", "/tmp/borhan-data"))
    tg = hub_obj or DesktopHub(st)
    app.extensions["borhan"] = {"store": st, "hub": tg}
    app.register_blueprint(core)
    app.register_blueprint(desktop.bp)
    app.register_blueprint(self_bot.bp)
    app.before_request(csrf_check)

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/") and "Cache-Control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(HubError)
    def hub_error(exc):
        return err(str(exc), exc.status)

    @app.errorhandler(404)
    def nf(_):
        return err("مسیر پیدا نشد", 404)

    @app.errorhandler(413)
    def too_big(_):
        return err("حجم درخواست زیاد است", 413)

    @app.errorhandler(Exception)
    def unexpected(exc):
        if isinstance(exc, HTTPException):
            return err(exc.description or "خطا", exc.code or 400)
        log.exception("unhandled error")
        return err("خطای داخلی سرور؛ گزارش فنی ثبت شد", 500)

    if not testing:
        tg.start()
        tg.ready.wait(5)
        self_bot.Scheduler(st, tg).start()
        atexit.register(tg.shutdown)
        # A low-cost heartbeat records service liveness in Supabase. This is useful
        # for diagnostics/backups, but it does NOT prevent a cloud provider from
        # reclaiming an Always Free VM based on its own utilization policy.
        def heartbeat():
            while True:
                try:
                    st.set_runtime_state("service:heartbeat", utcnow().isoformat())
                except Exception:
                    log.exception("Supabase heartbeat failed")
                time.sleep(max(60, int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "300"))))
        threading.Thread(target=heartbeat, daemon=True, name="supabase-heartbeat").start()
    return app


app = create_app() if os.getenv("BORHAN_NO_AUTOSTART") != "1" else None  # gunicorn entrypoint: app:app

if __name__ == "__main__":  # local dev
    (app or create_app()).run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True)
