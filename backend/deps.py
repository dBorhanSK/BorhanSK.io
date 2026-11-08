"""Shared helpers: auth, CSRF, access rules, rate limiting, audit log."""
from __future__ import annotations

import hmac
import os
import time
from functools import wraps

from flask import current_app, jsonify, request, session

from store import OWNER_ID

_member_cache: dict[str, tuple[float, bool]] = {}
_buckets: dict[str, list[float]] = {}
_audit_seen: dict[tuple, float] = {}


def ctx():
    return current_app.extensions["borhan"]


def store():
    return ctx()["store"]


def hub():
    return ctx()["hub"]


def err(message: str, status: int = 400):
    return jsonify({"error": message}), status


def current_user() -> dict | None:
    uid = session.get("uid")
    if not uid:
        return None
    role = session.get("role")
    if role == "owner":
        return {"id": OWNER_ID, "role": "owner", "name": session.get("name") or "owner"}
    cached = _member_cache.get(uid)
    now = time.monotonic()
    if cached and now - cached[0] < 20:
        ok = cached[1]
    else:
        m = store().get_member_by_id(uid)
        ok = bool(m and m.get("active", True))
        _member_cache[uid] = (now, ok)
    if not ok:
        session.clear()
        return None
    return {"id": uid, "role": "admin", "name": session.get("name") or "admin"}


def invalidate_member(member_id: str):
    _member_cache.pop(member_id, None)


def login_required(fn):
    @wraps(fn)
    def wrapped(*a, **kw):
        user = current_user()
        if not user:
            return err("برای ادامه وارد شو", 401)
        return fn(*a, **kw)
    return wrapped


def owner_required(fn):
    @wraps(fn)
    @login_required
    def wrapped(*a, **kw):
        if current_user()["role"] != "owner":
            return err("این بخش فقط برای مالک است", 403)
        return fn(*a, **kw)
    return wrapped


def allowed_origins() -> set[str]:
    raw = os.getenv("PUBLIC_ORIGINS", "https://borhansk.netlify.app")
    out = {o.strip().rstrip("/") for o in raw.split(",") if o.strip()}
    out.add(request.host_url.rstrip("/"))
    return out


def csrf_check():
    """Called for every state-changing request (see app.before_request)."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") not in allowed_origins():
        return err("منبع درخواست مجاز نیست", 403)
    if request.endpoint in {"core.login", "core.hook_send"}:
        return None
    expected = session.get("csrf", "")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        return err("نشست منقضی شده؛ صفحه را تازه کن", 403)
    return None


def rate_limit(key: str, limit: int, window: int = 60) -> bool:
    now = time.monotonic()
    arr = [t for t in _buckets.get(key, []) if now - t < window]
    if len(arr) >= limit:
        _buckets[key] = arr
        return False
    arr.append(now)
    _buckets[key] = arr
    if len(_buckets) > 5000:
        for k in list(_buckets)[:1000]:
            _buckets.pop(k, None)
    return True


def limited(limit: int, window: int = 60):
    def deco(fn):
        @wraps(fn)
        def wrapped(*a, **kw):
            uid = (current_user() or {}).get("id", "anon")
            if not rate_limit(f"{fn.__name__}:{uid}", limit, window):
                return err("درخواست‌ها زیاد است؛ کمی صبر کن", 429)
            return fn(*a, **kw)
        return wrapped
    return deco


def client_ip() -> str:
    return (request.headers.get("X-Nf-Client-Connection-Ip")
            or (request.headers.get("X-Forwarded-For", "").split(",")[0].strip())
            or request.remote_addr or "?")


# ---------------------------------------------------------------- account access rules
def account_for(account_id: str, level: str):
    """Return (account, None) or (None, error_response).

    level:
      login   – only the account holder (QR / phone / code / password, logout)
      content – holder, OR the owner when the holder switched on "owner access"
      meta    – holder or owner (label, status, identity, remove)
    """
    user = current_user()
    acc = store().get_account(account_id)
    if not user or not acc:
        return None, err("حساب پیدا نشد", 404)
    holder = acc["owner_id"] == user["id"]
    if holder:
        return acc, None
    if user["role"] != "owner":
        return None, err("حساب پیدا نشد", 404)
    if level == "login":
        return None, err("ورود به حساب فقط توسط صاحب همان حساب انجام می‌شود", 403)
    if level == "content" and not acc.get("owner_access"):
        return None, err("صاحب این حساب هنوز «دسترسی مالک» را فعال نکرده است", 403)
    return acc, None


def audit(acc: dict, what: str, level: str = "info"):
    """Write an event the account holder can see whenever the owner acts on their account."""
    user = current_user()
    if not user or acc["owner_id"] == user["id"]:
        return
    key = (acc["id"], what)
    now = time.monotonic()
    if now - _audit_seen.get(key, 0) < 60:
        return
    _audit_seen[key] = now
    store().add_event(level, f"مالک روی حساب «{acc['label']}» انجام داد: {what}", acc["owner_id"])
