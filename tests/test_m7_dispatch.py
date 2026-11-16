import pytest

from app.errors import DomainError
from app.modules.m7_dispatch import repo, service
import app.store as store

T0 = "2026-11-16T09:00:00+08:00"
T1 = "2026-11-16T09:01:00+08:00"
T2 = "2026-11-16T09:02:01+08:00"

MANIFEST = """issue,edition,page,file_name,sha256,phash,clarity,header_issue
2026-11-01,01,001,a.tif,aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa,0f0f,92,2026-11-01
2026-11-01,01,004,b.tif,bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb,f0f0,81,2026-11-01
2026-11-02,01,001,c.tif,cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc,0f1f,88,2026-11-02
2026-11-02,01,002,d.tif,dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd,0f1e,86,2026-11-02
"""


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "dispatch.sqlite3")
    monkeypatch.setattr(repo, "WRITE_HOOK", None)


def generate(counts=None):
    return service.generate({"text": MANIFEST, "expected_counts": counts or {"2026-11-01|01": 4}, "at": T0})


def sites(*capacities):
    for site, capacity in capacities:
        service.configure_site({"site": site, "capacity": capacity})


def claim(task_id="T-2026-11-01-01-002", site="SITE-A", **extra):
    payload = {"task_id": task_id, "site": site, "at": T1, "timeout_seconds": 60}
    payload.update(extra)
    return service.claim(payload)


def reassign(task_id="T-2026-11-01-01-002", at=T2, **extra):
    payload = {"task_id": task_id, "at": at}
    payload.update(extra)
    return service.reassign(payload)


def receipt(order, **extra):
    payload = {"order_id": order["order_id"], "credential": order["credential"]}
    payload.update(extra)
    return service.receipt(payload)


def test_generate_uses_real_page_order():
    result = generate()
    assert result["tasks"] == ["T-2026-11-01-01-002", "T-2026-11-01-01-003"]
    assert result["reason_code"] == "MISSING_PAGE"


def test_generate_from_rows_and_declared_count():
    result = service.generate({
        "rows": [
            {"issue": "2026-11-01", "edition": "1", "page": "1"},
            {"issue": "2026-11-01", "edition": "1", "page": "3"},
            {"issue": "2026-11-01", "edition": "1", "page": "3"},
        ],
        "expected_counts": {"2026-11-01|01": 4},
        "at": T0,
    })
    assert result["tasks"] == ["T-2026-11-01-01-002", "T-2026-11-01-01-004"]


def test_regeneration_keeps_existing_task_identity_and_progress():
    generate()
    sites(("SITE-A", 1))
    order = claim()
    again = generate()
    assert again["created"] == [] and again["existing"] == ["T-2026-11-01-01-002", "T-2026-11-01-01-003"]
    tasks = {task["task_id"]: task for task in service.tasks({})["tasks"]}
    assert tasks["T-2026-11-01-01-002"]["status"] == "IN_FLIGHT"
    assert tasks["T-2026-11-01-01-002"]["active_order"]["order_id"] == order["order_id"]


def test_claim_respects_site_capacity():
    generate()
    sites(("SITE-A", 1), ("SITE-B", 1))
    claim()
    with pytest.raises(DomainError) as exc:
        claim(site="SITE-A", task_id="T-2026-11-01-01-003")
    assert exc.value.code == "CAPACITY_FULL" and exc.value.status == 409
    other = claim(task_id="T-2026-11-01-01-003", site="SITE-B")
    assert other["site"] == "SITE-B"
    snapshot = service.summary({"at": T1})
    assert [(s["site"], s["in_flight"], s["available"]) for s in snapshot["capacity"]["sites"]] == [("SITE-A", 1, 0), ("SITE-B", 1, 0)]


def test_claim_rejects_stale_version():
    generate()
    sites(("SITE-A", 2))
    claim(expected_version=1)
    with pytest.raises(DomainError) as exc:
        claim(expected_version=1, site="SITE-A")
    assert exc.value.code == "STALE_VERSION"


def test_claim_retry_same_request_is_idempotent():
    generate()
    sites(("SITE-A", 1))
    first = claim(request_id="req-1")
    again = claim(request_id="req-1")
    assert again == first
    assert len(service.orders({"task_id": first["task_id"]})["orders"]) == 1
    snapshot = service.summary({"at": T1})
    assert snapshot["capacity"]["sites"][0]["in_flight"] == 1


def test_claim_retry_with_different_payload_rejected():
    generate()
    sites(("SITE-A", 1))
    claim(request_id="req-1")
    with pytest.raises(DomainError) as exc:
        claim(request_id="req-1", site="SITE-B")
    assert exc.value.code == "IDEMPOTENCY_MISMATCH"


