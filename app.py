from __future__ import annotations

import atexit
import hmac
import logging
import os
import secrets
import threading
import time
import urllib.request
from datetime import datetime, timezone
from functools import wraps
from io import BytesIO
from urllib.parse import urlsplit

from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.middleware.proxy_fix import ProxyFix

from flask import Flask, jsonify, make_response, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from dotenv import load_dotenv

# Read the local .env before constructing Store/CloudRuntime; Render env vars remain authoritative.
load_dotenv()

from runtime import CloudRuntime, owner_can_access_account

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("borhan.web")

app = Flask(__name__)
_secret = os.getenv("SECRET_KEY", "").strip()
_is_production = bool(os.getenv("RENDER") or os.getenv("RENDER_EXTERNAL_URL"))
if not _secret and _is_production:
    raise RuntimeError("SECRET_KEY در محیط Render تنظیم نشده است")
if _is_production and len(_secret) < 32:
    raise RuntimeError("SECRET_KEY در Render باید حداقل ۳۲ نویسه تصادفی داشته باشد")
app.secret_key = _secret or secrets.token_urlsafe(48)
_cookie_secure = os.getenv("COOKIE_SECURE", "true" if _is_production else "false").lower() == "true"
app.config.update(
    SESSION_COOKIE_NAME="__Host-borhan_session" if _cookie_secure else "borhan_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_cookie_secure,
    SESSION_COOKIE_PATH="/",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
    MAX_CONTENT_LENGTH=128 * 1024,
    TRUSTED_HOSTS=[x.strip() for x in os.getenv("TRUSTED_HOSTS", "").split(",") if x.strip()] or None,
)
# Render terminates TLS and forwards original scheme/IP. Trust the single Render proxy hop.
if _is_production:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

_LOGIN_CSRF_SALT = "borhan-sk-login-csrf-v1"
_LOGIN_CSRF_MAX_AGE = 15 * 60

def _login_csrf_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app.secret_key, salt=_LOGIN_CSRF_SALT)

def _new_login_csrf_token() -> str:
    return _login_csrf_serializer().dumps({"purpose": "login", "nonce": secrets.token_urlsafe(24)})

def _valid_login_csrf_token(token: str) -> bool:
    if not token or len(token) > 2048:
        return False
    try:
        payload = _login_csrf_serializer().loads(token, max_age=_LOGIN_CSRF_MAX_AGE)
    except (BadSignature, TypeError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("purpose") == "login"
        and isinstance(payload.get("nonce"), str)
        and len(payload["nonce"]) >= 24
    )

def _same_origin_login_post() -> bool:
    expected_host = (request.host or "").lower().rstrip(".")
    expected_scheme = "https" if _is_production else request.scheme

    def matches(value: str) -> bool:
        try:
            parsed = urlsplit(value)
            if not parsed.netloc or parsed.username or parsed.password:
                return False
            return parsed.netloc.lower().rstrip(".") == expected_host and parsed.scheme.lower() == expected_scheme
        except (TypeError, ValueError):
            return False

    origin = request.headers.get("Origin", "").strip()
    if origin:
        return matches(origin)
    referer = request.headers.get("Referer", "").strip()
    if referer:
        return matches(referer)
    return request.headers.get("Sec-Fetch-Site", "").lower() == "same-origin"
if _is_production and len(os.getenv("DASHBOARD_PASSWORD", "")) < 12:
    raise RuntimeError("DASHBOARD_PASSWORD در Render باید حداقل ۱۲ نویسه داشته باشد")

runtime = CloudRuntime()
_runtime_start_lock = threading.Lock()
_runtime_started = False

_login_lock = threading.Lock()
_login_attempts: dict[str, tuple[int, float]] = {}
_api_rate_lock = threading.Lock()
_api_rate: dict[tuple[str, str], list[float]] = {}
LOGIN_MAX_ATTEMPTS = max(3, int(os.getenv("LOGIN_MAX_ATTEMPTS", "7")))
LOGIN_LOCK_SECONDS = max(60, int(os.getenv("LOGIN_LOCK_SECONDS", "900")))

_keepalive_stop = threading.Event()
_keepalive_thread: threading.Thread | None = None
_keepalive_lock = threading.Lock()
_keepalive_state = {
    "enabled": False, "url_configured": False, "interval_seconds": 240,
    "last_attempt_at": None, "last_success_at": None,
    "last_status_code": None, "last_error": "",
}


def ensure_runtime():
    global _runtime_started
    with _runtime_start_lock:
        if not _runtime_started and not app.config.get("TESTING"):
            runtime.start()
            _runtime_started = True


@app.before_request
def _start_runtime_for_request():
    ensure_runtime()


atexit.register(runtime.shutdown)


def current_user() -> dict:
    return {"id": session.get("user_id", ""), "username": session.get("username", ""),
            "role": session.get("role", "")}


def owner_auth_marker() -> str:
    secret = str(app.secret_key).encode("utf-8")
    password = os.getenv("DASHBOARD_PASSWORD", "").encode("utf-8")
    return hmac.new(secret, password, "sha256").hexdigest()


def owner_scope() -> str | None:
    user = current_user()
    return None if user["role"] == "owner" else user["id"]


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        role = session.get("role")
        user_id = session.get("user_id")
        authenticated = bool(user_id and role in {"owner", "admin"})
        # Deleting an admin must revoke their still-open browser sessions too.
        if authenticated and role == "owner":
            configured_owner = os.getenv("OWNER_USERNAME", "owner").strip().lower()
            authenticated = bool(
                session.get("username") == configured_owner
                and hmac.compare_digest(str(session.get("owner_auth_marker", "")), owner_auth_marker())
            )
        elif authenticated and role == "admin":
            try:
                member = runtime.store.get_member(str(session.get("username", "")))
                authenticated = bool(member and member.get("id") == user_id)
            except Exception:
                # A temporary Supabase outage must not silently destroy a valid
                # admin session; distinguish backend failure from a deleted admin.
                log.exception("could not validate active admin session")
                if request.path.startswith("/api/"):
                    return jsonify({"error": "بررسی حساب ادمین موقتاً ممکن نیست؛ کمی بعد دوباره تلاش کن"}), 503
                response = make_response(render_template(
                    "login.html",
                    error="ارتباط با پایگاه داده موقتاً برقرار نیست؛ صفحه را تازه کن.",
                    login_csrf=_new_login_csrf_token(),
                ), 503)
                return response
        if not authenticated:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "نشست ورود معتبر نیست؛ دوباره وارد شو"}), 401
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped


