"""Authenticated browser bridge to the official Telegram Desktop window."""
from __future__ import annotations

import os
import secrets
import stat
import time
from pathlib import Path

from flask import Blueprint, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from deps import account_for, audit, err, hub, limited, login_required

bp = Blueprint("desktop", __name__, url_prefix="/api/accounts/<account_id>/desktop")


def cleanup_expired_uploads() -> int:
    """Remove old temporary files uploaded for sending through Telegram Desktop.

    This deliberately does not touch Telegram profiles, downloaded files, or
    Supabase rows. Set UPLOAD_RETENTION_HOURS=0 to disable the cleanup.
    """
    try:
        retention_hours = int(os.getenv("UPLOAD_RETENTION_HOURS", "48"))
    except (TypeError, ValueError):
        retention_hours = 48
    if retention_hours <= 0:
        return 0

    root = Path(os.getenv("TELEGRAM_UPLOAD_DIR", "/data/uploads"))
    if not root.is_dir() or root.is_symlink():
        return 0
    cutoff = time.time() - retention_hours * 3600
    removed = 0

    # Do not follow symlinks: uploads should remain confined to the upload root.
    for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for filename in files:
            path = current_path / filename
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
            except OSError:
                continue
        for dirname in dirs:
            path = current_path / dirname
            try:
                if path.is_symlink():
                    continue
                path.rmdir()
            except OSError:
                pass
    return removed


def _account(account_id):
    return account_for(account_id, "content")


@bp.post("/open")
@login_required
@limited(20, 60)
def open_desktop(account_id):
    acc, bad = _account(account_id)
    if bad:
        return bad
    audit(acc, "باز کردن پنجره Telegram Desktop", "warning")
    result = hub().call(hub().open_account(account_id), 35)
    return jsonify({**result, "stage": hub().stage_of(account_id)})


@bp.post("/ready")
@login_required
@limited(10, 60)
def mark_ready(account_id):
    acc, bad = _account(account_id)
    if bad:
        return bad
    result = hub().call(hub().mark_ready(account_id), 20)
    audit(acc, "تأیید اتصال حساب در Telegram Desktop", "warning")
    return jsonify(result)


@bp.get("/status")
@login_required
def desktop_status(account_id):
    acc, bad = _account(account_id)
    if bad:
        return bad
    return jsonify({"running": hub().running(account_id), "ready": hub().is_connected(account_id),
                    "stage": hub().stage_of(account_id)})


@bp.post("/view-token")
@login_required
@limited(30, 60)
def view_token(account_id):
    acc, bad = _account(account_id)
    if bad:
        return bad
    audit(acc, "ساخت دسترسی موقت به صفحه Telegram Desktop", "warning")
    url = hub().issue_view_token(account_id)
    return jsonify({"url": url, "expires_in": 600})


@bp.post("/close")
@login_required
@limited(10, 60)
def close_desktop(account_id):
    acc, bad = _account(account_id)
    if bad:
        return bad
    result = hub().call(hub().close_account(account_id), 20)
    audit(acc, "بستن Telegram Desktop (نشست ذخیره می‌شود)", "warning")
    return jsonify(result)


@bp.post("/upload")
@login_required
@limited(20, 60)
def upload_file(account_id):
    acc, bad = _account(account_id)
    if bad:
        return bad
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return err("یک فایل انتخاب کن")
    safe_name = secure_filename(uploaded.filename)[:120] or "telegram-upload.bin"
    folder = Path(os.getenv("TELEGRAM_UPLOAD_DIR", "/data/uploads")) / account_id
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    file_id = secrets.token_hex(8)
    destination = folder / f"{file_id}_{safe_name}"
    uploaded.save(destination)
    size = destination.stat().st_size
    if not size or size > 25 * 1024 * 1024:
        destination.unlink(missing_ok=True)
        return err("فایل خالی است یا از حد ۲۵ مگابایت بزرگ‌تر است", 413)
    try: destination.chmod(0o600)
    except OSError: pass
    audit(acc, f"بارگذاری فایل برای Telegram Desktop: {safe_name}", "warning")
    return jsonify({"ok": True, "name": destination.name, "path": str(destination), "size": size}), 201


@bp.get("/downloads")
@login_required
def downloads(account_id):
    acc, bad = _account(account_id)
    if bad:
        return bad
    folder = hub().download_dir(account_id)
    if not folder.exists():
        return jsonify([])
    rows = []
    for item in sorted(folder.rglob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            if item.is_file() and not item.is_symlink() and folder.resolve() in item.resolve().parents:
                rows.append({"name": item.relative_to(folder).as_posix(), "size": item.stat().st_size,
                             "modified": item.stat().st_mtime})
        except OSError:
            continue
    return jsonify(rows[:100])


@bp.get("/downloads/<path:filename>")
@login_required
def download_file(account_id, filename):
    acc, bad = _account(account_id)
    if bad:
        return bad
    if "\\" in filename or any(part in {".", ".."} for part in filename.split("/")):
        return err("نام فایل نامعتبر است", 400)
    folder = hub().download_dir(account_id)
    path = folder / filename
    if not path.is_file() or path.is_symlink() or folder.resolve() not in path.resolve().parents:
        return err("فایل پیدا نشد", 404)
    audit(acc, f"دریافت فایل از Telegram Desktop: {filename}")
    return send_from_directory(folder, filename, as_attachment=True, max_age=0)
