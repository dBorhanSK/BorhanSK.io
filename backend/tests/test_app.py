import os
import sys
import tempfile
import unittest
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse

os.environ["BORHAN_NO_AUTOSTART"] = "1"
os.environ["DASHBOARD_PASSWORD"] = "owner-password-123"
os.environ["COOKIE_SECURE"] = "false"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["WEBHOOK_TOKEN"] = "hook-token"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from store import Store  # noqa: E402


class FakeHub:
    configured = True
    last_error: dict = {}
    configured = True
    loop = None
    ready = None

    def __init__(self):
        import threading
        self.sent = []
        self.connected = set()
        self.running_set = set()
        self.last_error = {}
        self.ready = threading.Event()

    def is_connected(self, aid):
        return aid in self.connected

    def running(self, aid):
        return aid in self.running_set or aid in self.connected

    def stage_of(self, aid):
        return "connected" if aid in self.connected else ("desktop_login" if self.running(aid) else "idle")

    def issue_view_token(self, aid):
        if not self.running(aid):
            raise RuntimeError("not running")
        return "https://127.0.0.1/vnc.html?token=test"

    async def open_account(self, aid):
        self.running_set.add(aid)
        return {"running": True, "ready": aid in self.connected}

    async def mark_ready(self, aid):
        self.running_set.add(aid)
        self.connected.add(aid)
        return {"ready": True}

    async def disconnect_keep(self, aid):
        self.running_set.add(aid)
        return {"running": True}

    async def revoke_view_tokens(self, aid):
        return {"ok": True, "running": self.running(aid)}

    def call(self, coro, timeout=25):
        return asyncio.run(coro)

    async def dialogs(self, aid, q="", limit=40):
        return [{"id": 1, "name": "Test", "unread": 0, "type": "user", "preview": "hi", "date": None,
                 "pinned": False, "out": False}]

    async def send(self, aid, chat, text, reply_to=None):
        self.sent.append((aid, chat, text))
        return {"id": 5, "text": text}

    async def send_text(self, aid, target, text):
        self.sent.append((aid, target, text))

    async def remove(self, aid, revoke=True):
        self.connected.discard(aid)
        self.running_set.discard(aid)


class Base(unittest.TestCase):
    def setUp(self):
        import app as app_module
        app_module._login_fail.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.hub = FakeHub()
        self.app = create_app(testing=True, store_obj=self.store, hub_obj=self.hub)
        self.admin = self.store.create_member("alice", generate_password_hash("alice-password-12"))
        self.admin2 = self.store.create_member("bob", generate_password_hash("bob-password-1234"))

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, username, password):
        c = self.app.test_client()
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_json())
        c.csrf = r.get_json()["csrf"]
        return c

    @staticmethod
    def post(c, url, json=None):
        return c.post(url, json=json or {}, headers={"X-CSRF-Token": c.csrf})


class StoreTests(Base):
    def test_job_lifecycle(self):
        acc = self.store.create_account("owner", "main")
        job = self.store.create_job(name="n", target="@x", action="send", payload="a\nb", interval_seconds=60,
                                    account_id=acc["id"], max_runs=1)
        self.assertEqual(len(self.store.list_jobs()), 1)
        claimed = self.store.claim_due_job(job, 1)
        self.assertEqual(claimed["status"], "paused")
        self.assertTrue(self.store.set_job_status(job["id"], "active"))
        self.assertTrue(self.store.trigger_job(job["id"]))
        self.assertTrue(self.store.delete_account(acc["id"]))
        self.assertEqual(self.store.list_jobs(), [])

    def test_events_scope(self):
        self.store.add_event("info", "a", "owner")
        self.store.add_event("info", "b", "x")
        self.assertEqual(len(self.store.list_events(10, "x")), 1)
        self.assertEqual(self.store.list_events(10)[0]["message"], "b")


