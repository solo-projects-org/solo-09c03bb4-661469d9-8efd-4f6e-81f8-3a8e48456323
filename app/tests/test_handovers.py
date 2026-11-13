"""Tests for handover sheet batches and the cross-carrier waybill mapping."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app.handovers as handovers
from app.handovers import Change, HandoverConflict, HandoverStore, Sheet
from app.main import app

client = TestClient(app)
MONTH = "2026-10"


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    path = tmp_path / "handovers.db"
    monkeypatch.setenv("HANDOVERS_DB", str(path))
    handovers.reset_store()
    yield str(path)
    handovers.reset_store()


def sheet(no, ca, wa, cb, wb):
    return {
        "handover_no": no,
        "carrier_a": ca,
        "waybill_no_a": wa,
        "carrier_b": cb,
        "waybill_no_b": wb,
    }


def post(sheets=(), revoke=(), month=MONTH):
    body = {"settlement_month": month, "sheets": list(sheets),
            "revoke": list(revoke)}
    resp = client.post("/handovers", json=body)
    return resp


def lookup(carrier, waybill_no, month=MONTH):
    resp = client.get("/mapping", params={
        "settlement_month": month,
        "carrier": carrier,
        "waybill_no": waybill_no,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def pairs(body, key="waybill_nos"):
    return {(e["carrier"], e["waybill_no"]) for e in body[key]}


CHAIN = [
    sheet("HJ-1", "甲", "YD-1001", "乙", "YT-7001"),
    sheet("HJ-2", "乙", "YT-7001", "丙", "SF-9001"),
]


def test_batch_links_a_chain_into_one_ticket():
    resp = post(CHAIN)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted_sheets"] == 2 and body["revoked_sheets"] == 0
    assert len(body["rebuilt_tickets"]) == 1
    assert pairs(body["rebuilt_tickets"][0], "endpoints") == {
        ("甲", "YD-1001"), ("乙", "YT-7001"), ("丙", "SF-9001")}
    # every endpoint sees the same ticket from the read-only mapping
    ids = set()
    for carrier, number in [("甲", "YD-1001"), ("乙", "YT-7001"), ("丙", "SF-9001")]:
        found = lookup(carrier, number)
        ids.add(found["ticket_id"])
        assert pairs(found) == {
            ("甲", "YD-1001"), ("乙", "YT-7001"), ("丙", "SF-9001")}
    assert len(ids) == 1


def test_correction_moves_the_number_between_tickets():
    post(CHAIN)
    # HJ-2 corrected: 丙's number no longer belongs with 乙's YT-7001
    resp = post([sheet("HJ-2", "乙", "YT-7001", "丙", "SF-9002")])
    assert resp.status_code == 200, resp.text
    assert pairs(lookup("丙", "SF-9002", MONTH)) == {
        ("甲", "YD-1001"), ("乙", "YT-7001"), ("丙", "SF-9002")}
    # the superseded spelling is gone from the mapping
    assert lookup("丙", "SF-9001")["waybill_nos"] == []


def test_revoke_last_path_splits_and_drops_orphans():
    post(CHAIN)
    resp = post(revoke=["HJ-2"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["revoked_sheets"] == 1
    # the chain is cut: 甲/乙 keep one ticket, 丙 has no sheet left
    assert pairs(lookup("甲", "YD-1001")) == {("甲", "YD-1001"), ("乙", "YT-7001")}
    assert pairs(lookup("乙", "YT-7001")) == {("甲", "YD-1001"), ("乙", "YT-7001")}
    assert lookup("丙", "SF-9001") == {
        "settlement_month": MONTH, "carrier": "丙", "waybill_no": "SF-9001",
        "ticket_id": None, "waybill_nos": [],
    }


def test_revoke_unknown_sheet_is_a_harmless_noop():
    post([CHAIN[0]])
    resp = post(revoke=["HJ-missing"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted_sheets"] == 0
    assert resp.json()["revoked_sheets"] == 0
    assert len(lookup("甲", "YD-1001")["waybill_nos"]) == 2


def test_conflict_in_batch_is_rejected_and_nothing_is_applied():
    post([CHAIN[0]])
    conflicting = sheet("HJ-3", "乙", "YT-7001", "甲", "YD-2001")
    resp = post([conflicting])
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail[0]["carrier"] == "甲"
    assert set(detail[0]["waybill_nos"]) == {"YD-1001", "YD-2001"}
    assert detail[0]["handover_nos"] == ["HJ-1", "HJ-3"]
    # previous state intact, including the rejected sheet
    assert pairs(lookup("甲", "YD-1001")) == {("甲", "YD-1001"), ("乙", "YT-7001")}
    assert lookup("甲", "YD-2001")["waybill_nos"] == []
    assert pairs(lookup("乙", "YT-7001")) == {("甲", "YD-1001"), ("乙", "YT-7001")}


def test_conflict_within_one_batch_rejects_the_whole_batch():
    batch = [
        CHAIN[0],
        sheet("HJ-4", "甲", "YD-1002", "乙", "YT-7001"),
        sheet("HJ-5", "丙", "SF-9001", "丁", "JD-8001"),
    ]
    resp = post(batch)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"][0]["carrier"] == "甲"
    # HJ-5 would have been fine on its own, but the batch as a whole failed
    assert lookup("丁", "JD-8001")["waybill_nos"] == []
    assert lookup("丙", "SF-9001")["waybill_nos"] == []


def test_conflict_from_correction_reverts_correction():
    post(CHAIN)
    # correcting HJ-2 so it links 乙's existing number to a second 甲 number
    # would put two 甲 numbers into the ticket built by HJ-1
    resp = post([sheet("HJ-2", "乙", "YT-7001", "甲", "YD-1002")])
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"][0]["carrier"] == "甲"
    # the correction did not land: 丙's spelling is still linked, YD-1002 is absent
    assert pairs(lookup("丙", "SF-9001")) == {
        ("甲", "YD-1001"), ("乙", "YT-7001"), ("丙", "SF-9001")}
    assert lookup("甲", "YD-1002")["waybill_nos"] == []


def test_sheet_joining_one_carrier_to_itself_is_a_conflict():
    # one sheet may not claim two numbers of the same carrier are one parcel
    resp = post([sheet("HJ-1", "甲", "YD-1", "甲", "YD-2")])
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"][0]
    assert detail["carrier"] == "甲"
    assert set(detail["waybill_nos"]) == {"YD-1", "YD-2"}
    assert lookup("甲", "YD-1")["waybill_nos"] == []


def test_same_month_isolated_from_other_months():
    post(CHAIN)
    resp = post([sheet("HJ-1", "甲", "YD-1001", "乙", "YT-7001")], month="2026-11")
    assert resp.status_code == 200, resp.text
    assert len(lookup("甲", "YD-1001", MONTH)["waybill_nos"]) == 3
    assert lookup("丙", "SF-9001", "2026-11")["waybill_nos"] == []
    # month-scoped lookup does not leak
    assert pairs(lookup("甲", "YD-1001", "2026-11")) == {
        ("甲", "YD-1001"), ("乙", "YT-7001")}


def test_only_affected_tickets_are_rebuilt():
    post(CHAIN)
    post([sheet("HJ-9", "戊", "JM-5001", "己", "YT-5001")])
    untouched = lookup("戊", "JM-5001")["ticket_id"]
    affected_before = lookup("甲", "YD-1001")["ticket_id"]
    resp = post(revoke=["HJ-2"])
    assert resp.status_code == 200, resp.text
    rebuilt = {t["ticket_id"] for t in resp.json()["rebuilt_tickets"]}
    assert untouched not in rebuilt
    assert lookup("戊", "JM-5001")["ticket_id"] == untouched
    # the split ticket is rebuilt under a new id (its endpoints changed)
    assert len(rebuilt) == 1
    assert lookup("甲", "YD-1001")["ticket_id"] in rebuilt
    assert affected_before not in rebuilt


def test_cycle_of_sheets_forms_one_ticket():
    post([
        sheet("HJ-1", "甲", "YD-1", "乙", "YT-1"),
        sheet("HJ-2", "乙", "YT-1", "丙", "SF-1"),
        sheet("HJ-3", "丙", "SF-1", "甲", "YD-1"),
    ])
    assert len(lookup("甲", "YD-1")["waybill_nos"]) == 3


def test_lookup_does_not_touch_sheets(db):
    post(CHAIN)
    conn = sqlite3.connect(db)
    before = conn.execute(
        "SELECT COUNT(*) FROM handover_sheets").fetchone()[0]
    conn.close()
    for _ in range(3):
        lookup("甲", "YD-1001")
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM handover_sheets").fetchone()[0] == before
    conn.close()


def test_sheets_and_mapping_are_persisted_to_sqlite(db):
    post(CHAIN)
    conn = sqlite3.connect(db)
    sheet_rows = conn.execute(
        "SELECT handover_no, carrier_a, waybill_no_a, carrier_b, waybill_no_b"
        " FROM handover_sheets WHERE settlement_month = ?", (MONTH,)).fetchall()
    mapping_rows = conn.execute(
        "SELECT carrier, waybill_no FROM waybill_mapping"
        " WHERE settlement_month = ?", (MONTH,)).fetchall()
    conn.close()
    assert len(sheet_rows) == 2 and len(mapping_rows) == 3
    # a fresh store over the same file serves the same mapping
    store = HandoverStore(db)
    ticket = store.lookup(MONTH, "丙", "SF-9001")
    assert ticket is not None and len(ticket.endpoints) == 3


def test_validation_errors_are_reported_together():
    resp = post(
        sheets=[sheet("HJ-1", "甲", "YD-1", "甲", "YD-1"),
                sheet("HJ-1", "乙", "YT-1", "丙", "SF-1")],
        revoke=["HJ-1"],
    )
    assert resp.status_code == 400, resp.text
    messages = " ".join(item["message"] for item in resp.json()["detail"])
    assert "自环" in messages
    assert "只能出现一次" in messages
    assert "既录入/更正又撤销" in messages


def test_empty_batch_and_bad_month_rejected():
    resp = post()
    assert resp.status_code == 400
    assert "不能同时为空" in resp.json()["detail"][0]["message"]
    resp = post([CHAIN[0]], month="2026-13")
    assert resp.status_code == 400
    assert "01-12" in resp.json()["detail"][0]["message"]
    resp = post([CHAIN[0]], month="202610")
    assert resp.status_code == 400
    assert "YYYY-MM" in resp.json()["detail"][0]["message"]


def test_concurrent_batches_follow_commit_order(db):
    """Two conflicting batches: the first to commit wins, the other conflicts."""
    store = HandoverStore(db)
    barrier = threading.Barrier(2)
    results = {}

    def run(tag, change):
        barrier.wait()
        try:
            store.apply_batch(MONTH, [change])
            results[tag] = "ok"
        except HandoverConflict:
            results[tag] = "conflict"

    first = Change("HJ-A", Sheet("HJ-A", "甲", "YD-1", "乙", "YT-1"))
    second = Change("HJ-B", Sheet("HJ-B", "乙", "YT-1", "甲", "YD-2"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, "a", first), pool.submit(run, "b", second)]
        for future in futures:
            future.result()

    assert sorted(results.values()) == ["conflict", "ok"]
    conn = sqlite3.connect(db)
    sheet_count = conn.execute(
        "SELECT COUNT(*) FROM handover_sheets WHERE settlement_month = ?",
        (MONTH,)).fetchone()[0]
    conn.close()
    assert sheet_count == 1
    # whichever committed, the mapping reflects exactly that sheet
    if results["a"] == "ok":
        assert pairs(lookup("甲", "YD-1")) == {("甲", "YD-1"), ("乙", "YT-1")}
        assert lookup("甲", "YD-2")["waybill_nos"] == []
    else:
        assert pairs(lookup("甲", "YD-2")) == {("甲", "YD-2"), ("乙", "YT-1")}
        assert lookup("甲", "YD-1")["waybill_nos"] == []


def test_analyze_endpoint_is_kept():
    schema = client.get("/openapi.json").json()
    assert "/analyze" in schema["paths"]
    assert "/handovers" in schema["paths"] and "/mapping" in schema["paths"]
    example = schema["components"]["schemas"]["HandoverBatchRequest"][
        "examples"][0]
    assert example["sheets"][0]["handover_no"] == "HJ-0001"