def owner_required(fn):
    @wraps(fn)
    @login_required
    def wrapped(*args, **kwargs):
        if session.get("role") != "owner":
            return jsonify({"error": "این بخش فقط برای مالک است"}), 403
        return fn(*args, **kwargs)
    return wrapped


def csrf_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            expected = session.get("csrf_token", "")
            supplied = request.headers.get("X-CSRF-Token", "") or request.form.get("csrf", "")
            if not expected or not supplied or not hmac.compare_digest(expected, supplied):
                return jsonify({"error": "درخواست نامعتبر است؛ صفحه را تازه کن"}), 403
        return fn(*args, **kwargs)
    return wrapped


def account_for_user(account_id: str):
    account = runtime.store.get_account(account_id)
    if not account:
        return None
    user = current_user()
    if user["role"] == "owner":
        return account if owner_can_access_account(account) else None
    return account if account.get("owner_id") == user["id"] else None


def default_account_id() -> str | None:
    rows = runtime.store.list_accounts(owner_scope())
    if not rows:
        return None
    if current_user()["role"] == "owner":
        preferred = next((a for a in rows if a.get("owner_id") == "owner"), None)
        return preferred["id"] if preferred else None
    return rows[0]["id"]


def scoped_account_rows(status_snapshot: dict | None = None) -> list[dict]:
    user = current_user()
    rows = runtime.store.list_accounts(owner_scope())
    snapshot = status_snapshot or runtime.status(owner_scope(), include_jobs=False)
    status_rows = {a.get("id"): a for a in snapshot.get("accounts", [])}
    members = {m["id"]: m["username"] for m in runtime.store.list_members()} if user["role"] == "owner" else {}
    result = []
    for row in rows:
        live = status_rows.get(row["id"], {})
        is_mine = row.get("owner_id") == user["id"]
        is_loaded = row["id"] in status_rows
        result.append({
            **row,
            "owner_access": bool(row.get("owner_access")),
            # Inactive accounts are intentionally lazy-loaded to protect Render Free RAM.
            # Keep their last persisted status visible until the context is opened again.
            "connected": bool(live.get("connected")) if is_loaded else row.get("status") == "connected",
            "runtime_loaded": is_loaded,
            "last_error": str(live.get("last_error", ""))[:240],
            "can_open_session": is_mine,
            "can_inspect": is_mine or (user["role"] == "owner" and owner_can_access_account(row)),
            "can_manage_session": is_mine or (user["role"] == "owner" and owner_can_access_account(row)),
            "can_toggle_owner_access": user["role"] == "admin" and is_mine,
            "owner_label": (
                ("مالک" if row.get("owner_id") == "owner" else members.get(row.get("owner_id"), "ادمین"))
                if user["role"] == "owner" else "حساب من"
            ),
        })
    return result


def scoped_job_rows() -> list[dict]:
    user = current_user()
    rows = runtime.store.list_jobs(owner_id=None)
    if user["role"] == "owner":
        accounts_by_id = {a["id"]: a for a in runtime.store.list_accounts()}
        return [job for job in rows if (
            not job.get("account_id") and job.get("owner_id") == "owner"
        ) or owner_can_access_account(accounts_by_id.get(job.get("account_id")))]
    account_ids = {a["id"] for a in runtime.store.list_accounts(user["id"])}
    return [job for job in rows if job.get("account_id") in account_ids
            or (job.get("owner_id") == user["id"] and not job.get("account_id"))]


