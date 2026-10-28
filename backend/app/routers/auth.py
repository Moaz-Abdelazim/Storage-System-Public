import math
from datetime import datetime, timedelta
from typing import Optional

from ..timezone_utils import now_cairo

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session, joinedload

from .. import models, schemas, auth
from ..database import get_db
from ..display_utils import format_user_display
from ..security import client_ip, login_limiter
from ..query_helpers import contains, page_args, paged

router = APIRouter(prefix="/api/auth", tags=["Auth"])

# حماية تسجيل الدخول من التخمين: انظر app/security.py (حد لكل IP + حد لكل اسم مستخدم)
# هاش وهمي بنتحقق منه لما اسم المستخدم مش موجود، عشان زمن الرد يبقى متقارب ومفيش حد يعرف
# مين موجود ومين لأ من سرعة الرد (User Enumeration by timing)
_DUMMY_HASH = auth.get_password_hash("timing-equalizer-not-a-real-password")


def _escape_like(value: str) -> str:
    """بيهرّب % و _ في بحث LIKE عشان يتعاملوا كحروف عادية."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _active_admin_count(db: Session, exclude_user_id: int = None) -> int:
    q = db.query(models.User).filter(models.User.role == "admin", models.User.is_active == True)  # noqa: E712
    if exclude_user_id is not None:
        q = q.filter(models.User.id != exclude_user_id)
    return q.count()


@router.post("/login", response_model=schemas.Token)
def login(payload: schemas.LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = client_ip(request)
    username = payload.username.strip()
    login_limiter.check(db, ip, username.lower())

    user = db.query(models.User).filter(models.User.username == username).first()
    password_ok = auth.verify_password(payload.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not password_ok:
        login_limiter.record_failure(db, ip, username.lower())
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="اسم المستخدم أو كلمة المرور غلط")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="الحساب موقوف")

    login_limiter.clear(db, ip, username.lower())

    # الجلسة (login_log) بتتعمل الأول عشان ناخد الـ id بتاعها ونحطه جوه التوكن نفسه (sid) -
    # ده اللي بيخلي أي إغلاق للجلسة دي (من المستخدم أو من الأدمن) يبطل التوكن فورًا
    log = models.LoginLog(
        user_id=user.id,
        username=user.username,
        login_time=now_cairo(),
        ip_address=ip,
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    token = auth.create_access_token({"sub": user.username, "sid": log.id})
    auth.set_session_cookie(response, token)  # التوكن في HttpOnly cookie فقط - مش بنرجّعه في الـ body
    return schemas.Token(username=user.username, role=user.role)


@router.post("/logout")
def logout(
    response: Response,
    result: tuple = Depends(auth.get_current_user_and_session),
    db: Session = Depends(get_db),
):
    _user, session = result
    session.logout_time = now_cairo()
    db.commit()
    auth.clear_session_cookie(response)
    return {"message": "تم تسجيل الخروج"}


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    result: tuple = Depends(auth.get_current_user_and_session),
    db: Session = Depends(get_db),
):
    """المستخدم بيغيّر باسورده بنفسه (لازم يكتب الحالي). بيقفل باقي جلساته المفتوحة وبيسيب الجلسة الحالية."""
    user, session = result
    if not auth.verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="كلمة المرور الحالية غلط")
    if payload.new_password == payload.current_password:
        raise HTTPException(status_code=400, detail="كلمة المرور الجديدة لازم تختلف عن الحالية")
    user.password_hash = auth.get_password_hash(payload.new_password)
    db.query(models.LoginLog).filter(
        models.LoginLog.user_id == user.id, models.LoginLog.logout_time.is_(None), models.LoginLog.id != session.id
    ).update({"logout_time": now_cairo()}, synchronize_session=False)
    db.commit()
    return {"message": "تم تغيير كلمة المرور"}


@router.get("/me", response_model=schemas.UserOut)
def me(current_user: models.User = Depends(auth.get_current_user)):
    return current_user


@router.post("/users", response_model=schemas.UserOut)
def create_user(
    payload: schemas.UserCreate,
    db: Session = Depends(get_db),
    admin: models.User = Depends(auth.require_admin),
):
    if db.query(models.User).filter(models.User.username == payload.username).first():
        raise HTTPException(status_code=400, detail="اسم المستخدم موجود بالفعل")
    user = models.User(
        username=payload.username,
        full_name=payload.full_name,
        password_hash=auth.get_password_hash(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get("/usernames")
def list_usernames(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    """قايمة خفيفة بأسماء المستخدمين بس (لأي مستخدم مسجل دخول) - تستخدم في اقتراحات البحث
    (زي البحث في سجل الحركات عن مين سلّم/استلم)، مش لإدارة المستخدمين الكاملة."""
    users = db.query(models.User.username).filter(models.User.is_active == True).order_by(models.User.username).all()
    return [u[0] for u in users]


def _user_row(u: models.User) -> dict:
    return {"id": u.id, "username": u.username, "full_name": u.full_name, "role": u.role, "is_active": u.is_active, "created_at": u.created_at}


@router.get("/users/stats")
def users_stats(db: Session = Depends(get_db), admin: models.User = Depends(auth.require_admin)):
    """أعداد سريعة للكروت: الكل / الأدمن / الموظفين / النشطين / الموقوفين (بضربة واحدة)."""
    U = models.User
    row = db.query(
        func.count(U.id),
        func.coalesce(func.sum(case((U.role == "admin", 1), else_=0)), 0),
        func.coalesce(func.sum(case((U.is_active.is_(True), 1), else_=0)), 0),
    ).first()
    total, admins, active = row[0] or 0, int(row[1] or 0), int(row[2] or 0)
    return {"total": total, "admins": admins, "staff": total - admins, "active": active, "inactive": total - active}


@router.get("/users")
def list_users(
    search: Optional[str] = None,
    role: Optional[str] = None,  # admin / staff
    status: Optional[str] = None,  # active / inactive
    page: Optional[int] = None,  # من غير page = القايمة كلها (للتوافق)
    page_size: int = 15,
    db: Session = Depends(get_db),
    admin: models.User = Depends(auth.require_admin),
):
    """المستخدمين (أدمن). مع page: ترقيم وبحث وفلترة بالصلاحية/الحالة من قاعدة البيانات."""
    U = models.User
    q = db.query(U)
    if search and search.strip():
        q = q.filter(contains(U.username, search) | contains(U.full_name, search))
    if role in ("admin", "staff"):
        q = q.filter(U.role == role)
    if status == "active":
        q = q.filter(U.is_active.is_(True))
    elif status == "inactive":
        q = q.filter(U.is_active.is_(False))
    q = q.order_by(U.username)
    if page is None:
        return [_user_row(u) for u in q.all()]
    pg, size = page_args(page, page_size)
    total = q.count()
    return paged([_user_row(u) for u in q.offset((pg - 1) * size).limit(size).all()], total, pg, size)


@router.put("/users/{user_id}", response_model=schemas.UserOut)
def update_user(
    user_id: int,
    payload: schemas.UserUpdate,
    db: Session = Depends(get_db),
    admin_session: tuple = Depends(auth.require_admin_session),
):
    admin, current_session = admin_session
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="المستخدم مش موجود")

    data = payload.model_dump(exclude_unset=True)

    # حماية من إن النظام يفضل من غير أدمن شغّال (تعطيل/تنزيل آخر أدمن، أو الأدمن يوقف نفسه)
    will_be_admin = (data.get("role") or user.role) == "admin"
    will_be_active = data.get("is_active") if data.get("is_active") is not None else user.is_active
    if user.role == "admin" and user.is_active and (not will_be_admin or not will_be_active):
        if user.id == admin.id and not will_be_active:
            raise HTTPException(status_code=400, detail="متقدرش توقف حسابك انت وانت مسجل دخول بيه")
        if _active_admin_count(db, exclude_user_id=user.id) == 0:
            raise HTTPException(status_code=400, detail="دي آخر أدمن شغّال في النظام - مينفعش توقفه أو تنزّل صلاحيته")

    if "username" in data and data["username"]:
        new_username = data["username"].strip()
        duplicate = db.query(models.User).filter(models.User.username == new_username, models.User.id != user_id).first()
        if duplicate:
            raise HTTPException(status_code=400, detail="اسم المستخدم ده مستخدم بالفعل")
        if new_username != user.username:
            # الاسم جزء من التوكن، فتغييره بيقفل الجلسات المفتوحة للمستخدم ده بدل ما تفضل معلّقة
            db.query(models.LoginLog).filter(
                models.LoginLog.user_id == user.id, models.LoginLog.logout_time.is_(None)
            ).update({"logout_time": now_cairo(), "closed_by_id": admin.id}, synchronize_session=False)
        user.username = new_username

    if "full_name" in data:
        user.full_name = data["full_name"]

    if "role" in data and data["role"]:
        user.role = data["role"]

    if "is_active" in data and data["is_active"] is not None:
        user.is_active = data["is_active"]
        if data["is_active"] is False:
            # لو الأدمن وقف الحساب، اقفل أي جلسات مفتوحة ليه فورًا (مش بس تمنع دخول جديد)
            db.query(models.LoginLog).filter(
                models.LoginLog.user_id == user.id, models.LoginLog.logout_time.is_(None)
            ).update({"logout_time": now_cairo(), "closed_by_id": admin.id})

    if "password" in data and data["password"]:
        user.password_hash = auth.get_password_hash(data["password"])
        # تغيير الباسورد بيقفل كل الجلسات المفتوحة للمستخدم ده (ما عدا جلسة الأدمن الحالية لو بيغيّر باسورده هو)
        others = db.query(models.LoginLog).filter(models.LoginLog.user_id == user.id, models.LoginLog.logout_time.is_(None))
        if user.id == admin.id:
            others = others.filter(models.LoginLog.id != current_session.id)
        others.update({"logout_time": now_cairo(), "closed_by_id": admin.id}, synchronize_session=False)

    db.commit()
    db.refresh(user)
    return user


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: models.User = Depends(auth.require_admin),
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="متقدرش تحذف حسابك انت وانت مسجل دخول بيه")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="المستخدم مش موجود")
    if user.role == "admin" and user.is_active and _active_admin_count(db, exclude_user_id=user.id) == 0:
        raise HTTPException(status_code=400, detail="دي آخر أدمن شغّال في النظام - مينفعش تتحذف")

    # فصل المستخدم عن كل الفواتير/السجلات المرتبطة بيه بدل ما نمنع الحذف -
    # اسمه فضل محفوظ كنسخة (username snapshot) في كل واحدة منها فهيفضل يظهر في العرض
    # بس هيتوضح جنبه إن الحساب اتمسح
    db.query(models.StockInInvoice).filter(models.StockInInvoice.received_by_id == user_id).update({"received_by_id": None})
    db.query(models.StockOutInvoice).filter(models.StockOutInvoice.delivered_by_id == user_id).update({"delivered_by_id": None})
    db.query(models.QuantityAdjustment).filter(models.QuantityAdjustment.changed_by_id == user_id).update({"changed_by_id": None})
    db.query(models.LoginLog).filter(models.LoginLog.user_id == user_id).update({"user_id": None, "logout_time": now_cairo()})

    db.delete(user)
    db.commit()
    return {"message": "تم حذف المستخدم"}


@router.get("/login-logs")
def login_logs(
    page: int = 1,
    page_size: int = 15,
    user: Optional[str] = None,
    ip: Optional[str] = None,
    status: Optional[str] = None,  # active / closed / (فاضي = الكل)
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: models.User = Depends(auth.require_admin),
):
    """
    سجل جلسات الدخول (للأدمن) - بـ pagination وفلاتر على السيرفر، فيفضل سريع مهما كبر السجل
    (قبل كده كان بيرجّع كل السجلات مرة واحدة).
    """
    page = max(1, page)
    page_size = 15 if page_size <= 0 else min(page_size, 100)

    q = db.query(models.LoginLog)
    if user and user.strip():
        # بنبحث في الاسم وقت الدخول (النسخة المحفوظة) وفي الاسم الحالي للحساب (لو اتغيّر بعدين)
        like = f"%{_escape_like(user.strip())}%"
        q = q.outerjoin(models.User, models.User.id == models.LoginLog.user_id).filter(
            or_(models.LoginLog.username.like(like, escape="\\"), models.User.username.like(like, escape="\\"))
        )
    if ip and ip.strip():
        q = q.filter(models.LoginLog.ip_address.like(f"%{_escape_like(ip.strip())}%", escape="\\"))
    if status == "active":
        q = q.filter(models.LoginLog.logout_time.is_(None))
    elif status == "closed":
        q = q.filter(models.LoginLog.logout_time.isnot(None))
    elif status not in (None, ""):
        raise HTTPException(status_code=400, detail="قيمة الحالة لازم تكون active أو closed")
    try:
        if date_from:
            q = q.filter(models.LoginLog.login_time >= datetime.fromisoformat(date_from))
        if date_to:
            end = datetime.fromisoformat(date_to)
            q = q.filter(models.LoginLog.login_time < (end + timedelta(days=1) if len(date_to) <= 10 else end))
    except ValueError:
        raise HTTPException(status_code=400, detail="صيغة التاريخ غير صحيحة")

    total = q.count()
    rows = (
        q.options(joinedload(models.LoginLog.user), joinedload(models.LoginLog.closed_by))
        .order_by(models.LoginLog.login_time.desc(), models.LoginLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [
            {
                "id": l.id,
                "username": format_user_display(l.user, l.username),
                "login_time": l.login_time,
                "logout_time": l.logout_time,
                "ip_address": l.ip_address,
                "is_active": l.logout_time is None,
                "closed_by": l.closed_by.username if l.closed_by else None,
            }
            for l in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, math.ceil(total / page_size)) if total else 1,
    }


@router.post("/login-logs/{session_id}/revoke")
def revoke_session(
    session_id: int,
    db: Session = Depends(get_db),
    admin: models.User = Depends(auth.require_admin),
):
    """إنهاء جلسة معينة بالقوة (من الأدمن) - أي طلب جاي بنفس التوكن ده هيترفض على طول."""
    session = db.query(models.LoginLog).filter(models.LoginLog.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="الجلسة مش موجودة")
    if session.logout_time is not None:
        return {"message": "الجلسة دي مقفولة بالفعل"}
    session.logout_time = now_cairo()
    session.closed_by_id = admin.id
    db.commit()
    return {"message": "تم إنهاء الجلسة"}
