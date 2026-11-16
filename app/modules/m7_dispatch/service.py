"""补扫派工业务层：从真实期版页序生成缺页任务，再按站点容量接单/转派。"""

from app.errors import DomainError
from app.modules.m1_intake.parser import parse_manifest
from app.modules.m2_issue.rules import expected_pages, normalize_edition, normalize_issue, normalize_page

from . import repo
from .model import (
    DEFAULT_TIMEOUT_SECONDS,
    REASON_TASK_MISSING,
    parse_moment,
    task_id_for,
)


def _at(value):
    return parse_moment(value)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise DomainError("BAD_REQUEST", f"expected an integer, got {value!r}", 422)


def _conflict(exc: Exception) -> DomainError:
    if isinstance(exc, repo.StaleStateError):
        return DomainError("STALE_VERSION", f"过期版本：期望 {exc.expected}，当前 {exc.actual}", 409)
    if isinstance(exc, repo.CapacityFull):
        return DomainError("CAPACITY_FULL", str(exc), 409)
    if isinstance(exc, repo.IdempotencyMismatch):
        return DomainError("IDEMPOTENCY_MISMATCH", str(exc), 409)
    if isinstance(exc, repo.StateConflict):
        status = 404 if exc.code.endswith("_NOT_FOUND") else 409
        return DomainError(exc.code, exc.message, status)
    return DomainError("WRITE_FAILED", f"派工写入失败，已回滚：{exc}", 503)


def _guard(fn):
    try:
        return fn()
    except DomainError:
        raise
    except (repo.StaleStateError, repo.CapacityFull, repo.IdempotencyMismatch, repo.StateConflict) as exc:
        raise _conflict(exc) from exc
    except Exception as exc:  # 写入失败已回滚，可安全重试
        raise _conflict(exc) from exc


# ---------------------------------------------------------------- 生成

def _rows_from_text(text: str) -> tuple[list[dict], list[dict]]:
    good, bad = parse_manifest(text)
    return good, [
        {"line_no": item.get("line_no"), "reason_code": item.get("reason_code"), "raw": item.get("raw")}
        for item in bad
    ]


def _rows_from_payload(payload: dict) -> tuple[list[dict], list[dict]]:
    """返回 (按业务键分组的页序, 输入问题)。支持结构化行或原始清单文本。"""
    text = payload.get("text", payload.get("manifest"))
    rows_field = payload.get("rows", payload.get("scans"))
    problems: list[dict] = []
    if text is not None:
        rows, problems = _rows_from_text(text)
    elif rows_field is not None:
        rows = list(rows_field)
    else:
        rows = []
        for group in payload.get("issues", []):
            for page in group.get("pages", []):
                row = {**page, "issue": group["issue"], "edition": group["edition"]}
                if group.get("expected_count") is not None:
                    row["expected_count"] = group["expected_count"]
                rows.append(row)

    groups: dict[tuple[str, str], list[dict]] = {}
    for index, row in enumerate(rows, 1):
        try:
            issue = normalize_issue(str(row["issue"]))
            edition = normalize_edition(str(row["edition"]))
            page = normalize_page(str(row["page"]))
        except (KeyError, ValueError) as exc:
            problems.append({"line_no": row.get("line_no", index), "reason_code": "BAD_BUSINESS_KEY", "raw": str(exc)})
            continue
        groups.setdefault((issue, edition), []).append({**row, "issue": issue, "edition": edition, "page": page})
    grouped = []
    for (issue, edition), items in sorted(groups.items()):
        declared = {int(item["expected_count"]) for item in items if item.get("expected_count") is not None}
        grouped.append({
            "issue": issue,
            "edition": edition,
            "rows": items,
            "expected_count": max(declared) if declared else None,
        })
    return grouped, problems


def _expected_count(group: dict, declared: int | None) -> int:
    """声明值优先；未声明时以真实页序中的最大页码作为下界。"""
    if declared is not None:
        return int(declared)
    if group.get("expected_count") is not None:
        return int(group["expected_count"])
    return max(int(row["page"]) for row in group["rows"])


