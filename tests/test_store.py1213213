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
