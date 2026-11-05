"""
حالة جلسات الدخول (login_logs). كل صف هو جلسة فعلية بتتحقق منها كل طلب (التوكن فيه sid).

الحالة بتتحسب من البيانات المخزّنة مش من وجود logout_time بس:
  active      مفتوحة: logout_time فاضي و expires_at لسه ما جاش
  expired     انتهت صلاحيتها تلقائيًا (expires_at عدّى) - مش تسجيل خروج
  logged_out  المستخدم سجّل خروج بنفسه
  revoked     أنهاها أدمن (إنهاء جلسة، إيقاف حساب/دور، تغيير باسورد/اسم من أدمن)
  system      اتقفلت تلقائيًا من النظام (المستخدم غيّر باسورده، أو الحساب اتمسح)

expires_at بيتحسب وقت الدخول بنفس مدة التوكن (ACCESS_TOKEN_EXPIRE_MINUTES). لو الجلسة عدّى وقتها ومحدش لمسها،
أي تحقق/دخول/عرض للسجل بيقفلها بـ end_reason='expired' و logout_time = expires_at (مش وقت الاكتشاف) في UPDATE واحد شرطي.
"""
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import models
from ..timezone_utils import now_cairo

END_REASONS = ("logout", "expired", "revoked", "system")
L = models.LoginLog


def expiry_for(login_time: datetime, minutes: int) -> datetime:
    return login_time + timedelta(minutes=minutes)


def expire_stale_sessions(db: Session, now: Optional[datetime] = None) -> int:
    """بيقفل كل الجلسات المفتوحة اللي عدّى expires_at بتاعها. بيرجّع عددها. (من غير commit)"""
    now = now or now_cairo()
    res = db.execute(
        update(L).where(L.logout_time.is_(None), L.expires_at.isnot(None), L.expires_at <= now)
        .values(logout_time=L.expires_at, end_reason="expired"),
        execution_options={"synchronize_session": False},
    )
    return res.rowcount or 0


def expire_session(db: Session, session_id: int) -> int:
    """بيقفل جلسة واحدة كمنتهية (لو لسه مفتوحة). مبيعتمدش على الساعة: بيتنادى لما التوكن نفسه ثبت إنه منتهي."""
    res = db.execute(
        update(L).where(L.id == session_id, L.logout_time.is_(None))
        .values(logout_time=now_cairo(), end_reason="expired"),
        execution_options={"synchronize_session": False},
    )
    return res.rowcount or 0


def close_open_sessions(db: Session, *conds, reason: str, closed_by_id: Optional[int] = None) -> int:
    """يقفل الجلسات المفتوحة المطابقة (بعد ما يقفل المنتهية الأول عشان سببها يفضل 'expired'). من غير commit."""
    assert reason in END_REASONS
    now = now_cairo()
    expire_stale_sessions(db, now)
    res = db.execute(
        update(L).where(L.logout_time.is_(None), *conds).values(logout_time=now, end_reason=reason, closed_by_id=closed_by_id),
        execution_options={"synchronize_session": False},
    )
    return res.rowcount or 0


def session_status(log: models.LoginLog, now: Optional[datetime] = None) -> str:
    now = now or now_cairo()
    if log.logout_time is None:
        return "expired" if (log.expires_at is not None and log.expires_at <= now) else "active"
    reason = log.end_reason
    if reason == "expired":
        return "expired"
    if reason == "revoked" or (reason is None and log.closed_by_id is not None):
        return "revoked"
    if reason == "system":
        return "system"
    return "logged_out"


def ended_at(log: models.LoginLog, now: Optional[datetime] = None) -> Optional[datetime]:
    """وقت انتهاء الجلسة: وقت الخروج/الإنهاء، أو expires_at للمنتهية تلقائيًا (حتى لو لسه ما اتقفلتش في الـ DB)."""
    if log.logout_time is not None:
        return log.logout_time
    return log.expires_at if session_status(log, now) == "expired" else None