def generate(payload: dict | None = None, *, at=None) -> dict:
    payload = payload or {}
    moment = _at(at or payload.get("at"))
    groups, problems = _rows_from_payload(payload)
    declared = payload.get("expected_counts") or {}

    tasks: list[dict] = []
    skipped: list[dict] = []
    for group in groups:
        key = f"{group['issue']}|{group['edition']}"
        try:
            count = _expected_count(group, declared.get(key))
        except (TypeError, ValueError):
            problems.append({"business_key": key, "reason_code": "BAD_COUNT", "raw": declared.get(key)})
            continue
        if count < 1:
            problems.append({"business_key": key, "reason_code": "BAD_COUNT", "raw": count})
            continue
        present = {row["page"] for row in group["rows"]}
        missing = [page for page in expected_pages(count) if page not in present]
        for page in missing:
            tasks.append({
                "task_id": task_id_for(group["issue"], group["edition"], page),
                "issue": group["issue"],
                "edition": group["edition"],
                "page": page,
                "expected_count": count,
            })
        if not missing:
            skipped.append({"business_key": key, "reason_code": "COMPLETE", "missing": 0})

    seen, unique = set(), []
    for task in tasks:
        if task["task_id"] in seen:
            continue
        seen.add(task["task_id"])
        unique.append(task)

    result = repo.upsert_tasks(unique, moment)
    return {
        "run_at": moment.isoformat(),
        "generated": len(unique),
        "created": result["created"],
        "existing": result["existing"],
        "tasks": [task["task_id"] for task in unique],
        "complete_groups": skipped,
        "rejected": problems,
        "reason_code": REASON_TASK_MISSING,
    }


# ---------------------------------------------------------------- 站点

def configure_site(payload: dict) -> dict:
    site = str(payload.get("site") or "").strip()
    if not site:
        raise DomainError("BAD_SITE", "site is required", 422)
    capacity = payload.get("capacity", 1)
    try:
        capacity = int(capacity)
    except (TypeError, ValueError):
        raise DomainError("BAD_CAPACITY", "capacity must be an integer", 422)
    if capacity < 1:
        raise DomainError("BAD_CAPACITY", "capacity must be >= 1", 422)
    return _guard(lambda: repo.configure_site(site, capacity))


# ---------------------------------------------------------------- 接单

def claim(payload: dict) -> dict:
    task_id = str(payload.get("task_id") or "").strip() or None
    site = str(payload.get("site") or "").strip()
    if not site:
        raise DomainError("BAD_REQUEST", "site is required", 422)
    timeout = payload.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        raise DomainError("BAD_TIMEOUT", "timeout_seconds must be an integer", 422)
    if timeout < 1:
        raise DomainError("BAD_TIMEOUT", "timeout_seconds must be >= 1", 422)
    expected = payload.get("expected_version")
    expected = None if expected is None else _int(expected)
    moment = _at(payload.get("at"))
    return _guard(lambda: repo.claim(
        task_id,
        site,
        expected_version=expected,
        timeout_seconds=timeout,
        request_id=payload.get("request_id"),
        at=moment,
    ))


# ---------------------------------------------------------------- 转派

def reassign(payload: dict) -> dict:
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        raise DomainError("BAD_REQUEST", "task_id is required", 422)
    expected = payload.get("expected_version")
    expected = None if expected is None else _int(expected)
    timeout = payload.get("timeout_seconds")
    if timeout is not None:
        timeout = _int(timeout)
        if timeout < 1:
            raise DomainError("BAD_TIMEOUT", "timeout_seconds must be >= 1", 422)
    moment = _at(payload.get("at"))
    return _guard(lambda: repo.reassign(
        task_id,
        expected_version=expected,
        exclude_site=payload.get("exclude_site"),
        to_site=payload.get("to_site"),
        timeout_seconds=timeout,
        request_id=payload.get("request_id"),
        at=moment,
    ))


# ---------------------------------------------------------------- 回执

def receipt(payload: dict) -> dict:
    order_id = str(payload.get("order_id") or "").strip()
    credential = payload.get("credential")
    if not order_id or not credential:
        raise DomainError("BAD_REQUEST", "order_id and credential are required", 422)
    moment = _at(payload.get("at"))
    return _guard(lambda: repo.receipt(
        order_id,
        credential=credential,
        site=payload.get("site"),
        payload=payload.get("payload"),
        request_id=payload.get("request_id"),
        at=moment,
    ))


# ---------------------------------------------------------------- 回读

def tasks(filters: dict | None = None) -> dict:
    filters = filters or {}
    rows = repo.list_tasks(issue=filters.get("issue"), edition=filters.get("edition"), status=filters.get("status"))
    moment = _at(filters.get("at"))
    overdue_ids = {row["order_id"] for row in repo.list_inflight(moment) if row["overdue"]}
    for row in rows:
        order = row.get("active_order")
        row["active_valid"] = bool(order) and order["order_id"] not in overdue_ids
        row["overdue"] = bool(order) and order["order_id"] in overdue_ids
    return {"tasks": rows, "count": len(rows)}


def summary(payload: dict | None = None) -> dict:
    payload = payload or {}
    return repo.summary(_at(payload.get("at")))


def verify(payload: dict | None = None) -> dict:
    payload = payload or {}
    return repo.verify(_at(payload.get("at")))


def orders(payload: dict | None = None) -> dict:
    payload = payload or {}
    return {"orders": repo.list_orders(payload.get("task_id"))}


def receipts_read(payload: dict | None = None) -> dict:
    payload = payload or {}
    rows = repo.list_receipts(status=payload.get("status"), task_id=payload.get("task_id"))
    return {"receipts": rows, "count": len(rows)}