def rate_limited(limit: int, window_seconds: int = 60):
    """Small per-process abuse guard for expensive Telegram operations."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            key = (str(session.get("user_id") or request.remote_addr or "unknown"), request.endpoint or fn.__name__)
            now = time.monotonic()
            with _api_rate_lock:
                recent = [stamp for stamp in _api_rate.get(key, []) if now - stamp < window_seconds]
                if len(recent) >= limit:
                    return jsonify({"error": "تعداد درخواست‌ها زیاد است؛ کمی بعد دوباره تلاش کن"}), 429
                recent.append(now)
                _api_rate[key] = recent
                if len(_api_rate) > 4096:
                    for old_key in list(_api_rate)[:1024]:
                        if not _api_rate[old_key] or now - _api_rate[old_key][-1] >= window_seconds:
                            _api_rate.pop(old_key, None)
            return fn(*args, **kwargs)
        return wrapped
    return decorate


def log_account_action(account: dict, message: str, level: str = "info"):
    """Keep owner actions visible to the affected admin as well as the owner."""
    actor = current_user()
    runtime.store.add_event(level, message, actor["id"])
    account_owner = str(account.get("owner_id", "owner"))
    if actor["role"] == "owner" and account_owner != "owner":
        runtime.store.add_event(level, f"اقدام مالک: {message}", account_owner)


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    if _is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob: data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "object-src 'none'; frame-src 'none'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health():
    # This endpoint is a liveness probe, not a dependency-readiness probe. It must
    # stay cheap and must not mark the whole web service unhealthy when Chromium
    # or Supabase is warming up or temporarily unavailable.
    loop_alive = bool(runtime.loop and runtime.loop.is_running())
    payload = {"ok": True, "service": "running", "runtime_loop_alive": loop_alive,
               "browser_started": bool(runtime.browser and runtime.browser.is_connected()),
               "time": datetime.now(timezone.utc).isoformat()}
    return jsonify(payload), 200


@app.get("/wake")
def wake():
    state = runtime.status()
    ready = bool(state.get("browser_started"))
    response = jsonify({"ok": True, "service": state.get("service", "starting"),
                        "browser_started": ready,
                        "message": "service is awake" if ready else "service is waking up"})
    response.headers["Retry-After"] = "10" if not ready else "0"
    return response, 200


def _login_blocked(ip: str) -> tuple[bool, int]:
    now = time.monotonic()
    with _login_lock:
        count, until = _login_attempts.get(ip, (0, 0.0))
        if until > now:
            return True, max(1, int(until - now))
        if until and until <= now:
            _login_attempts.pop(ip, None)
    return False, 0


def _record_login_failure(ip: str):
    now = time.monotonic()
    with _login_lock:
        count, until = _login_attempts.get(ip, (0, 0.0))
        count += 1
        _login_attempts[ip] = (count, now + LOGIN_LOCK_SECONDS if count >= LOGIN_MAX_ATTEMPTS else until)
        if len(_login_attempts) > 4096:
            for old_ip in list(_login_attempts)[:1024]:
                _login_attempts.pop(old_ip, None)


def _clear_login_failures(ip: str):
    with _login_lock:
        _login_attempts.pop(ip, None)


@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr or "unknown"
    if request.method == "GET":
        # Login CSRF is a short-lived signed token instead of a value stored in the
        # Flask session cookie. This avoids false 403s when a browser omits the
        # pre-auth session cookie, while same-origin validation still blocks CSRF.
        return render_template("login.html", error="", login_csrf=_new_login_csrf_token())

    supplied_csrf = request.form.get("login_csrf", "")
    if not _same_origin_login_post() or not _valid_login_csrf_token(supplied_csrf):
        log.warning("login request rejected by origin/CSRF validation from %s", ip)
        return render_template(
            "login.html",
            error="درخواست ورود نامعتبر یا منقضی شده است؛ صفحه ورود را تازه کن.",
            login_csrf=_new_login_csrf_token(),
        ), 403

    blocked, wait_seconds = _login_blocked(ip)
    if blocked:
        return render_template(
            "login.html",
            error=f"تلاش‌های زیاد؛ {wait_seconds} ثانیه دیگر دوباره امتحان کن.",
            login_csrf=_new_login_csrf_token(),
        ), 429

    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "")
    owner_username = os.getenv("OWNER_USERNAME", "owner").strip().lower()
    owner_password = os.getenv("DASHBOARD_PASSWORD", "")
    valid = False
    identity = None
    admin_record_found = False
    auth_failure_reason = "unknown"
    owner_username_match = bool(
        username and owner_username and hmac.compare_digest(username, owner_username)
    )

    if owner_username_match:
        if not owner_password:
            auth_failure_reason = "owner_password_not_configured"
        else:
            valid = hmac.compare_digest(owner_password.encode("utf-8"), password.encode("utf-8"))
            auth_failure_reason = "owner_password_match" if valid else "owner_password_mismatch"
        if valid:
            identity = {"id": "owner", "username": owner_username, "role": "owner"}
    elif username:
        try:
            member = runtime.store.get_member(username)
        except Exception:
            log.exception("admin credential lookup failed")
            return render_template(
                "login.html",
                error="ارتباط با پایگاه داده موقتاً برقرار نیست؛ کمی بعد دوباره تلاش کن.",
                login_csrf=_new_login_csrf_token(),
            ), 503
        admin_record_found = bool(member)
        if member:
            try:
                valid = check_password_hash(member["password_hash"], password)
            except (ValueError, TypeError, KeyError):
                valid = False
            auth_failure_reason = "admin_password_match" if valid else "admin_password_mismatch"
            if valid:
                identity = {"id": member["id"], "username": member["username"], "role": "admin"}
        else:
            auth_failure_reason = "admin_username_not_found"
    else:
        auth_failure_reason = "empty_username"

    if not valid or not identity:
        # Safe server-side diagnostics: no username, password, hash, cookie, or token is logged.
        log.warning(
            "login authentication rejected: reason=%s owner_username_match=%s "
            "owner_password_configured=%s admin_record_found=%s submitted_password_nonempty=%s",
            auth_failure_reason,
            owner_username_match,
            bool(owner_password),
            admin_record_found,
            bool(password),
        )
        _record_login_failure(ip)
        return render_template(
            "login.html",
            error="نام کاربری یا رمز عبور نادرست است.",
            login_csrf=_new_login_csrf_token(),
        ), 401

    log.info("login authentication accepted: role=%s", identity["role"])

    _clear_login_failures(ip)
    session.clear()  # Prevent session fixation.
    # Keep the session key consistent with current_user() and login_required().
    # Those functions read "user_id"; the previous code stored the identifier as "id",
    # which caused every successful login to be redirected straight back to /login.
    session["user_id"] = str(identity["id"])
    session["username"] = str(identity["username"])
    session["role"] = str(identity["role"])
    if identity["role"] == "owner":
        session["owner_auth_marker"] = owner_auth_marker()
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True
    log.info(
        "login session established: role=%s user_id_present=%s",
        session.get("role", ""),
        bool(session.get("user_id")),
    )
    response = redirect(url_for("index"))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/logout")
@login_required
@csrf_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def index():
    return render_template("index.html", csrf_token=session.get("csrf_token", ""),
                           user=current_user())


@app.get("/api/status")
@login_required
@rate_limited(30, 60)
def api_status():
    try:
        state = runtime.status(owner_scope(), include_jobs=False)
        with _keepalive_lock:
            state["keepalive"] = {
                "enabled": bool(_keepalive_state.get("enabled")),
                "interval_seconds": int(_keepalive_state.get("interval_seconds", 240)),
                "last_success_at": _keepalive_state.get("last_success_at"),
                "last_status_code": _keepalive_state.get("last_status_code"),
                "last_error": str(_keepalive_state.get("last_error", ""))[:160],
            }
        state["accounts"] = scoped_account_rows(state)
        visible_jobs = scoped_job_rows()
        state["jobs_total"] = len(visible_jobs)
        state["jobs_active"] = sum(1 for job in visible_jobs if job.get("status") == "active")
        state["user"] = current_user()
        return jsonify(state)
    except Exception:
        log.exception("status query failed")
        return jsonify({"service": "error", "error": "خواندن وضعیت سرویس ممکن نشد؛ گزارش فنی ثبت شد"}), 503


@app.route("/api/accounts", methods=["GET", "POST"])
@login_required
@rate_limited(60, 60)
def api_accounts():
    if request.method == "GET":
        return jsonify(scoped_account_rows())
    expected = session.get("csrf_token", "")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        return jsonify({"error": "درخواست نامعتبر است؛ صفحه را تازه کن"}), 403
    data = request.get_json(silent=True) or {}
    platform = str(data.get("platform", "telegram")).strip().lower()
    label = str(data.get("label", "")).strip()[:80]
    if platform != "telegram":
        return jsonify({"error": "در این نسخه فقط اتصال Telegram Web فعال است؛ اتصال پلتفرم‌های دیگر هنوز نیازمند کانکتور رسمی و تنظیمات اختصاصی است."}), 400
    if not label:
        return jsonify({"error": "برای تشخیص حساب، یک نام دلخواه وارد کن."}), 400
    try:
        account = runtime.store.create_account(current_user()["id"], platform, label)
        # Browser startup and Telegram Web navigation are asynchronous. Do not make
        # the browser wait on a long-running request that can become an HTTP 502.
        runtime.schedule_account_start(account)
        runtime.store.add_event("info", f"حساب جدید اضافه شد: {label}", current_user()["id"])
        account.update({"owner_access": False, "connected": False, "status": "starting"})
        return jsonify(account), 202
    except Exception as exc:
        log.exception("account creation failed")
        return jsonify({"error": "ساخت نشست آغاز نشد؛ وضعیت سرویس را بررسی کن."}), 503


@app.post("/api/accounts/<account_id>/owner-access")
@login_required
@csrf_required
@rate_limited(6, 60)
def api_account_owner_access(account_id: str):
    # Only the logged-in account holder may grant or revoke owner access.
    account = account_for_user(account_id)
    if not account or current_user()["role"] != "admin":
        return jsonify({"error": "فقط صاحب حساب ادمین می‌تواند این مجوز را تغییر دهد"}), 404
    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "مقدار مجوز نامعتبر است"}), 400
    try:
        if not runtime.store.update_account(account_id, {"owner_access": enabled}):
            return jsonify({"error": "ذخیره مجوز انجام نشد"}), 500
        state = "فعال شد" if enabled else "لغو شد"
        runtime.store.add_event("warning" if enabled else "info",
                                f"دسترسی مشاهده و مدیریت مالک برای حساب «{account['label']}» {state}.",
                                account["owner_id"])
        return jsonify({"ok": True, "owner_access": enabled})
    except Exception:
        log.exception("owner access permission update failed")
        return jsonify({"error": "ذخیره مجوز انجام نشد؛ گزارش فنی ثبت شد"}), 500


def _owned_login_account(account_id: str):
    """Login credentials and QR flows are available only to the account holder."""
    account = runtime.store.get_account(account_id)
    if not account or account.get("owner_id") != current_user().get("id"):
        return None
    return account


async def _execute_telegram_login(account: dict, operation: str, value: str):
    """Ensure the account browser exists, then perform one auth step in the runtime loop."""
    account_id = str(account["id"])
    telegram = runtime.get_telegram(account_id)
    if not telegram or not telegram.started:
        await runtime.add_telegram_account(account)
        telegram = runtime.get_telegram(account_id)
    if not telegram or not telegram.started:
        raise RuntimeError("مرورگر تلگرام آماده نشد")
    if operation == "phone":
        return await telegram.start_phone_login(value)
    if operation == "code":
        return await telegram.submit_login_code(value)
    if operation == "password":
        return await telegram.submit_2fa_password(value)
    raise RuntimeError("عملیات ورود ناشناخته است")


@app.get("/api/accounts/<account_id>/login-state")
@login_required
@rate_limited(30, 60)
def api_account_login_state(account_id: str):
    account = _owned_login_account(account_id)
    if not account:
        return jsonify({"error": "حساب پیدا نشد یا اجازه دسترسی نداری"}), 404

    queued = runtime.get_auth_operation_state(account_id)
    if queued:
        if queued.get("connected") or queued.get("stage") == "connected":
            runtime.unpin_account(account_id)
        else:
            runtime.pin_account(account_id, 180)
        if queued.get("stage") == "processing":
            response = jsonify(queued)
            response.headers["Retry-After"] = "2"
            return response, 202
        if queued.get("stage") in {"code", "password", "connected", "error", "unavailable"}:
            return jsonify(queued)

    cached = runtime.get_cached_login_state(account_id, max_age_seconds=3.0)
    if cached:
        if cached.get("connected"):
            runtime.unpin_account(account_id)
        else:
            runtime.pin_account(account_id, 180)
        return jsonify(cached)

    telegram = runtime.get_telegram(account_id)
    if not telegram or not telegram.started:
        runtime.pin_account(account_id, 180)
        try:
            runtime.schedule_account_start(account)
        except Exception:
            log.exception("could not schedule Telegram context startup")
        response = jsonify({"stage": "starting", "connected": False,
                            "message": "در حال آماده‌سازی مرورگر تلگرام…"})
        response.headers["Retry-After"] = "3"
        return response, 202

    if not runtime.schedule_login_state_refresh(account_id):
        response = jsonify({"stage": "loading", "connected": False,
                            "message": "وضعیت تلگرام در حال بررسی است…"})
        response.headers["Retry-After"] = "2"
        return response, 202
    response = jsonify({"stage": "loading", "connected": False,
                        "message": "وضعیت تلگرام در حال بررسی است…"})
    response.headers["Retry-After"] = "2"
    return response, 202


@app.post("/api/accounts/<account_id>/login/phone")
@login_required
@csrf_required
@rate_limited(3, 300)
def api_account_phone_login(account_id: str):
    account = _owned_login_account(account_id)
    if not account:
        return jsonify({"error": "حساب پیدا نشد یا اجازه دسترسی نداری"}), 404
    data = request.get_json(silent=True) or {}
    phone = str(data.get("phone", "")).strip()
    if not phone or len(phone) > 24:
        return jsonify({"error": "شماره تلفن معتبر با کد کشور وارد کن."}), 400
    scheduled = runtime.schedule_auth_operation(
        account_id, "phone", _execute_telegram_login(account, "phone", phone)
    )
    state = runtime.get_auth_operation_state(account_id) or {
        "stage": "processing", "connected": False,
        "message": "درخواست ورود در صف اجرا قرار گرفت…",
    }
    response = jsonify(state)
    response.headers["Retry-After"] = "2"
    return response, 202 if scheduled or state.get("stage") == "processing" else 503


@app.post("/api/accounts/<account_id>/login/code")
@login_required
@csrf_required
@rate_limited(5, 300)
def api_account_phone_code(account_id: str):
    account = _owned_login_account(account_id)
    if not account:
        return jsonify({"error": "حساب پیدا نشد یا اجازه دسترسی نداری"}), 404
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip()
    if not code or len(code) > 16:
        return jsonify({"error": "کد ورود معتبر وارد کن."}), 400
    scheduled = runtime.schedule_auth_operation(
        account_id, "code", _execute_telegram_login(account, "code", code)
    )
    state = runtime.get_auth_operation_state(account_id) or {
        "stage": "processing", "connected": False, "message": "در حال بررسی کد ورود…",
    }
    response = jsonify(state)
    response.headers["Retry-After"] = "2"
    return response, 202 if scheduled or state.get("stage") == "processing" else 503


@app.post("/api/accounts/<account_id>/login/password")
@login_required
@csrf_required
@rate_limited(5, 300)
def api_account_2fa_password(account_id: str):
    account = _owned_login_account(account_id)
    if not account:
        return jsonify({"error": "حساب پیدا نشد یا اجازه دسترسی نداری"}), 404
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", ""))
    if not password or len(password) > 256:
        return jsonify({"error": "رمز دومرحله‌ای معتبر وارد کن."}), 400
    scheduled = runtime.schedule_auth_operation(
        account_id, "password", _execute_telegram_login(account, "password", password)
    )
    state = runtime.get_auth_operation_state(account_id) or {
        "stage": "processing", "connected": False, "message": "در حال بررسی رمز دومرحله‌ای…",
    }
    response = jsonify(state)
    response.headers["Retry-After"] = "2"
    return response, 202 if scheduled or state.get("stage") == "processing" else 503


@app.get("/api/accounts/<account_id>/qr.png")
@login_required
@rate_limited(25, 60)
def api_account_qr(account_id: str):
    account = _owned_login_account(account_id)
    if not account:
        return jsonify({"error": "QR فقط برای صاحب حساب قابل مشاهده است"}), 404

    runtime.pin_account(account_id, 180)
    cached_login = runtime.get_cached_login_state(account_id, max_age_seconds=20.0)
    live = next((row for row in runtime.status(include_jobs=False).get("accounts", []) if row.get("id") == account_id), None)
    if (live and live.get("connected")) or (cached_login and cached_login.get("connected")):
        return "already connected", 204

    telegram = runtime.get_telegram(account_id)
    if not telegram or not telegram.started:
        try:
            runtime.schedule_account_start(account)
        except Exception:
            log.exception("could not schedule Telegram context startup for QR")
        response = jsonify({"error": "مرورگر در حال آماده‌سازی است؛ چند ثانیه دیگر تلاش کن."})
        response.headers["Retry-After"] = "3"
        return response, 202

    image = runtime.get_cached_qr(account_id)
    if image is None:
        runtime.schedule_qr_refresh(account_id)
        response = jsonify({"error": "در حال آماده‌سازی یا تازه‌سازی تصویر QR…"})
        response.headers["Retry-After"] = "3"
        return response, 202
    response = make_response(send_file(BytesIO(image), mimetype="image/png"))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.post("/api/accounts/<account_id>/restart")
@login_required
@csrf_required
@rate_limited(5, 60)
def api_account_restart(account_id: str):
    account = account_for_user(account_id)
    if not account:
        return jsonify({"error": "حساب پیدا نشد یا اجازه دسترسی نداری"}), 404
    try:
        future = runtime.schedule(runtime.restart_telegram_account(account_id))
        def _record_restart(done):
            try:
                done.result()
                log_account_action(account, f"نشست دوباره راه‌اندازی شد: {account['label']}", "info")
            except Exception:
                log.exception("Telegram account restart failed (%s)", account_id)
        future.add_done_callback(_record_restart)
        response = jsonify({"ok": True, "status": "restarting", "message": "راه‌اندازی مجدد در پس‌زمینه آغاز شد."})
        response.headers["Retry-After"] = "3"
        return response, 202
    except Exception:
        log.exception("could not schedule account restart (%s)", account_id)
        return jsonify({"error": "راه‌اندازی مجدد در صف قرار نگرفت؛ وضعیت سرویس را بررسی کن."}), 503


@app.delete("/api/accounts/<account_id>")
@login_required
@csrf_required
@rate_limited(5, 60)
def api_account_delete(account_id: str):
    account = account_for_user(account_id)
    if not account:
        return jsonify({"error": "حساب پیدا نشد یا اجازه دسترسی نداری"}), 404
    try:
        runtime.submit(runtime.remove_telegram_account(account_id), timeout=35)
        # Delete every job attached to this account, including delegated jobs
        # created by the owner. Store.delete_account repeats this cleanup for Supabase.
        for job in runtime.store.list_jobs(owner_id=None):
            if job.get("account_id") == account_id:
                runtime.store.delete_job(job["id"], None)
        runtime.store.delete_account(account_id)
        log_account_action(account, f"حساب حذف شد: {account['label']}", "warning")
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)[:300]}), 400


# Compatibility endpoint for the old single-account QR view.
@app.get("/api/qr.png")
@login_required
def api_qr():
    account_id = default_account_id()
    if not account_id:
        return "account not found", 404
    return redirect(url_for("api_account_qr", account_id=account_id))


@app.post("/api/browser/restart")
@login_required
@csrf_required
@rate_limited(5, 60)
def api_browser_restart():
    account_id = default_account_id()
    if not account_id:
        return jsonify({"error": "هیچ حسابی ثبت نشده است"}), 404
    try:
        runtime.schedule(runtime.restart_telegram_account(account_id))
        response = jsonify({"ok": True, "status": "restarting"})
        response.headers["Retry-After"] = "3"
        return response, 202
    except Exception:
        log.exception("could not schedule browser restart (%s)", account_id)
        return jsonify({"error": "راه‌اندازی مجدد در صف قرار نگرفت؛ وضعیت سرویس را بررسی کن."}), 503


def validate_job(payload):
    name = str(payload.get("name", "")).strip()[:80]
    target = str(payload.get("target", "")).strip()[:160]
    action = str(payload.get("action", "send")).strip()
    content = str(payload.get("payload", "")).strip()[:1500]
    account_id = str(payload.get("account_id", "")).strip()
    try:
        interval = int(payload.get("interval_minutes", 5)) * 60
        max_runs_raw = payload.get("max_runs")
        max_runs = None if max_runs_raw in (None, "", 0, "0") else int(max_runs_raw)
    except (TypeError, ValueError):
        raise ValueError("فاصله یا تعداد اجرا نامعتبر است")
    minimum = max(60, int(os.getenv("MIN_JOB_INTERVAL_SECONDS", "60")))
    if not name or not target or not content:
        raise ValueError("نام، مقصد و متن عملیات الزامی است")
    if action not in {"send", "click"}:
        raise ValueError("نوع عملیات نامعتبر است")
    if interval < minimum or interval > 86400:
        raise ValueError(f"فاصله باید بین {max(1, minimum // 60)} دقیقه و ۲۴ ساعت باشد")
    if max_runs is not None and not (1 <= max_runs <= 100000):
        raise ValueError("تعداد اجرا باید بین ۱ تا ۱۰۰۰۰۰ باشد")
    return name, target, action, content, interval, max_runs, account_id


@app.get("/api/jobs")
@login_required
@rate_limited(30, 60)
def api_jobs():
    # Owner and admin see only the jobs permitted by their account scopes.
    return jsonify(scoped_job_rows())


@app.post("/api/jobs")
@login_required
@csrf_required
@rate_limited(10, 60)
def api_create_job():
    try:
        name, target, action, content, interval, max_runs, account_id = validate_job(request.get_json(silent=True) or {})
        account_id = account_id or default_account_id()
        if not account_id or not account_for_user(account_id):
            raise ValueError("حساب انتخاب‌شده متعلق به شما نیست یا صاحب آن دسترسی مالک را فعال نکرده است")
        max_active_jobs = max(1, int(os.getenv("MAX_ACTIVE_JOBS_PER_ACCOUNT", "20")))
        max_total_jobs = max(max_active_jobs, int(os.getenv("MAX_TOTAL_JOBS_PER_ACCOUNT", "200")))
        all_jobs = [job for job in runtime.store.list_jobs(owner_id=None) if job.get("account_id") == account_id]
        if sum(1 for job in all_jobs if job.get("status") == "active") >= max_active_jobs:
            raise ValueError(f"برای هر حساب حداکثر {max_active_jobs} زمان‌بندی فعال مجاز است")
        if len(all_jobs) >= max_total_jobs:
            raise ValueError(f"برای هر حساب حداکثر {max_total_jobs} زمان‌بندی ذخیره‌شده مجاز است")
        row = runtime.store.create_job(name=name, target=target, action=action, payload=content,
                                       interval_seconds=interval, max_runs=max_runs,
                                       owner_id=current_user()["id"], account_id=account_id)
        runtime.store.add_event("success", f"زمان‌بندی ساخته شد: {name}", current_user()["id"])
        if current_user()["role"] == "owner":
            account = runtime.store.get_account(account_id)
            if account and account.get("owner_id") != "owner":
                runtime.store.add_event("info", f"مالک زمان‌بندی «{name}» را برای حساب شما ساخت.", account["owner_id"])
        return jsonify(row), 201
    except Exception as exc:
        return jsonify({"error": str(exc)[:300]}), 400


@app.post("/api/jobs/<job_id>/<action>")
@login_required
@csrf_required
@rate_limited(30, 60)
def api_job_action(job_id: str, action: str):
    user = current_user()
    try:
        affected_job = runtime.store.get_job(job_id, None)
        if not affected_job:
            return jsonify({"error": "کار پیدا نشد یا اجازه دسترسی نداری"}), 404

        if user["role"] == "admin":
            linked_account = runtime.store.get_account(affected_job.get("account_id")) if affected_job.get("account_id") else None
            owns_account = bool(linked_account and linked_account.get("owner_id") == user["id"])
            legacy_unlinked_job = not affected_job.get("account_id") and affected_job.get("owner_id") == user["id"]
            if not (owns_account or legacy_unlinked_job):
                return jsonify({"error": "کار پیدا نشد یا اجازه دسترسی نداری"}), 404
            job_scope = affected_job.get("owner_id")
            if action == "resume" and job_scope == "owner" and linked_account and not linked_account.get("owner_access"):
                return jsonify({"error": "مجوز مالک برای این حساب لغو شده است؛ این زمان‌بندی قابل ادامه نیست"}), 403
        else:
            linked_account = runtime.store.get_account(affected_job.get("account_id")) if affected_job.get("account_id") else None
            if linked_account and not owner_can_access_account(linked_account):
                return jsonify({"error": "این حساب دسترسی مدیریتی مالک را فعال نکرده است"}), 403
            if not linked_account and affected_job.get("owner_id") != "owner":
                return jsonify({"error": "کار پیدا نشد یا اجازه دسترسی نداری"}), 404
            job_scope = None

        if action == "pause":
            ok = runtime.store.set_job_status(job_id, "paused", job_scope)
        elif action == "resume":
            if user["role"] == "owner" and linked_account and not owner_can_access_account(linked_account):
                return jsonify({"error": "دسترسی مدیریتی مالک برای این حساب فعال نیست"}), 403
            ok = runtime.store.set_job_status(job_id, "active", job_scope)
        elif action == "delete":
            ok = runtime.store.delete_job(job_id, job_scope)
        else:
            return jsonify({"error": "عملیات نامعتبر است"}), 400
        if not ok:
            return jsonify({"error": "کار پیدا نشد یا اجازه دسترسی نداری"}), 404

        runtime.store.add_event("info", f"زمان‌بندی {job_id}: {action}", user["id"])
        if linked_account and linked_account.get("owner_id") != user["id"]:
            runtime.store.add_event("info", f"ادمین {user['username']} زمان‌بندی «{affected_job.get('name', job_id)}» را {action} کرد.",
                                    linked_account["owner_id"])
        return jsonify({"ok": True})
    except Exception:
        log.exception("scheduled job action failed")
        return jsonify({"error": "تغییر زمان‌بندی انجام نشد؛ گزارش فنی ثبت شد"}), 500


@app.post("/api/test-send")
@login_required
@csrf_required
@rate_limited(8, 60)
def api_test_send():
    data = request.get_json(silent=True) or {}
    target = str(data.get("target", "")).strip()[:160]
    message_text = str(data.get("text", "")).strip()
    account_id = str(data.get("account_id", "")).strip() or default_account_id()
    try:
        if not target or not message_text or len(message_text) > 1500:
            raise ValueError("مقصد و متن لازم است؛ متن حداکثر ۱۵۰۰ نویسه باشد")
        account = account_for_user(account_id) if account_id else None
        if not account:
            raise ValueError("حساب انتخاب‌شده معتبر نیست یا اجازه دسترسی مالک فعال نشده است")
        telegram = runtime.get_telegram(account_id)
        if not telegram:
            raise ValueError("نشست مرورگر این حساب آماده نیست")
        runtime.submit(telegram.send_message(target, message_text), timeout=45)
        log_account_action(account, f"پیام از داشبورد به {target} ارسال شد", "success")
        return jsonify({"ok": True, "target": target})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        log.exception("Telegram message send failed")
        return jsonify({"error": "ارسال پیام انجام نشد؛ اتصال حساب و گزارش فنی را بررسی کن"}), 503


@app.get("/api/messages")
@login_required
@rate_limited(20, 60)
def api_messages():
    target = request.args.get("target", "").strip()[:160]
    account_id = request.args.get("account_id", "").strip() or default_account_id()
    if not target:
        return jsonify({"error": "نام کاربری یا مقصد گفتگو را وارد کن"}), 400
    account = account_for_user(account_id) if account_id else None
    if not account:
        return jsonify({"error": "حساب انتخاب‌شده معتبر نیست یا صاحب آن دسترسی بررسی مالک را فعال نکرده است"}), 403
    try:
        telegram = runtime.get_telegram(account_id)
        if not telegram:
            raise RuntimeError("Telegram browser session is unavailable")
        messages = runtime.submit(telegram.read_recent_messages(target), timeout=45)
        if current_user()["role"] == "owner" and account.get("owner_id") != "owner":
            log_account_action(account, f"مالک پیام‌های اخیر گفتگو با {target} را مشاهده کرد", "info")
        return jsonify({"messages": messages, "target": target, "account_id": account_id})
    except Exception:
        log.exception("Telegram messages read failed")
        return jsonify({"error": "خواندن گفتگو انجام نشد؛ مقصد و اتصال حساب را بررسی کن"}), 503


@app.route("/api/admins", methods=["GET", "POST"])
@owner_required
@rate_limited(10, 60)
def api_admins():
    if request.method == "GET":
        return jsonify(runtime.store.list_members())
    expected = session.get("csrf_token", "")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        return jsonify({"error": "درخواست نامعتبر است"}), 403
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip().lower()
    password = str(data.get("password", ""))
    owner_username = os.getenv("OWNER_USERNAME", "owner").strip().lower()
    if not username or len(username) < 3 or len(username) > 32 or not username.replace("_", "").isalnum():
        return jsonify({"error": "نام کاربری باید ۳ تا ۳۲ نویسه و فقط شامل حروف، عدد و _ باشد"}), 400
    if username == owner_username:
        return jsonify({"error": "این نام کاربری برای مالک رزرو شده است"}), 400
    if len(password) < 12:
        return jsonify({"error": "رمز ادمین باید حداقل ۱۲ نویسه باشد"}), 400
    try:
        row = runtime.store.create_member(username, generate_password_hash(password, method="scrypt"))
        runtime.store.add_event("info", f"ادمین جدید ساخته شد: {username}", "owner")
        return jsonify(row), 201
    except Exception as exc:
        return jsonify({"error": str(exc)[:300]}), 400


@app.delete("/api/admins/<member_id>")
@owner_required
@csrf_required
@rate_limited(5, 60)
def api_admin_delete(member_id: str):
    try:
        member_accounts = [a for a in runtime.store.list_accounts(member_id)]
        for account in member_accounts:
            runtime.submit(runtime.remove_telegram_account(account["id"]), timeout=30)
            runtime.store.delete_runtime_state(f"telegram_web_storage_state:{account['id']}")
            # Store.delete_member has no FK cascade in Supabase; remove orphan sessions explicitly.
            runtime.store.delete_account(account["id"])
        all_jobs = runtime.store.list_jobs(owner_id=None)
        for job in all_jobs:
            if job.get("owner_id") == member_id or job.get("account_id") in {a["id"] for a in member_accounts}:
                runtime.store.delete_job(job["id"], None)
        ok = runtime.store.delete_member(member_id)
        if not ok:
            return jsonify({"error": "ادمین پیدا نشد"}), 404
        runtime.store.add_event("warning", f"ادمین حذف شد: {member_id}", "owner")
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)[:300]}), 400


@app.post("/hooks/send")
@rate_limited(10, 60)
def external_send_hook():
    """Authenticated server-to-server send endpoint; not a Telegram Bot API webhook."""
    expected = os.getenv("WEBHOOK_TOKEN", "").strip()
    supplied = request.headers.get("X-Webhook-Token", "").strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    target = str(data.get("target", "")).strip()
    text = str(data.get("text", "")).strip()
    account_id = str(data.get("account_id", "")).strip() or default_account_id_for_owner()
    if not target or not text or len(text) > 1500:
        return jsonify({"error": "target and text are required; text max is 1500 chars"}), 400
    account = runtime.store.get_account(account_id) if account_id else None
    if not account or account.get("owner_id") != "owner":
        return jsonify({"error": "owner Telegram account not found"}), 404
    try:
        runtime.wait_for_browser(timeout=int(os.getenv("WAKE_WAIT_SECONDS", "55")),
                                  require_connected=True, account_id=account_id)
        telegram = runtime.get_telegram(account_id)
        result = runtime.submit(telegram.send_message(target, text), timeout=45)
        runtime.store.add_event("success", f"پیام webhook به {target} ارسال شد", "owner")
        return jsonify(result)
    except TimeoutError as exc:
        response = jsonify({"ok": False, "retry": True, "error": str(exc)})
        response.headers["Retry-After"] = "10"
        return response, 503
    except Exception as exc:
        log.exception("webhook send failed")
        return jsonify({"error": str(exc)[:300]}), 400


def default_account_id_for_owner() -> str | None:
    rows = runtime.store.list_accounts("owner")
    return rows[0]["id"] if rows else None


@app.get("/api/events")
@login_required
@rate_limited(30, 60)
def api_events():
    return jsonify(runtime.store.list_events(80, owner_scope()))


def _keepalive_worker(url: str, interval: int, timeout: int):
    """Best-effort request through Render's public ingress; it cannot override platform lifecycle rules."""
    log.info("HTTP keepalive started: interval=%ss", interval)
    # Give Gunicorn a moment to begin accepting traffic before the first self-probe.
    if _keepalive_stop.wait(min(15, interval)):
        return
    while not _keepalive_stop.is_set():
        with _keepalive_lock:
            _keepalive_state["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
            _keepalive_state["last_error"] = ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "BorhanSK-Health/1.0",
                "Cache-Control": "no-cache", "Accept": "application/json"}, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status = int(response.status)
                response.read(512)
            with _keepalive_lock:
                _keepalive_state["last_status_code"] = status
                if 200 <= status < 300:
                    _keepalive_state["last_success_at"] = datetime.now(timezone.utc).isoformat()
                    log.info("HTTP keepalive succeeded: %s returned %s", url, status)
                else:
                    _keepalive_state["last_error"] = f"health endpoint returned HTTP {status}"
        except Exception as exc:
            with _keepalive_lock:
                _keepalive_state["last_error"] = str(exc)[:300]
            log.warning("keepalive request failed: %s", exc)
        if _keepalive_stop.wait(interval):
            return


