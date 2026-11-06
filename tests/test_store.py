from datetime import timedelta

from store import Store, parse_dt, utcnow


def make_store(tmp_path):
    return Store(tmp_path)


def test_create_list_pause_resume_delete(tmp_path):
    store = make_store(tmp_path)
    row = store.create_job(name="meow", target="@somegroup", action="send", payload="میو", interval_seconds=300)
    assert row["status"] == "active"
    assert len(store.list_jobs()) == 1
    assert store.set_job_status(row["id"], "paused") is True
    assert store.get_job(row["id"])["status"] == "paused"
    assert store.set_job_status(row["id"], "active") is True
    assert store.get_job(row["id"])["status"] == "active"
    assert store.delete_job(row["id"]) is True
    assert store.list_jobs() == []


def test_claim_due_job_advances_and_pauses_at_max_runs(tmp_path):
    store = make_store(tmp_path)
    row = store.create_job(name="once", target="@somegroup", action="click", payload="میو", interval_seconds=60, max_runs=1)
    row["next_run_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
    # Make the persisted job due for a deterministic test.
    with store._connect() as con:
        con.execute("update jobs set next_run_at=? where id=?", (row["next_run_at"], row["id"]))
        con.commit()
    due = store.get_job(row["id"])
    claimed = store.claim_due_job(due)
    assert claimed is not None
    assert claimed["runs_count"] == 1
    assert claimed["status"] == "paused"


def test_events_are_ordered_newest_first(tmp_path):
    store = make_store(tmp_path)
    store.add_event("info", "first")
    store.add_event("success", "second")
    events = store.list_events()
    assert events[0]["message"] == "second"
    assert events[1]["message"] == "first"


def test_parse_dt_supports_utc_z_suffix():
    dt = parse_dt("2026-11-05T12:30:00Z")
    assert dt is not None and dt.utcoffset() == timedelta(0)


def test_accounts_and_member_credentials_are_scoped(tmp_path):
    store = make_store(tmp_path)
    member = store.create_member("operator_1", "test-password-hash")
    account_a = store.create_account(member["id"], "telegram", "پشتیبانی")
    account_b = store.create_account("owner", "telegram", "مالک")

    assert store.get_member("operator_1")["username"] == "operator_1"
    assert len(store.list_accounts(member["id"])) == 1
    assert store.list_accounts(member["id"])[0]["id"] == account_a["id"]
    assert {a["id"] for a in store.list_accounts()} == {account_a["id"], account_b["id"]}


def test_jobs_are_separated_by_owner_and_account(tmp_path):
    store = make_store(tmp_path)
    admin_job = store.create_job(name="admin", target="@target", action="send", payload="hello",
                                 interval_seconds=120, owner_id="admin-id", account_id="admin-account")
    owner_job = store.create_job(name="owner", target="@target", action="send", payload="hello",
                                 interval_seconds=120, owner_id="owner", account_id="owner-account")

    assert [j["id"] for j in store.list_jobs(owner_id="admin-id")] == [admin_job["id"]]
    assert {j["id"] for j in store.list_jobs()} == {admin_job["id"], owner_job["id"]}
    assert store.get_job(owner_job["id"], owner_id="admin-id") is None
    assert store.delete_job(owner_job["id"], owner_id="admin-id") is False


def test_runtime_state_can_be_deleted(tmp_path):
    store = make_store(tmp_path)
    store.set_runtime_state("session:one", "ciphertext")
    assert store.get_runtime_state("session:one") == "ciphertext"
    store.delete_runtime_state("session:one")
    assert store.get_runtime_state("session:one") is None



def test_admin_account_owner_access_starts_disabled_and_can_be_revoked(tmp_path):
    store = make_store(tmp_path)
    member = store.create_member("operator_2", "test-password-hash")
    account = store.create_account(member["id"], "telegram", "پشتیبانی")
    assert account["owner_access"] is False
    assert not bool(store.get_account(account["id"])["owner_access"])

    assert store.update_account(account["id"], {"owner_access": True}) is True
    assert bool(store.get_account(account["id"])["owner_access"])

    assert store.update_account(account["id"], {"owner_access": False}) is True
    assert not bool(store.get_account(account["id"])["owner_access"])


def test_admin_account_is_not_owner_accessible_by_default(tmp_path):
    from runtime import job_account_is_authorized

    store = make_store(tmp_path)
    member = store.create_member("operator_3", "test-password-hash")
    account = store.create_account(member["id"], "telegram", "شخصی")
    owner_job = {"owner_id": "owner"}
    admin_job = {"owner_id": member["id"]}

    assert job_account_is_authorized(admin_job, account) is True
    assert job_account_is_authorized(owner_job, account) is False
    account["owner_access"] = True
    assert job_account_is_authorized(owner_job, account) is True
    account["owner_access"] = False
    assert job_account_is_authorized(owner_job, account) is False



def test_legacy_accounts_table_migrates_owner_access_column(tmp_path):
    import sqlite3

    tmp_path.mkdir(exist_ok=True)
    db_path = tmp_path / "borhan-sk-local.sqlite3"
    with sqlite3.connect(db_path) as con:
        con.execute("""CREATE TABLE accounts (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, platform TEXT NOT NULL,
            label TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        con.execute("""INSERT INTO accounts(id,owner_id,platform,label,status,created_at,updated_at)
                      VALUES('old-account','admin-id','telegram','قدیمی','connected','2026-01-01','2026-01-01')""")
        con.commit()

    store = Store(tmp_path)
    migrated = store.get_account("old-account")
    assert migrated is not None
    assert migrated["label"] == "قدیمی"
    assert not bool(migrated["owner_access"])


def test_owner_account_access_requires_admin_consent():
    from runtime import owner_can_access_account

    own_account = {"owner_id": "owner", "owner_access": False}
    private_admin_account = {"owner_id": "admin-1", "owner_access": False}
    shared_admin_account = {"owner_id": "admin-1", "owner_access": True}

    assert owner_can_access_account(own_account) is True
    assert owner_can_access_account(private_admin_account) is False
    assert owner_can_access_account(shared_admin_account) is True
    assert owner_can_access_account(None) is False