def test_reassign_before_timeout_rejected():
    generate()
    sites(("SITE-A", 1), ("SITE-B", 1))
    claim()
    with pytest.raises(DomainError) as exc:
        reassign(at="2026-11-16T09:01:30+08:00")
    assert exc.value.code == "NOT_TIMED_OUT"


def test_reassign_keeps_original_order_and_issues_new_credential():
    generate()
    sites(("SITE-A", 1), ("SITE-B", 1))
    old = claim()
    new = reassign()
    assert new["site"] == "SITE-B" and new["generation"] == 2
    assert new["order_id"] != old["order_id"] and new["credential"] != old["credential"]
    orders = service.orders({"task_id": old["task_id"]})["orders"]
    assert [(o["order_id"], o["status"]) for o in orders] == [(old["order_id"], "SUPERSEDED"), (new["order_id"], "IN_FLIGHT")]
    assert orders[0]["close_reason"] == "TIMEOUT_REASSIGNED"
    assert old["credential"] not in [o.get("credential") for o in orders]  # 凭据不在回读中泄露


def test_reassign_requires_free_capacity_else_keeps_original_slot():
    generate()
    sites(("SITE-A", 1), ("SITE-B", 1))
    old = claim()
    claim(task_id="T-2026-11-01-01-003", site="SITE-B")
    with pytest.raises(DomainError) as exc:
        reassign()
    assert exc.value.code == "CAPACITY_FULL"
    tasks = {task["task_id"]: task for task in service.tasks({})["tasks"]}
    assert tasks[old["task_id"]]["status"] == "IN_FLIGHT"
    assert tasks[old["task_id"]]["active_order"]["order_id"] == old["order_id"]
    assert tasks[old["task_id"]]["active_order"]["site"] == "SITE-A"


def test_late_receipt_enters_conflict_and_does_not_affect_new_order():
    generate()
    sites(("SITE-A", 1), ("SITE-B", 1))
    old = claim()
    new = reassign()
    late = receipt(old, at="2026-11-16T09:03:00+08:00")
    assert late["status"] == "CONFLICT_PENDING" and late["reason_code"] == "LATE_RECEIPT"
    assert late["counts_as_completed"] is False
    assert late["task_status"] == "IN_FLIGHT" and late["active_order_id"] == new["order_id"]
    tasks = {task["task_id"]: task for task in service.tasks({})["tasks"]}
    assert tasks[new["task_id"]]["status"] == "IN_FLIGHT"
    assert service.receipts_read({"status": "CONFLICT_PENDING"})["count"] == 1
    assert service.summary({"at": "2026-11-16T09:03:00+08:00"})["in_flight"] == 1


def test_receipt_completes_task_and_releases_capacity():
    generate()
    sites(("SITE-A", 1), ("SITE-B", 1))
    order = claim()
    done = receipt(order, at="2026-11-16T09:01:30+08:00")
    assert done["status"] == "COMPLETED" and done["counts_as_completed"] is True
    snapshot = service.summary({"at": "2026-11-16T09:01:30+08:00"})
    assert snapshot["in_flight"] == 0 and snapshot["completed"] == 1
    assert snapshot["capacity"]["sites"][0]["available"] == 1
    duplicate = receipt(order, at="2026-11-16T09:01:40+08:00")
    assert duplicate["counts_as_completed"] is False
    assert len(service.receipts_read({"task_id": order["task_id"]})["receipts"]) == 1


def test_receipt_with_wrong_credential_rejected():
    generate()
    sites(("SITE-A", 1))
    order = claim()
    with pytest.raises(DomainError) as exc:
        service.receipt({"order_id": order["order_id"], "credential": "forged", "at": "2026-11-16T09:01:30+08:00"})
    assert exc.value.code == "INVALID_CREDENTIAL"
    assert service.summary({"at": "2026-11-16T09:01:30+08:00"})["in_flight"] == 1


def test_write_failure_rolls_back_and_retry_does_not_double_book():
    generate()
    sites(("SITE-A", 1))

    def boom(_conn):
        raise RuntimeError("simulated disk failure")

    repo.WRITE_HOOK = boom
    with pytest.raises(DomainError) as exc:
        claim(request_id="req-1")
    assert exc.value.code == "WRITE_FAILED" and exc.value.status == 503

    repo.WRITE_HOOK = None
    tasks = {task["task_id"]: task for task in service.tasks({})["tasks"]}
    assert tasks["T-2026-11-01-01-002"]["status"] == "PENDING"
    assert tasks["T-2026-11-01-01-002"]["version"] == 1
    assert service.orders({"task_id": "T-2026-11-01-01-002"})["orders"] == []
    assert service.summary({"at": T1})["capacity"]["sites"][0]["in_flight"] == 0

    retried = claim(request_id="req-1")
    assert retried["order_id"] == "O-T-2026-11-01-01-002-01"
    assert service.summary({"at": T1})["capacity"]["sites"][0]["in_flight"] == 1


