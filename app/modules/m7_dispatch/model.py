"""补扫派工的标识、时间与状态常量。"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import TIMEZONE

# 任务状态
PENDING = "PENDING"
IN_FLIGHT = "IN_FLIGHT"
COMPLETED = "COMPLETED"

# 工单状态（原单永不删除，只关闭）
ORDER_IN_FLIGHT = "IN_FLIGHT"
ORDER_COMPLETED = "COMPLETED"
ORDER_SUPERSEDED = "SUPERSEDED"

# 回执状态
RECEIPT_COMPLETED = "COMPLETED"
RECEIPT_CONFLICT = "CONFLICT_PENDING"

# 稳定原因码
REASON_LATE_RECEIPT = "LATE_RECEIPT"
REASON_TIMEOUT_REASSIGN = "TIMEOUT_REASSIGNED"
REASON_TASK_MISSING = "MISSING_PAGE"

DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_SITE_CAPACITY = 1


def now() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def iso(moment: datetime) -> str:
    return moment.isoformat()


def parse_moment(value: str | None, fallback: datetime | None = None) -> datetime:
    if value is None:
        return fallback or now()
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo(TIMEZONE))
    return parsed


def deadline_after(start: datetime, timeout_seconds: int) -> datetime:
    return start + timedelta(seconds=int(timeout_seconds))


def task_id_for(issue: str, edition: str, page: str) -> str:
    """任务标识只由期版页序决定，重复生成不会产生新身份。"""
    return f"T-{issue}-{edition}-{page}"


def order_id_for(task_id: str, generation: int) -> str:
    return f"O-{task_id}-{generation:02d}"