class AuthTests(Base):
    def test_login_and_csrf(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/api/me").status_code, 401)
        self.assertEqual(c.post("/api/login", json={"username": "owner", "password": "bad"}).status_code, 401)
        c = self.client("owner", "owner-password-123")
        self.assertEqual(c.get("/api/me").get_json()["role"], "owner")
        # state-changing call without CSRF header is refused
        self.assertEqual(c.post("/api/accounts", json={"label": "x"}).status_code, 403)
        self.assertEqual(self.post(c, "/api/accounts", {"label": "x"}).status_code, 201)

    def test_origin_check(self):
        c = self.client("owner", "owner-password-123")
        r = c.post("/api/accounts", json={"label": "x"},
                   headers={"X-CSRF-Token": c.csrf, "Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = c.post("/api/accounts", json={"label": "x"},
                   headers={"X-CSRF-Token": c.csrf, "Origin": "https://borhansk.netlify.app"})
        self.assertEqual(r.status_code, 201)

    def test_lockout(self):
        c = self.app.test_client()
        codes = [c.post("/api/login", json={"username": "alice", "password": "nope"}).status_code for _ in range(9)]
        self.assertIn(429, codes)

    def test_admin_disabled_is_kicked(self):
        c = self.client("alice", "alice-password-12")
        self.assertEqual(c.get("/api/accounts").status_code, 200)
        owner = self.client("owner", "owner-password-123")
        self.assertEqual(owner.patch(f"/api/admins/{self.admin['id']}", json={"active": False},
                                     headers={"X-CSRF-Token": owner.csrf}).status_code, 200)
        import deps
        deps._member_cache.clear()
        self.assertEqual(c.get("/api/accounts").status_code, 401)


class AccessTests(Base):
    def setUp(self):
        super().setUp()
        self.alice = self.client("alice", "alice-password-12")
        self.bob = self.client("bob", "bob-password-1234")
        self.owner = self.client("owner", "owner-password-123")
        self.acc = self.post(self.alice, "/api/accounts", {"label": "alice-tg"}).get_json()
        self.hub.connected.add(self.acc["id"])
        self.hub.running_set.add(self.acc["id"])

    def test_admin_isolation(self):
        self.assertEqual(self.bob.get("/api/accounts").get_json(), [])
        self.assertEqual(self.post(self.bob, f"/api/accounts/{self.acc['id']}/desktop/open").status_code, 404)
        self.assertEqual(self.bob.delete(f"/api/accounts/{self.acc['id']}",
                                         headers={"X-CSRF-Token": self.bob.csrf}).status_code, 404)

    def test_owner_sees_meta_but_not_content_without_consent(self):
        rows = self.owner.get("/api/accounts").get_json()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["can_use"])
        self.assertEqual(rows[0]["holder"], "alice")
        self.assertEqual(self.post(self.owner, f"/api/accounts/{self.acc['id']}/desktop/open").status_code, 403)
        self.assertEqual(self.post(self.owner, f"/api/accounts/{self.acc['id']}/desktop/view-token").status_code, 403)

    def test_owner_with_consent_and_audit(self):
        r = self.post(self.alice, f"/api/accounts/{self.acc['id']}/owner-access", {"enabled": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.post(self.owner, f"/api/accounts/{self.acc['id']}/desktop/open").status_code, 200)
        r = self.post(self.owner, f"/api/accounts/{self.acc['id']}/desktop/view-token")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/vnc.html?token=test", r.get_json()["url"])
        events = self.alice.get("/api/events").get_json()
        self.assertTrue(any("مالک" in e["message"] for e in events))
        # revoking removes access again
        self.post(self.alice, f"/api/accounts/{self.acc['id']}/owner-access", {"enabled": False})
        self.assertEqual(self.post(self.owner, f"/api/accounts/{self.acc['id']}/desktop/view-token").status_code, 403)

    def test_job_content_hidden_from_owner_without_consent(self):
        r = self.post(self.alice, "/api/self/jobs", {"account_id": self.acc["id"], "name": "j", "target": "@secret",
                                                     "action": "send", "payload": "private text",
                                                     "interval_minutes": 5})
        self.assertEqual(r.status_code, 201, r.get_json())
        rows = self.owner.get("/api/self/jobs").get_json()
        self.assertEqual(rows[0]["payload"], "")
        self.assertTrue(rows[0]["hidden"])
        self.assertEqual(self.alice.get("/api/self/jobs").get_json()[0]["payload"], "private text")
        self.assertEqual(self.bob.get("/api/self/jobs").get_json(), [])

    def test_job_validation(self):
        base = {"account_id": self.acc["id"], "name": "j", "target": "@x", "action": "send", "payload": "p"}
        self.assertEqual(self.post(self.alice, "/api/self/jobs", {**base, "interval_minutes": 0.1}).status_code, 400)
        self.assertEqual(self.post(self.alice, "/api/self/jobs", {**base, "interval_minutes": 5,
                                                                  "active_from": 9}).status_code, 400)
        self.assertEqual(self.post(self.alice, "/api/self/jobs", {**base, "interval_minutes": 5,
                                                                  "active_from": 9, "active_to": 22}).status_code, 201)

    def test_admin_cannot_create_admins(self):
        self.assertEqual(self.post(self.alice, "/api/admins", {"username": "eve", "password": "x" * 14}).status_code, 403)
        self.assertEqual(self.post(self.owner, "/api/admins", {"username": "eve", "password": "x" * 14}).status_code, 201)

    def test_webhook(self):
        c = self.app.test_client()
        self.assertEqual(c.post("/hooks/send", json={"target": "@x", "text": "t"}).status_code, 401)
        mine = self.post(self.owner, "/api/accounts", {"label": "own"}).get_json()
        self.hub.connected.add(mine["id"])
        r = c.post("/hooks/send", json={"target": "@x", "text": "t"}, headers={"X-Webhook-Token": "hook-token"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.hub.sent[-1][1:], ("@x", "t"))


class DesktopTokenTests(unittest.TestCase):
    def test_websockify_token_is_in_websocket_path_query(self):
        from desktop_hub import DesktopHub
        with tempfile.TemporaryDirectory() as tmp:
            hub = object.__new__(DesktopHub)
            hub.api_host = "203-0-113-10.nip.io"
            hub.api_scheme = "https"
            hub.tokens = {}
            hub.lock = threading.RLock()
            hub.token_file = Path(tmp) / "tokens.txt"
            hub._session = lambda _account_id: SimpleNamespace(vnc_port=5901)
            url = hub.issue_view_token("account-1")
            outer = parse_qs(urlparse(url).query)
            self.assertNotIn("token", outer)
            ws_path = outer["path"][0]
            self.assertEqual(urlparse(ws_path).path, "websockify")
            token = parse_qs(urlparse(ws_path).query)["token"][0]
            self.assertIn(f"{token}: 127.0.0.1:5901", hub.token_file.read_text())


class SelfLogicTests(unittest.TestCase):
    def test_active_window(self):
        from datetime import datetime
        from modules.self_bot import in_active_window
        self.assertTrue(in_active_window({"active_from": None, "active_to": None}, datetime(2026, 1, 1, 3)))
        self.assertTrue(in_active_window({"active_from": 9, "active_to": 22}, datetime(2026, 1, 1, 10)))
        self.assertFalse(in_active_window({"active_from": 9, "active_to": 22}, datetime(2026, 1, 1, 23)))
        self.assertTrue(in_active_window({"active_from": 22, "active_to": 6}, datetime(2026, 1, 1, 2)))

    def test_desktop_error(self):
        # Official Telegram Desktop login is performed in its GUI, not parsed by this API.
        from desktop_hub import HubError
        self.assertEqual(str(HubError("desktop")), "desktop")


if __name__ == "__main__":
    unittest.main()