def start_keepalive():
    global _keepalive_thread
    enabled = os.getenv("KEEPALIVE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    base = (os.getenv("KEEPALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    interval = max(120, int(os.getenv("KEEPALIVE_INTERVAL_SECONDS", "240")))
    timeout = max(3, min(12, int(os.getenv("KEEPALIVE_TIMEOUT_SECONDS", "8"))))
    url = f"{base}/health" if base else ""
    with _keepalive_lock:
        _keepalive_state.update(enabled=bool(enabled and url), url_configured=bool(url), interval_seconds=interval)
    if not enabled:
        log.info("HTTP keepalive disabled by KEEPALIVE_ENABLED")
        return
    if not url:
        log.warning("HTTP keepalive not started: KEEPALIVE_URL/RENDER_EXTERNAL_URL is not configured")
        return
    with _keepalive_lock:
        if _keepalive_thread and _keepalive_thread.is_alive():
            return
        _keepalive_stop.clear()
        _keepalive_thread = threading.Thread(target=_keepalive_worker, args=(url, interval, timeout),
                                             name="borhan-health-keepalive", daemon=True)
        _keepalive_thread.start()


def stop_keepalive():
    _keepalive_stop.set()
    if _keepalive_thread and _keepalive_thread.is_alive():
        _keepalive_thread.join(timeout=2)


start_keepalive()
atexit.register(stop_keepalive)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True, debug=False)
