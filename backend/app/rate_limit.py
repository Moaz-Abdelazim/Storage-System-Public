"""
Rate limiter خفيف (in-process) للـ endpoints الحساسة غير الدخول (تغيير الباسورد، import، إنشاء مستخدم).
⚠ حدوده: العدّاد في ذاكرة الـ process الواحد. مع أكتر من worker الحد الفعلي = الحد × عدد الـ workers، وبيتصفّر
مع إعادة التشغيل. ده مقصود (حماية من الإساءة العادية مش من هجوم موزّع)؛ تخمين الباسورد عند الدخول
محمي بشكل منفصل وثابت في الـ DB (security.LoginLimiter).
"""
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import Depends, HTTPException, status

from . import auth, models

_lock = threading.Lock()
_hits: Dict[str, Deque[float]] = defaultdict(deque)


def reset_all() -> None:
    with _lock:
        _hits.clear()


def hit(key: str, max_calls: int, window_seconds: int) -> None:
    """بيسجّل محاولة، وبيرمي 429 (مع Retry-After) لو تعدّت الحد في النافذة."""
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        while q and q[0] <= now - window_seconds:
            q.popleft()
        if len(q) >= max_calls:
            retry = max(1, int(window_seconds - (now - q[0])))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"محاولات كتير. جرّب تاني بعد {retry} ثانية",
                headers={"Retry-After": str(retry)},
            )
        q.append(now)


def limit_per_user(name: str, max_calls: int, window_seconds: int):
    """dependency: حد لكل مستخدم مسجّل لعملية معيّنة."""

    def dependency(current_user: models.User = Depends(auth.get_current_user)) -> None:
        hit(f"{name}:{current_user.id}", max_calls, window_seconds)

    return dependency