def test_overdue_inflight_is_visible_in_readback():
    generate()
    sites(("SITE-A", 1))
    claim()
    before = service.tasks({"at": T1})["tasks"][0]
    after = service.tasks({"at": "2026-11-16T09:05:00+08:00"})["tasks"][0]
    assert before["overdue"] is False and before["active_valid"] is True
    assert after["overdue"] is True and after["active_valid"] is False
    assert service.summary({"at": "2026-11-16T09:05:00+08:00"})["overdue"] == 1


def test_concurrent_claims_do_not_double_book_capacity():
    import threading

    generate()
    sites(("SITE-A", 1))
    results = []

    def attempt(task_id):
        try:
            results.append(("ok", claim(task_id=task_id)))
        except DomainError as exc:
            results.append(("err", exc.code))

    threads = [threading.Thread(target=attempt, args=(task_id,)) for task_id in ("T-2026-11-01-01-002", "T-2026-11-01-01-003")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(item[0] for item in results) == ["err", "ok"]
    assert [item[1] for item in results if item[0] == "err"] == ["CAPACITY_FULL"]
    snapshot = service.summary({"at": T1})
    assert snapshot["capacity"]["sites"][0]["in_flight"] == 1
    assert len(service.orders({})["orders"]) == 1


def test_late_receipt_after_new_order_completes_still_needs_review():
    generate()
    sites(("SITE-A", 1), ("SITE-B", 1))
    old = claim()
    new = reassign()
    receipt(new, at="2026-11-16T09:03:00+08:00")
    late = receipt(old, at="2026-11-16T09:04:00+08:00")
    assert late["status"] == "CONFLICT_PENDING" and late["counts_as_completed"] is False
    assert late["task_status"] == "COMPLETED"
    assert service.summary({"at": "2026-11-16T09:04:00+08:00"})["conflict_pending"] == 1


def test_http_surface_generates_claims_transfers_and_reads_back():
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    assert client.get("/dispatch").status_code == 200
    generated = client.post("/api/dispatch/generate", json={
        "text": MANIFEST,
        "expected_counts": {"2026-11-01|01": 4},
        "at": T0,
    }).json()
    assert generated["tasks"] == ["T-2026-11-01-01-002", "T-2026-11-01-01-003"]

    client.post("/api/dispatch/sites", json={"site": "SITE-A", "capacity": 1})
    client.post("/api/dispatch/sites", json={"site": "SITE-B", "capacity": 1})
    claimed = client.post("/api/dispatch/claim", json={
        "task_id": "T-2026-11-01-01-002", "site": "SITE-A", "timeout_seconds": 60, "at": T1,
    }).json()
    conflict = client.post("/api/dispatch/claim", json={
        "task_id": "T-2026-11-01-01-003", "site": "SITE-A", "at": T1,
    })
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "CAPACITY_FULL"

    transferred = client.post("/api/dispatch/reassign", json={"task_id": "T-2026-11-01-01-002", "at": T2}).json()
    assert transferred["generation"] == 2 and transferred["site"] == "SITE-B"

    late = client.post("/api/dispatch/receipt", json={
        "order_id": claimed["order_id"], "credential": claimed["credential"], "at": "2026-11-16T09:03:00+08:00",
    }).json()
    assert late["status"] == "CONFLICT_PENDING" and late["counts_as_completed"] is False

    done = client.post("/api/dispatch/receipt", json={
        "order_id": transferred["order_id"], "credential": transferred["credential"], "at": "2026-11-16T09:04:00+08:00",
    }).json()
    assert done["counts_as_completed"] is True

    readback = client.get("/api/dispatch/tasks", params={"at": "2026-11-16T09:05:00+08:00"}).json()
    statuses = {task["task_id"]: task["status"] for task in readback["tasks"]}
    assert statuses == {"T-2026-11-01-01-002": "COMPLETED", "T-2026-11-01-01-003": "PENDING"}
    summary = client.get("/api/dispatch/summary", params={"at": "2026-11-16T09:05:00+08:00"}).json()
    assert (summary["tasks"], summary["pending"], summary["in_flight"], summary["completed"]) == (2, 1, 0, 1)
    assert summary["conflict_pending"] == 1
    assert client.get("/api/dispatch/orders", params={"task_id": "T-2026-11-01-01-002"}).json()["orders"][0]["status"] == "SUPERSEDED"
    assert client.get("/api/dispatch/receipts", params={"status": "CONFLICT_PENDING"}).json()["count"] == 1


def test_verify_balances_tasks_inflight_and_capacity():
    generate()
    sites(("SITE-A", 1), ("SITE-B", 1))
    old = claim()
    service.reassign({"task_id": "T-2026-11-01-01-002", "at": T2})
    verdict = service.verify({"at": "2026-11-16T09:02:30+08:00"})
    assert verdict["ok"] and verdict["violations"] == []
    assert verdict["tasks"] == {"total": 2, "pending": 1, "in_flight": 1, "completed": 0, "overdue": 0}
    assert verdict["in_flight"] == 1
    assert [(row["site"], row["in_flight"], row["available"]) for row in verdict["capacity"]] == [("SITE-A", 0, 1), ("SITE-B", 1, 0)]

    receipt(old, at="2026-11-16T09:03:00+08:00")  # 迟到回执不改变守恒
    after = service.verify({"at": "2026-11-16T09:03:30+08:00"})
    assert after["ok"] and after["in_flight"] == 1 and after["receipts"]["conflict_pending"] == 1


def test_repeated_late_receipt_keeps_single_conflict_record():
    generate()
    sites(("SITE-A", 1), ("SITE-B", 1))
    old = claim()
    reassign()
    first = receipt(old, at="2026-11-16T09:03:00+08:00")
    second = receipt(old, at="2026-11-16T09:03:30+08:00")
    assert first["receipt_id"] == second["receipt_id"]
    assert service.receipts_read({"status": "CONFLICT_PENDING"})["count"] == 1


def test_capacity_cannot_drop_below_occupancy():
    generate()
    sites(("SITE-A", 2))
    claim()
    claim(task_id="T-2026-11-01-01-003", site="SITE-A")
    assert service.configure_site({"site": "SITE-A", "capacity": 2})["available"] == 0
    with pytest.raises(DomainError) as exc:
        service.configure_site({"site": "SITE-A", "capacity": 0})
    assert exc.value.code == "BAD_CAPACITY"
    with pytest.raises(DomainError) as exc:
        service.configure_site({"site": "SITE-A", "capacity": 1})
    assert exc.value.code == "CAPACITY_BELOW_OCCUPIED"
    assert service.summary({"at": T1})["capacity"]["sites"][0]["capacity"] == 2


def test_generate_rejects_bad_expected_count_without_tasks():
    result = service.generate({
        "rows": [{"issue": "2026-11-01", "edition": "01", "page": "001"}],
        "expected_counts": {"2026-11-01|01": "abc"},
        "at": T0,
    })
    assert result["generated"] == 0 and result["rejected"][0]["reason_code"] == "BAD_COUNT"
    assert service.summary({"at": T0})["tasks"] == 0


def test_summary_separates_valid_inflight_from_overdue():
    generate()
    sites(("SITE-A", 2))
    claim()
    fresh = service.summary({"at": T1})
    stale = service.summary({"at": "2026-11-16T09:05:00+08:00"})
    assert (fresh["in_flight"], fresh["in_flight_valid"], fresh["overdue"]) == (1, 1, 0)
    assert (stale["in_flight"], stale["in_flight_valid"], stale["overdue"]) == (1, 0, 1)
    assert stale["capacity"]["sites"][0]["in_flight"] == 1  # 超时单在转派前仍占原站点名额


def test_claim_without_task_takes_queue_head_in_order():
    generate()
    sites(("SITE-A", 2))
    first = service.claim({"site": "SITE-A", "at": T1, "timeout_seconds": 60})
    second = service.claim({"site": "SITE-A", "at": T1, "timeout_seconds": 60})
    assert (first["task_id"], second["task_id"]) == ("T-2026-11-01-01-002", "T-2026-11-01-01-003")
    with pytest.raises(DomainError) as exc:
        service.claim({"site": "SITE-A", "at": T1})
    assert exc.value.code == "QUEUE_EMPTY"


def test_generate_surfaces_manifest_quarantine():
    dirty = MANIFEST + "2026-11-01,01,002\n"
    result = service.generate({"text": dirty, "expected_counts": {"2026-11-01|01": 4}, "at": T0})
    assert any(item["reason_code"] == "COLUMN_COUNT" for item in result["rejected"])
    assert result["tasks"] == ["T-2026-11-01-01-002", "T-2026-11-01-01-003"]
