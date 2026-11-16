from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.web import page

from .service import claim, configure_site, generate, orders, reassign, receipt, receipts_read, summary, tasks, verify

router = APIRouter()
ROWS = [
    {"key": "2026-11-01|01|004", "status": "在途", "value": "SITE-A"},
    {"key": "2026-11-01|01|007", "status": "待接单", "value": "队列 1"},
    {"key": "2026-11-02|02|003", "status": "冲突待核", "value": "1"},
]


@router.get("/dispatch", response_class=HTMLResponse)
def view(request: Request):
    return page(request, "m7.html", "补扫派工", "/api/dispatch/summary", ROWS)


@router.get("/api/dispatch/summary")
def read_summary(at: str | None = None):
    return summary({"at": at})


@router.post("/api/dispatch/summary")
def run_summary(payload: dict | None = None):
    return summary(payload or {})


@router.post("/api/dispatch/generate")
def generate_tasks(payload: dict | None = None):
    return generate(payload or {})


@router.get("/api/dispatch/verify")
def read_verify(at: str | None = None):
    return verify({"at": at})


@router.get("/api/dispatch/tasks")
def read_tasks(issue: str | None = None, edition: str | None = None, status: str | None = None, at: str | None = None):
    return tasks({"issue": issue, "edition": edition, "status": status, "at": at})


@router.get("/api/dispatch/orders")
def read_orders(task_id: str | None = None):
    return orders({"task_id": task_id})


@router.get("/api/dispatch/receipts")
def read_receipts(status: str | None = None, task_id: str | None = None):
    return receipts_read({"status": status, "task_id": task_id})


@router.post("/api/dispatch/sites")
def set_site(payload: dict):
    return configure_site(payload)


@router.post("/api/dispatch/claim")
def take_task(payload: dict):
    return claim(payload)


@router.post("/api/dispatch/reassign")
def transfer_task(payload: dict):
    return reassign(payload)


@router.post("/api/dispatch/receipt")
def accept_receipt(payload: dict):
    return receipt(payload)
