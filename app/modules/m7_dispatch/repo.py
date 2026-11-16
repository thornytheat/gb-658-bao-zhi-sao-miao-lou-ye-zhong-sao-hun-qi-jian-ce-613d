"""补扫派工持久化层。

所有状态变更都在单个 ``BEGIN IMMEDIATE`` 事务内完成：要么整体提交，
要么整体回滚。原工单只在超时转派时关闭为 ``SUPERSEDED``，从不删除，
以便保留凭据与执行证据。
"""

import json
import secrets
import sqlite3
from contextlib import contextmanager
from hashlib import sha256

from app.store import connect

from .model import (
    COMPLETED,
    DEFAULT_SITE_CAPACITY,
    IN_FLIGHT,
    ORDER_COMPLETED,
    ORDER_IN_FLIGHT,
    ORDER_SUPERSEDED,
    PENDING,
    REASON_LATE_RECEIPT,
    RECEIPT_COMPLETED,
    RECEIPT_CONFLICT,
    deadline_after,
    iso,
    order_id_for,
    parse_moment,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatch_sites (
  site TEXT PRIMARY KEY,
  capacity INTEGER NOT NULL CHECK (capacity >= 1)
);
CREATE TABLE IF NOT EXISTS dispatch_tasks (
  task_id TEXT PRIMARY KEY,
  issue TEXT NOT NULL,
  edition TEXT NOT NULL,
  page TEXT NOT NULL,
  expected_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  version INTEGER NOT NULL,
  active_order_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch_orders (
  order_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  site TEXT NOT NULL,
  generation INTEGER NOT NULL,
  status TEXT NOT NULL,
  version INTEGER NOT NULL,
  credential TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  deadline TEXT NOT NULL,
  timeout_seconds INTEGER NOT NULL,
  closed_at TEXT,
  close_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_dispatch_orders_task ON dispatch_orders(task_id);
CREATE INDEX IF NOT EXISTS ix_dispatch_orders_site ON dispatch_orders(site, status);
CREATE TABLE IF NOT EXISTS dispatch_receipts (
  receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  site TEXT NOT NULL,
  credential TEXT NOT NULL,
  status TEXT NOT NULL,
  reason_code TEXT,
  received_at TEXT NOT NULL,
  payload TEXT
);
CREATE INDEX IF NOT EXISTS ix_dispatch_receipts_order ON dispatch_receipts(order_id);
CREATE TABLE IF NOT EXISTS dispatch_requests (
  request_id TEXT PRIMARY KEY,
  operation TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  response TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

# 故障注入钩子：测试可在提交前抛出异常，验证回滚后进度不变。
WRITE_HOOK = None


class StorageError(RuntimeError):
    """派工写入失败；事务已回滚，可安全重试。"""


class StaleStateError(RuntimeError):
    """并发操作携带的版本已过期。"""

    def __init__(self, expected, actual):
        super().__init__(f"stale version: expected {expected}, actual {actual}")
        self.expected = expected
        self.actual = actual


class IdempotencyMismatch(RuntimeError):
    pass


class CapacityFull(RuntimeError):
    pass


class StateConflict(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def fingerprint(operation: str, payload: dict) -> str:
    canonical = json.dumps({"operation": operation, "payload": payload}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


@contextmanager
def transaction():
    ensure_schema()
    conn = connect()
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        if WRITE_HOOK is not None:
            WRITE_HOOK(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def ensure_schema() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _row(row):
    return None if row is None else dict(row)


def _task(conn, task_id: str):
    return _row(conn.execute("SELECT * FROM dispatch_tasks WHERE task_id = ?", (task_id,)).fetchone())


def _order(conn, order_id: str):
    return _row(conn.execute("SELECT * FROM dispatch_orders WHERE order_id = ?", (order_id,)).fetchone())


def _active_order(conn, task_id: str):
    return _row(conn.execute("SELECT * FROM dispatch_orders WHERE task_id = ? AND status = ? ORDER BY generation DESC LIMIT 1", (task_id, ORDER_IN_FLIGHT)).fetchone())


def _occupied(conn, site: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM dispatch_orders WHERE site = ? AND status = ?", (site, ORDER_IN_FLIGHT)).fetchone()[0]


def _capacity(conn, site: str) -> int:
    row = conn.execute("SELECT capacity FROM dispatch_sites WHERE site = ?", (site,)).fetchone()
    return int(row[0]) if row else DEFAULT_SITE_CAPACITY


def _ensure_site(conn, site: str, capacity: int | None = None) -> int:
    row = conn.execute("SELECT capacity FROM dispatch_sites WHERE site = ?", (site,)).fetchone()
    if row:
        if capacity is not None and int(capacity) != int(row[0]):
            if int(capacity) < _occupied(conn, site):
                raise StateConflict("CAPACITY_BELOW_OCCUPIED", f"site {site} already holds {_occupied(conn, site)} orders")
            conn.execute("UPDATE dispatch_sites SET capacity = ? WHERE site = ?", (int(capacity), site))
            return int(capacity)
        return int(row[0])
    value = DEFAULT_SITE_CAPACITY if capacity is None else int(capacity)
    if value < 1:
        raise StateConflict("BAD_CAPACITY", "site capacity must be >= 1")
    conn.execute("INSERT INTO dispatch_sites(site, capacity) VALUES (?, ?)", (site, value))
    return value


def _replay(conn, request_id: str | None, operation: str, fp: str):
    if not request_id:
        return None
    row = conn.execute("SELECT * FROM dispatch_requests WHERE request_id = ?", (request_id,)).fetchone()
    if row is None:
        return None
    if row["operation"] != operation or row["fingerprint"] != fp:
        raise IdempotencyMismatch("request_id reused with a different payload")
    return json.loads(row["response"])


def _remember(conn, request_id: str | None, operation: str, fp: str, response: dict, at: str) -> None:
    if not request_id:
        return
    conn.execute(
        "INSERT INTO dispatch_requests(request_id, operation, fingerprint, response, created_at) VALUES (?, ?, ?, ?, ?)",
        (request_id, operation, fp, json.dumps(response, ensure_ascii=False, sort_keys=True), at),
    )


# ---------------------------------------------------------------- 站点容量

def configure_site(site: str, capacity: int) -> dict:
    with transaction() as conn:
        value = _ensure_site(conn, site, capacity)
        return {"site": site, "capacity": value, "in_flight": _occupied(conn, site), "available": value - _occupied(conn, site)}


# ---------------------------------------------------------------- 生成任务

def upsert_tasks(rows: list[dict], at) -> dict:
    """按业务键幂等生成补扫任务；已存在的任务保留其工单进度。"""
    created, existing = [], []
    stamp = iso(at)
    with transaction() as conn:
        for row in rows:
            current = _task(conn, row["task_id"])
            if current is None:
                conn.execute(
                    "INSERT INTO dispatch_tasks(task_id, issue, edition, page, expected_count, status, version, active_order_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, NULL, ?, ?)",
                    (row["task_id"], row["issue"], row["edition"], row["page"], row["expected_count"], PENDING, stamp, stamp),
                )
                created.append(row["task_id"])
            else:
                conn.execute("UPDATE dispatch_tasks SET expected_count = ?, updated_at = ? WHERE task_id = ?", (row["expected_count"], stamp, row["task_id"]))
                existing.append(row["task_id"])
    return {"created": created, "existing": existing}


# ---------------------------------------------------------------- 接单

def _next_pending(conn):
    return _row(conn.execute(
        "SELECT * FROM dispatch_tasks WHERE status = ? ORDER BY created_at, task_id LIMIT 1",
        (PENDING,),
    ).fetchone())


def claim(task_id: str | None, site: str, *, expected_version: int | None, timeout_seconds: int, request_id: str | None, at) -> dict:
    payload = {"task_id": task_id, "site": site, "expected_version": expected_version, "timeout_seconds": timeout_seconds}
    fp = fingerprint("claim", payload)
    start = at
    with transaction() as conn:
        cached = _replay(conn, request_id, "claim", fp)
        if cached is not None:
            return cached

        if task_id is None:
            task = _next_pending(conn)
            if task is None:
                raise StateConflict("QUEUE_EMPTY", "no pending task to claim")
            task_id = task["task_id"]
        else:
            task = _task(conn, task_id)
        if task is None:
            raise StateConflict("TASK_NOT_FOUND", f"task {task_id} not found")
        if expected_version is not None and int(expected_version) != int(task["version"]):
            raise StaleStateError(int(expected_version), int(task["version"]))
        if task["status"] == COMPLETED:
            raise StateConflict("TASK_COMPLETED", f"task {task_id} is already completed")
        if task["status"] == IN_FLIGHT:
            raise StateConflict("TASK_TAKEN", f"task {task_id} is already in flight")

        capacity = _ensure_site(conn, site)
        occupied = _occupied(conn, site)
        if occupied >= capacity:
            raise CapacityFull(f"site {site} is full ({occupied}/{capacity})")

        generation = conn.execute("SELECT COALESCE(MAX(generation), 0) + 1 FROM dispatch_orders WHERE task_id = ?", (task_id,)).fetchone()[0]
        order_id = order_id_for(task_id, generation)
        credential = secrets.token_urlsafe(18)
        deadline = deadline_after(start, timeout_seconds)
        conn.execute(
            "INSERT INTO dispatch_orders(order_id, task_id, site, generation, status, version, credential, issued_at, deadline, timeout_seconds) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (order_id, task_id, site, generation, ORDER_IN_FLIGHT, credential, iso(start), iso(deadline), int(timeout_seconds)),
        )
        conn.execute(
            "UPDATE dispatch_tasks SET status = ?, active_order_id = ?, version = version + 1, updated_at = ? WHERE task_id = ?",
            (IN_FLIGHT, order_id, iso(start), task_id),
        )
        response = {
            "task_id": task_id,
            "order_id": order_id,
            "site": site,
            "generation": generation,
            "status": ORDER_IN_FLIGHT,
            "version": 1,
            "task_version": int(task["version"]) + 1,
            "credential": credential,
            "issued_at": iso(start),
            "deadline": iso(deadline),
            "timeout_seconds": int(timeout_seconds),
        }
        _remember(conn, request_id, "claim", fp, response, iso(start))
        return response


# ---------------------------------------------------------------- 超时转派

def reassign(task_id: str, *, expected_version: int | None, exclude_site: str | None, to_site: str | None, timeout_seconds: int | None, request_id: str | None, at) -> dict:
    payload = {"task_id": task_id, "expected_version": expected_version, "exclude_site": exclude_site, "to_site": to_site, "timeout_seconds": timeout_seconds}
    fp = fingerprint("reassign", payload)
    with transaction() as conn:
        cached = _replay(conn, request_id, "reassign", fp)
        if cached is not None:
            return cached

        task = _task(conn, task_id)
        if task is None:
            raise StateConflict("TASK_NOT_FOUND", f"task {task_id} not found")
        if expected_version is not None and int(expected_version) != int(task["version"]):
            raise StaleStateError(int(expected_version), int(task["version"]))
        if task["status"] != IN_FLIGHT:
            raise StateConflict("NO_ACTIVE_ORDER", f"task {task_id} has no in-flight order")

        old = _active_order(conn, task_id)
        if old is None:
            raise StateConflict("NO_ACTIVE_ORDER", f"task {task_id} has no in-flight order")
        if at < parse_moment(old["deadline"]):
            raise StateConflict("NOT_TIMED_OUT", f"order {old['order_id']} is not timed out yet")

        if to_site is not None:
            if to_site == old["site"]:
                raise StateConflict("SAME_SITE", f"task {task_id} is already dispatched to site {to_site}")
            _ensure_site(conn, to_site)
            if _occupied(conn, to_site) >= _capacity(conn, to_site):
                raise CapacityFull(f"site {to_site} is full")
            new_site = to_site
        else:
            excluded = exclude_site or old["site"]
            candidates = []
            for row in conn.execute("SELECT site FROM dispatch_sites ORDER BY site").fetchall():
                site = row["site"]
                if site == excluded:
                    continue
                if _occupied(conn, site) < _capacity(conn, site):
                    candidates.append(site)
            if not candidates:
                raise CapacityFull(f"no site with free capacity to take task {task_id}")
            new_site = candidates[0]
        keep_timeout = int(timeout_seconds if timeout_seconds is not None else old["timeout_seconds"])
        if keep_timeout < 1:
            raise StateConflict("BAD_TIMEOUT", "timeout must be >= 1 second")

        conn.execute(
            "UPDATE dispatch_orders SET status = ?, version = version + 1, closed_at = ?, close_reason = ? WHERE order_id = ?",
            (ORDER_SUPERSEDED, iso(at), "TIMEOUT_REASSIGNED", old["order_id"]),
        )
        generation = int(old["generation"]) + 1
        order_id = order_id_for(task_id, generation)
        credential = secrets.token_urlsafe(18)
        deadline = deadline_after(at, keep_timeout)
        conn.execute(
            "INSERT INTO dispatch_orders(order_id, task_id, site, generation, status, version, credential, issued_at, deadline, timeout_seconds) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (order_id, task_id, new_site, generation, ORDER_IN_FLIGHT, credential, iso(at), iso(deadline), keep_timeout),
        )
        conn.execute(
            "UPDATE dispatch_tasks SET active_order_id = ?, version = version + 1, updated_at = ? WHERE task_id = ?",
            (order_id, iso(at), task_id),
        )
        response = {
            "task_id": task_id,
            "order_id": order_id,
            "site": new_site,
            "generation": generation,
            "status": ORDER_IN_FLIGHT,
            "version": 1,
            "task_version": int(task["version"]) + 1,
            "credential": credential,
            "issued_at": iso(at),
            "deadline": iso(deadline),
            "timeout_seconds": keep_timeout,
            "superseded_order_id": old["order_id"],
            "superseded_site": old["site"],
        }
        _remember(conn, request_id, "reassign", fp, response, iso(at))
        return response


# ---------------------------------------------------------------- 回执

def receipt(order_id: str, *, credential: str, site: str | None, payload: dict | None, request_id: str | None, at) -> dict:
    body = {"order_id": order_id, "credential": credential, "site": site, "payload": payload or {}}
    fp = fingerprint("receipt", body)
    with transaction() as conn:
        cached = _replay(conn, request_id, "receipt", fp)
        if cached is not None:
            return cached

        order = _order(conn, order_id)
        if order is None:
            raise StateConflict("ORDER_NOT_FOUND", f"order {order_id} not found")
        if not secrets.compare_digest(str(order["credential"]), str(credential)):
            raise StateConflict("INVALID_CREDENTIAL", f"credential does not match order {order_id}")

        task = _task(conn, order["task_id"])
        team = site or order["site"]

        if order["status"] == ORDER_SUPERSEDED:
            existing = conn.execute(
                "SELECT * FROM dispatch_receipts WHERE order_id = ? AND status = ? ORDER BY receipt_id LIMIT 1",
                (order_id, RECEIPT_CONFLICT),
            ).fetchone()
            reason = REASON_LATE_RECEIPT
            receipt_id = existing["receipt_id"] if existing else _insert_receipt(conn, order, team, credential, RECEIPT_CONFLICT, reason, payload, at)
            response = {
                "receipt_id": receipt_id,
                "order_id": order_id,
                "task_id": order["task_id"],
                "site": team,
                "status": RECEIPT_CONFLICT,
                "reason_code": reason,
                "counts_as_completed": False,
                "task_status": task["status"],
                "active_order_id": task["active_order_id"],
                "message": "late receipt kept for review; it does not cancel or complete the new order",
            }
            _remember(conn, request_id, "receipt", fp, response, iso(at))
            return response

        if order["status"] == ORDER_COMPLETED:
            duplicate = conn.execute("SELECT * FROM dispatch_receipts WHERE order_id = ? ORDER BY receipt_id LIMIT 1", (order_id,)).fetchone()
            response = {
                "receipt_id": duplicate["receipt_id"] if duplicate else None,
                "order_id": order_id,
                "task_id": order["task_id"],
                "site": team,
                "status": RECEIPT_COMPLETED,
                "reason_code": None,
                "counts_as_completed": False,
                "task_status": task["status"],
                "active_order_id": task["active_order_id"],
                "message": "order already completed; duplicate receipt ignored",
            }
            _remember(conn, request_id, "receipt", fp, response, iso(at))
            return response

        # 当前在途工单：凭据有效，完成本单。
        conn.execute(
            "UPDATE dispatch_orders SET status = ?, version = version + 1, closed_at = ? WHERE order_id = ?",
            (ORDER_COMPLETED, iso(at), order_id),
        )
        conn.execute(
            "UPDATE dispatch_tasks SET status = ?, active_order_id = NULL, version = version + 1, updated_at = ? WHERE task_id = ?",
            (COMPLETED, iso(at), order["task_id"]),
        )
        receipt_id = _insert_receipt(conn, order, team, credential, RECEIPT_COMPLETED, None, payload, at)
        response = {
            "receipt_id": receipt_id,
            "order_id": order_id,
            "task_id": order["task_id"],
            "site": team,
            "status": RECEIPT_COMPLETED,
            "reason_code": None,
            "counts_as_completed": True,
            "task_status": COMPLETED,
            "active_order_id": None,
            "message": "receipt accepted; task completed",
        }
        _remember(conn, request_id, "receipt", fp, response, iso(at))
        return response


def _insert_receipt(conn, order, site, credential, status, reason, payload, at) -> int:
    cursor = conn.execute(
        "INSERT INTO dispatch_receipts(order_id, task_id, site, credential, status, reason_code, received_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (order["order_id"], order["task_id"], site, credential, status, reason, iso(at), json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)),
    )
    return int(cursor.lastrowid)


# ---------------------------------------------------------------- 回读

def list_tasks(issue: str | None = None, edition: str | None = None, status: str | None = None) -> list[dict]:
    clauses, params = [], []
    if issue:
        clauses.append("t.issue = ?")
        params.append(issue)
    if edition:
        clauses.append("t.edition = ?")
        params.append(edition)
    if status:
        clauses.append("t.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT t.*, o.order_id AS active_order_id_read, o.site AS active_site,
               o.deadline AS active_deadline, o.generation AS active_generation
        FROM dispatch_tasks t
        LEFT JOIN dispatch_orders o ON o.order_id = t.active_order_id AND o.status = '{ORDER_IN_FLIGHT}'
        {where}
        ORDER BY t.created_at, t.task_id
    """
    conn = connect()
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema()
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
    for row in rows:
        row["reason_code"] = "MISSING_PAGE"
        row["active_order"] = None
        if row.get("active_order_id_read"):
            row["active_order"] = {
                "order_id": row["active_order_id_read"],
                "site": row["active_site"],
                "deadline": row["active_deadline"],
                "generation": row["active_generation"],
            }
        for key in ("active_order_id_read", "active_site", "active_deadline", "active_generation"):
            row.pop(key, None)
    return rows


def list_orders(task_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM dispatch_orders"
    params = []
    if task_id:
        sql += " WHERE task_id = ?"
        params.append(task_id)
    sql += " ORDER BY task_id, generation"
    conn = connect()
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema()
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
    for row in rows:
        row.pop("credential", None)
    return rows


def list_inflight(at) -> list[dict]:
    conn = connect()
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema()
        rows = [dict(row) for row in conn.execute(
            "SELECT o.*, t.issue, t.edition, t.page FROM dispatch_orders o JOIN dispatch_tasks t ON t.task_id = o.task_id WHERE o.status = ? ORDER BY o.deadline, o.order_id",
            (ORDER_IN_FLIGHT,),
        ).fetchall()]
    finally:
        conn.close()
    for row in rows:
        row.pop("credential", None)
        row["overdue"] = parse_moment(row["deadline"]) <= at
    return rows


def capacity_snapshot(at) -> dict:
    conn = connect()
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema()
        sites = []
        for row in conn.execute("SELECT site, capacity FROM dispatch_sites ORDER BY site").fetchall():
            occupied = _occupied(conn, row["site"])
            sites.append({"site": row["site"], "capacity": int(row["capacity"]), "in_flight": occupied, "available": int(row["capacity"]) - occupied})
        pending = conn.execute("SELECT COUNT(*) FROM dispatch_tasks WHERE status = ?", (PENDING,)).fetchone()[0]
        queue = [dict(row) for row in conn.execute(
            "SELECT task_id, issue, edition, page, created_at FROM dispatch_tasks WHERE status = ? ORDER BY created_at, task_id",
            (PENDING,),
        ).fetchall()]
    finally:
        conn.close()
    return {"sites": sites, "pending": int(pending), "queue": queue, "checked_at": iso(at)}


def list_receipts(status: str | None = None, task_id: str | None = None) -> list[dict]:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = connect()
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema()
        rows = [dict(row) for row in conn.execute(f"SELECT * FROM dispatch_receipts {where} ORDER BY receipt_id", params).fetchall()]
    finally:
        conn.close()
    for row in rows:
        row.pop("credential", None)
    return rows


def summary(at) -> dict:
    tasks = list_tasks()
    counts = {PENDING: 0, IN_FLIGHT: 0, COMPLETED: 0}
    for task in tasks:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    inflight = list_inflight(at)
    conflicts = list_receipts(status=RECEIPT_CONFLICT)
    overdue = sum(1 for row in inflight if row["overdue"])
    return {
        "tasks": len(tasks),
        "missing": len(tasks),
        "pending": counts.get(PENDING, 0),
        "in_flight": counts.get(IN_FLIGHT, 0),
        "in_flight_valid": counts.get(IN_FLIGHT, 0) - overdue,
        "completed": counts.get(COMPLETED, 0),
        "overdue": overdue,
        "conflict_pending": len(conflicts),
        "capacity": capacity_snapshot(at),
    }


def verify(at) -> dict:
    """核对缺页任务、有效在途与容量占用是否自洽，不修改任何状态。"""
    conn = connect()
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema()
        tasks = [dict(row) for row in conn.execute("SELECT * FROM dispatch_tasks").fetchall()]
        orders = [dict(row) for row in conn.execute("SELECT * FROM dispatch_orders").fetchall()]
        sites = [dict(row) for row in conn.execute("SELECT site, capacity FROM dispatch_sites").fetchall()]
        receipts = [dict(row) for row in conn.execute("SELECT * FROM dispatch_receipts").fetchall()]
    finally:
        conn.close()

    by_task: dict[str, list[dict]] = {}
    for order in orders:
        by_task.setdefault(order["task_id"], []).append(order)
    occupied: dict[str, int] = {}
    for order in orders:
        if order["status"] == ORDER_IN_FLIGHT:
            occupied[order["site"]] = occupied.get(order["site"], 0) + 1

    violations = []
    counts = {PENDING: 0, IN_FLIGHT: 0, COMPLETED: 0}
    overdue = 0
    for task in tasks:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
        active = [o for o in by_task.get(task["task_id"], []) if o["status"] == ORDER_IN_FLIGHT]
        if task["status"] == IN_FLIGHT and len(active) != 1:
            violations.append({"rule": "ONE_ACTIVE_ORDER", "task_id": task["task_id"], "actual": len(active)})
        if task["status"] != IN_FLIGHT and active:
            violations.append({"rule": "NO_ACTIVE_FOR_IDLE_TASK", "task_id": task["task_id"], "actual": len(active)})
        if active and active[0]["order_id"] != task["active_order_id"]:
            violations.append({"rule": "ACTIVE_ORDER_POINTER", "task_id": task["task_id"], "actual": active[0]["order_id"]})
        if active and parse_moment(active[0]["deadline"]) <= at:
            overdue += 1

    capacity = []
    for site in sites:
        used = occupied.get(site["site"], 0)
        capacity.append({"site": site["site"], "capacity": int(site["capacity"]), "in_flight": used, "available": int(site["capacity"]) - used})
        if used > int(site["capacity"]):
            violations.append({"rule": "CAPACITY_EXCEEDED", "site": site["site"], "actual": used, "limit": int(site["capacity"])})

    completed_orders = {o["order_id"] for o in orders if o["status"] == ORDER_COMPLETED}
    accepted_receipts = {r["order_id"] for r in receipts if r["status"] == RECEIPT_COMPLETED}
    for order_id in sorted(completed_orders - accepted_receipts):
        violations.append({"rule": "MISSING_RECEIPT", "order_id": order_id})

    return {
        "checked_at": iso(at),
        "tasks": {"total": len(tasks), "pending": counts.get(PENDING, 0), "in_flight": counts.get(IN_FLIGHT, 0), "completed": counts.get(COMPLETED, 0), "overdue": overdue},
        "tasks_balanced": len(tasks) == sum(counts.values()),
        "in_flight": len([o for o in orders if o["status"] == ORDER_IN_FLIGHT]),
        "in_flight_valid": len([o for o in orders if o["status"] == ORDER_IN_FLIGHT]) - overdue,
        "capacity": capacity,
        "receipts": {"completed": len(accepted_receipts), "conflict_pending": len([r for r in receipts if r["status"] == RECEIPT_CONFLICT])},
        "violations": violations,
        "ok": not violations,
    }

