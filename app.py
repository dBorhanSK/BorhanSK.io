from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading
import atexit
import urllib.request
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, abort, jsonify, make_response, redirect, render_template, request, send_file, session, url_for
from io import BytesIO

from runtime import CloudRuntime

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("meow.web")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
    MAX_CONTENT_LENGTH=128 * 1024,
)

runtime = CloudRuntime()
_runtime_start_lock = threading.Lock()
_runtime_started = False

# Best-effort HTTP keepalive. On Render, a request is sent to the service's own
# public URL so it arrives through Render's public ingress (unlike an internal
# asyncio heartbeat, which only proves that the Python process is alive).
_keepalive_stop = threading.Event()
_keepalive_thread: threading.Thread | None = None
_keepalive_lock = threading.Lock()
_keepalive_state = {
    "enabled": False,
    "url_configured": False,
    "interval_seconds": 240,
    "last_attempt_at": None,
    "last_success_at": None,
    "last_status_code": None,
    "last_error": "",
}


def ensure_runtime():
    global _runtime_started
    with _runtime_start_lock:
        if not _runtime_started and not app.config.get("TESTING"):
            runtime.start()
            _runtime_started = True


ensure_runtime()
atexit.register(runtime.shutdown)


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "برای ادامه وارد شو"}), 401
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped


def csrf_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            expected = session.get("csrf_token", "")
            supplied = request.headers.get("X-CSRF-Token", "") or request.form.get("csrf", "")
            if not expected or not hmac.compare_digest(expected, supplied):
                return jsonify({"error": "درخواست نامعتبر است؛ صفحه را تازه کن"}), 403
        return fn(*args, **kwargs)
    return wrapped


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health():
    # Lightweight Render health endpoint. The keepalive worker calls this route
    # through RENDER_EXTERNAL_URL at a configurable interval.
    state = dict(runtime._status) if _runtime_started else {"service": "starting"}
    ok = state.get("service") in {"online", "starting"}
    with _keepalive_lock:
        keepalive = dict(_keepalive_state)
    return jsonify({
        "ok": ok,
        "service": state.get("service"),
        "storage": runtime.store.backend,
        "telegram_connected": state.get("telegram_connected", False),
        "session_saved_at": state.get("session_saved_at"),
        "time": datetime.now(timezone.utc).isoformat(),
        "keepalive": keepalive,
    }), (200 if ok else 503)


