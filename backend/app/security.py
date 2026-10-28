"""
أدوات أمان مشتركة: استخراج IP العميل بشكل آمن + حماية تسجيل الدخول من التخمين.

- IP العميل: الافتراضي request.client.host. لو السيرفر ورا Proxy موثوق (TRUST_PROXY_HEADERS=1)
  بناخد الـ IP اللي الـ Proxy نفسه شافه من X-Forwarded-For (من اليمين، حسب TRUSTED_PROXY_COUNT)
  مش أول قيمة (لأن أول قيمة العميل يقدر يزوّرها).
- الحماية: حد محاولات فاشلة لكل IP، وحد أعلى لكل اسم مستخدم (عشان تخمين موزّع على IPs كتير)،
  والعدّاد متخزن في قاعدة البيانات.
"""
import os
from datetime import datetime, timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy import func

from . import models

TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "0") == "1"
TRUSTED_PROXY_COUNT = max(1, int(os.getenv("TRUSTED_PROXY_COUNT", "1")))
MAX_IP_FAILURES = int(os.getenv("LOGIN_MAX_FAILURES", "5"))
MAX_USER_FAILURES = int(os.getenv("LOGIN_MAX_FAILURES_PER_USER", "20"))
WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", str(15 * 60)))


def client_ip(request: Request) -> str:
    if TRUST_PROXY_HEADERS:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            parts = [p.strip() for p in fwd.split(",") if p.strip()]
            if parts:
                return parts[-TRUSTED_PROXY_COUNT] if len(parts) >= TRUSTED_PROXY_COUNT else parts[0]
    return request.client.host if request.client else "unknown"


class LoginLimiter:
    """
    عدّاد محاولات الدخول الفاشلة في جدول login_attempts (مش في الذاكرة):
    - حد لكل IP وحد أعلى لكل اسم مستخدم (ضد التخمين الموزّع).
    - بيشتغل صح مع أكتر من worker/process وبيفضل بعد إعادة تشغيل السيرفر.
    - الصفوف القديمة بتتمسح تلقائيًا.
    """

    @staticmethod
    def _now() -> datetime:
        return datetime.utcnow()

    def _count(self, db, column, value, since) -> int:
        return db.query(func.count(models.LoginAttempt.id)).filter(column == value, models.LoginAttempt.attempted_at >= since).scalar() or 0

    def check(self, db, ip: str, username: str):
        since = self._now() - timedelta(seconds=WINDOW_SECONDS)
        for column, value, limit in (
            (models.LoginAttempt.ip, ip, MAX_IP_FAILURES),
            (models.LoginAttempt.username, username, MAX_USER_FAILURES),
        ):
            if self._count(db, column, value, since) >= limit:
                oldest = (
                    db.query(func.min(models.LoginAttempt.attempted_at))
                    .filter(column == value, models.LoginAttempt.attempted_at >= since)
                    .scalar()
                )
                wait = int(WINDOW_SECONDS - (self._now() - oldest).total_seconds()) + 1 if oldest else WINDOW_SECONDS
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"محاولات دخول غلط كتير. حاول تاني بعد {max(1, wait // 60)} دقيقة تقريبًا.",
                    headers={"Retry-After": str(max(1, wait))},
                )

    def record_failure(self, db, ip: str, username: str):
        now = self._now()
        db.add(models.LoginAttempt(ip=ip[:64], username=username[:64], attempted_at=now))
        # تنضيف الصفوف المنتهية (رخيص بسبب الفهرس)
        db.query(models.LoginAttempt).filter(
            models.LoginAttempt.attempted_at < now - timedelta(seconds=WINDOW_SECONDS)
        ).delete(synchronize_session=False)
        db.commit()  # لازم يتسجّل حتى لو الطلب هيرد بـ 401

    def clear(self, db, ip: str, username: str):
        # بنمسح بس محاولات (نفس الـ IP + نفس الاسم): دخول ناجح لحساب تاني ما يصفّرش عدّاد IP بيخمّن حاجة تانية
        db.query(models.LoginAttempt).filter(
            models.LoginAttempt.ip == ip, models.LoginAttempt.username == username
        ).delete(synchronize_session=False)
        db.commit()


login_limiter = LoginLimiter()
