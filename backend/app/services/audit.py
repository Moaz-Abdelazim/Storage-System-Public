"""
سجل التدقيق: دالة واحدة audit() بتكتب صف في audit_log **جوه نفس الـ transaction** بتاع العملية
(يعني لو العملية اتعملها rollback السجل بيتلغي معاها، ولو اتعملها commit السجل بيتحفظ معاها).
الـ IP بيتحط من middleware في main.py عن طريق contextvar (مش محتاجين نمرر request لكل دالة).
"""
import json
from contextvars import ContextVar
from typing import Any, Optional

from sqlalchemy.orm import Session

from .. import models

current_ip: ContextVar[Optional[str]] = ContextVar("current_ip", default=None)
_MAX_SUMMARY = 8000


def audit(db: Session, user: Optional[models.User], entity_type: str, entity_id: Any, action: str, summary: Any = None) -> None:
    """بتضيف صف تدقيق للـ session (من غير commit). summary: dict/list/نص مختصر بالعربي (قبل/بعد)."""
    if isinstance(summary, (dict, list)):
        text = json.dumps(summary, ensure_ascii=False, default=str)
    else:
        text = None if summary is None else str(summary)
    if text and len(text) > _MAX_SUMMARY:
        text = text[: _MAX_SUMMARY - 1] + "…"
    db.add(models.AuditLog(
        user_id=getattr(user, "id", None),
        username=getattr(user, "username", None),
        ip=current_ip.get(),
        entity_type=entity_type,
        entity_id=None if entity_id is None else str(entity_id),
        action=action,
        summary=text,
    ))