@app.get("/wake")
def wake():
    """Lightweight inbound route: Render wakes the Free instance when this URL is requested."""
    state = dict(runtime._status)
    ready = bool(state.get("browser_started"))
    response = jsonify({
        "ok": True,
        "service": state.get("service", "starting"),
        "storage": runtime.store.backend,
        "browser_started": ready,
        "telegram_connected": state.get("telegram_connected", False),
        "message": "service is awake" if ready else "service is waking up; retry shortly",
    })
    response.headers["Retry-After"] = "10" if not ready else "0"
    # Always 200 so simple external uptime monitors can use this path as a wake ping.
    return response, 200


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        expected = os.getenv("DASHBOARD_PASSWORD", "")
        provided = request.form.get("password", "")
        if not expected:
            return render_template("login.html", error="روی Render باید DASHBOARD_PASSWORD تنظیم شود."), 503
        if hmac.compare_digest(expected, provided):
            session.clear()
            session["authenticated"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True
            return redirect(url_for("index"))
        return render_template("login.html", error="رمز عبور نادرست است."), 401
    return render_template("login.html", error="")


@app.post("/logout")
@login_required
@csrf_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def index():
    return render_template("index.html", csrf_token=session.get("csrf_token", ""))


@app.get("/api/status")
@login_required
def api_status():
    try:
        state = runtime.status()
        if runtime.telegram and runtime.loop:
            state["telegram"] = runtime.submit(runtime.telegram.status(), timeout=10)
        return jsonify(state)
    except Exception as exc:
        return jsonify({"service": "error", "error": str(exc)}), 503


@app.get("/api/qr.png")
@login_required
def api_qr():
    try:
        if not runtime.telegram:
            return "browser is starting", 503
        state = runtime.submit(runtime.telegram.status(), timeout=10)
        if state.get("connected"):
            return "already connected", 204
        image = runtime.submit(runtime.telegram.screenshot(), timeout=15)
        response = make_response(send_file(BytesIO(image), mimetype="image/png"))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response
    except Exception as exc:
        log.exception("QR screenshot failed")
        return jsonify({"error": str(exc)}), 503


@app.post("/api/browser/restart")
@login_required
@csrf_required
def api_browser_restart():
    try:
        state = runtime.submit(runtime.telegram.restart_page(), timeout=60)
        runtime.store.add_event("info", "صفحه Telegram Web دوباره بارگذاری شد")
        return jsonify({"ok": True, "telegram": state})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


def validate_job(payload):
    name = str(payload.get("name", "")).strip()[:80]
    target = str(payload.get("target", "")).strip()[:160]
    action = str(payload.get("action", "send")).strip()
    content = str(payload.get("payload", "")).strip()[:1500]
    try:
        interval = int(payload.get("interval_minutes", 5)) * 60
    except (TypeError, ValueError):
        raise ValueError("فاصله زمانی نامعتبر است")
    max_runs_raw = payload.get("max_runs")
    max_runs = None if max_runs_raw in (None, "", 0, "0") else int(max_runs_raw)
    minimum = int(os.getenv("MIN_JOB_INTERVAL_SECONDS", "60"))
    if not name or not target or not content:
        raise ValueError("نام، مقصد و متن عملیات الزامی است")
    if action not in {"send", "click"}:
        raise ValueError("نوع عملیات نامعتبر است")
    if interval < minimum or interval > 86400:
        raise ValueError(f"فاصله باید بین {max(1, minimum // 60)} دقیقه و ۲۴ ساعت باشد")
    if max_runs is not None and not (1 <= max_runs <= 100000):
        raise ValueError("تعداد اجرا باید بین ۱ تا ۱۰۰۰۰۰ باشد")
    return name, target, action, content, interval, max_runs


@app.get("/api/jobs")
@login_required
def api_jobs():
    return jsonify(runtime.store.list_jobs())


@app.post("/api/jobs")
@login_required
@csrf_required
def api_create_job():
    try:
        name, target, action, content, interval, max_runs = validate_job(request.get_json(silent=True) or {})
        row = runtime.store.create_job(name=name, target=target, action=action, payload=content,
                                       interval_seconds=interval, max_runs=max_runs)
        runtime.store.add_event("success", f"کار جدید ساخته شد: {name}")
        return jsonify(row), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/jobs/<job_id>/<action>")
@login_required
@csrf_required
def api_job_action(job_id: str, action: str):
    try:
        if action == "pause":
            ok = runtime.store.set_job_status(job_id, "paused")
        elif action == "resume":
            ok = runtime.store.set_job_status(job_id, "active")
        elif action == "delete":
            ok = runtime.store.delete_job(job_id)
        else:
            return jsonify({"error": "عملیات نامعتبر است"}), 400
        if not ok:
            return jsonify({"error": "کار پیدا نشد"}), 404
        runtime.store.add_event("info", f"کار {job_id}: {action}")
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/test-send")
@login_required
@csrf_required
def api_test_send():
    data = request.get_json(silent=True) or {}
    target = str(data.get("target", "")).strip()
    text = str(data.get("text", "")).strip()
    try:
        if not target or not text:
            raise ValueError("مقصد و متن پیام لازم است")
        result = runtime.submit(runtime.telegram.send_message(target, text), timeout=40)
        runtime.store.add_event("success", f"ارسال آزمایشی به {target} انجام شد")
        return jsonify(result)
    except Exception as exc:
        runtime.store.add_event("error", f"ارسال آزمایشی ناموفق بود: {exc}")
        return jsonify({"error": str(exc)}), 400


@app.get("/api/messages")
@login_required
def api_messages():
    target = request.args.get("target", "").strip()
    if not target:
        return jsonify({"error": "target is required"}), 400
    try:
        messages = runtime.submit(runtime.telegram.read_recent_messages(target), timeout=40)
        return jsonify({"messages": messages})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/hooks/send")
def external_send_hook():
    """Optional server-to-server webhook. This is NOT a Telegram Bot API webhook."""
    expected = os.getenv("WEBHOOK_TOKEN", "").strip()
    supplied = request.headers.get("X-Webhook-Token", "").strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    target = str(data.get("target", "")).strip()
    text = str(data.get("text", "")).strip()
    if not target or not text or len(text) > 1500:
        return jsonify({"error": "target and text are required; text max is 1500 chars"}), 400
    try:
        runtime.wait_for_browser(timeout=int(os.getenv("WAKE_WAIT_SECONDS", "55")), require_connected=True)
        if not runtime.telegram:
            raise RuntimeError("مرورگر تلگرام آماده نیست؛ چند لحظه دیگر دوباره درخواست را بفرست")
        result = runtime.submit(runtime.telegram.send_message(target, text), timeout=40)
        runtime.store.add_event("success", f"پیام از webhook به {target} ارسال شد")
        return jsonify(result)
    except TimeoutError as exc:
        try:
            runtime.store.add_event("warning", f"webhook needs retry after wake: {exc}")
        except Exception:
            pass
        response = jsonify({"ok": False, "retry": True, "error": str(exc)})
        response.headers["Retry-After"] = "10"
        return response, 503
    except Exception as exc:
        try:
            runtime.store.add_event("error", f"webhook send failed: {exc}")
        except Exception:
            pass
        return jsonify({"error": str(exc)}), 400


@app.get("/api/events")
@login_required
def api_events():
    return jsonify(runtime.store.list_events(80))


def _keepalive_worker(url: str, interval: int, timeout: int):
    """Periodically hit this service via its public Render URL.

    This is best-effort only: Render controls instance lifecycle, and no
    application-level ping can guarantee permanent uptime on a Free instance.
    """
    log.info("HTTP keepalive started: url=%s interval=%ss", url, interval)
    while not _keepalive_stop.wait(interval):
        attempted = datetime.now(timezone.utc).isoformat()
        with _keepalive_lock:
            _keepalive_state["last_attempt_at"] = attempted
            _keepalive_state["last_error"] = ""
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "MeowStudio-KeepAlive/1.0",
                    "Cache-Control": "no-cache",
                    "Accept": "application/json",
                },
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status_code = int(response.status)
                response.read(512)
            if 200 <= status_code < 300:
                with _keepalive_lock:
                    _keepalive_state["last_success_at"] = datetime.now(timezone.utc).isoformat()
                    _keepalive_state["last_status_code"] = status_code
                    _keepalive_state["last_error"] = ""
                log.info("HTTP keepalive OK (%s)", status_code)
            else:
                message = f"health endpoint returned HTTP {status_code}"
                with _keepalive_lock:
                    _keepalive_state["last_status_code"] = status_code
                    _keepalive_state["last_error"] = message
                log.warning("HTTP keepalive failed: %s", message)
        except Exception as exc:
            with _keepalive_lock:
                _keepalive_state["last_error"] = str(exc)[:300]
            log.warning("HTTP keepalive request failed: %s", exc)


def start_keepalive():
    global _keepalive_thread
    enabled = os.getenv("KEEPALIVE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    base_url = (os.getenv("KEEPALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    interval = max(60, int(os.getenv("KEEPALIVE_INTERVAL_SECONDS", "240")))
    timeout = max(3, min(30, int(os.getenv("KEEPALIVE_TIMEOUT_SECONDS", "15"))))
    health_url = f"{base_url}/health" if base_url else ""

    with _keepalive_lock:
        _keepalive_state.update(
            enabled=bool(enabled and health_url),
            url_configured=bool(health_url),
            interval_seconds=interval,
        )

    if not enabled:
        log.info("HTTP keepalive disabled by KEEPALIVE_ENABLED")
        return
    if not health_url:
        log.info("HTTP keepalive not started: RENDER_EXTERNAL_URL/KEEPALIVE_URL is not configured")
        return

    with _keepalive_lock:
        if _keepalive_thread and _keepalive_thread.is_alive():
            return
        _keepalive_stop.clear()
        _keepalive_thread = threading.Thread(
            target=_keepalive_worker,
            args=(health_url, interval, timeout),
            name="meow-http-keepalive",
            daemon=True,
        )
        _keepalive_thread.start()


def stop_keepalive():
    _keepalive_stop.set()
    thread = _keepalive_thread
    if thread and thread.is_alive():
        thread.join(timeout=2)


# Start for both `python app.py` and the production `gunicorn app:app` entrypoint.
start_keepalive()
atexit.register(stop_keepalive)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True, debug=False)
