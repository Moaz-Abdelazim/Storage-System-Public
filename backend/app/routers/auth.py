import math
from datetime import datetime, timedelta
from typing import Optional

from ..timezone_utils import now_cairo

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, update
from sqlalchemy.orm import Session, joinedload, selectinload

from .. import models, permissions, schemas, auth
from ..database import get_db
from ..display_utils import format_user_display
from ..security import client_ip, login_limiter
from ..query_helpers import contains, page_args, paged
from ..rate_limit import limit_per_user
from ..services.roles import role_name_exists  # noqa: F401

router = APIRouter(prefix="/api/auth", tags=["Auth"])

# حماية تسجيل الدخول من التخمين: انظر app/security.py (حد لكل IP + حد لكل اسم مستخدم)
# هاش وهمي بنتحقق منه لما اسم المستخدم مش موجود، عشان زمن الرد يبقى متقارب ومفيش حد يعرف
# مين موجود ومين لأ من سرعة الرد (User Enumeration by timing)
_DUMMY_HASH = auth.get_password_hash("timing-equalizer-not-a-real-password")


def _escape_like(value: str) -> str:
    """بيهرّب % و _ في بحث LIKE عشان يتعاملوا كحروف عادية."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _lock_active_admins(db: Session) -> list:
    """
    بيقفل (FOR UPDATE) صفوف الأدمنز النشطين عشان فحص "آخر أدمن" يتسلسل: أدمنين بيعطّلوا بعض في نفس اللحظة
    مش هيعدّوا الفحص الاتنين. (SQLite بتتجاهل FOR UPDATE، والكتابة فيها بتتسلسل أصلًا.)
    """
    if db.get_bind().dialect.name == "sqlite":
        # SQLite بتتجاهل FOR UPDATE: بناخد قفل الكتابة بتاعها بكتابة وهمية قبل القراءة عشان الفحص يتسلسل فعلًا
        db.execute(update(models.Role).where(models.Role.is_system.is_(True)).values(id=models.Role.id))
    return (
        db.query(models.User).join(models.Role, models.Role.id == models.User.role_id)
        .filter(models.Role.is_system.is_(True), models.User.is_active.is_(True))
        .order_by(models.User.id).populate_existing().with_for_update().all()
    )


def _other_active_admins(db: Session, exclude_user_id: int) -> int:
    return sum(1 for u in _lock_active_admins(db) if u.id != exclude_user_id)


def _user_out(u: models.User) -> dict:
    return {
        "id": u.id, "username": u.username, "full_name": u.full_name, "role_id": u.role_id, "role_name": u.role_name,
        "is_admin": u.is_admin, "is_active": bool(u.is_active), "created_at": u.created_at,
    }


def _get_assignable_role(db: Session, role_id: int, actor: models.User, actor_perms: set) -> models.Role:
    """
    الدور اللي هيتعيّن لمستخدم: لازم موجود وفعّال. ولو اللي بيعيّنه مش أدمن: مينفعش دور is_system ولا دور
    صلاحياته مش جزء من صلاحيات اللي بيعيّنه (منع تصعيد الصلاحيات).
    """
    role = db.query(models.Role).options(selectinload(models.Role.permissions)).filter(models.Role.id == role_id).first()
    if role is None:
        raise HTTPException(status_code=404, detail="الدور مش موجود")
    if not role.is_active and not role.is_system:
        raise HTTPException(status_code=400, detail="الدور ده موقوف - فعّله الأول أو اختار دور تاني")
    if not actor.is_admin:
        if role.is_system:
            raise HTTPException(status_code=403, detail="مينفعش تعيّن دور مدير النظام")
        extra = {rp.permission for rp in role.permissions} - set(actor_perms)
        if extra:
            raise HTTPException(
                status_code=403,
                detail="مينفعش تعيّن دور فيه صلاحيات أكبر من صلاحياتك: " + "، ".join(permissions.label_of(k) for k in sorted(extra)),
            )
    return role


def _forbid_touching_admin(actor: models.User, target: models.User) -> None:
    if target.is_admin and not actor.is_admin:
        raise HTTPException(status_code=403, detail="مينفعش تعدّل أو تحذف حساب مدير النظام")


@router.post("/login", response_model=schemas.Token)
def login(payload: schemas.LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = client_ip(request)
    username = payload.username.strip()
    login_limiter.check(db, ip, username.lower())

    user = db.query(models.User).options(joinedload(models.User.role).selectinload(models.Role.permissions)).filter(models.User.username == username).first()
    password_ok = auth.verify_password(payload.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not password_ok:
        login_limiter.record_failure(db, ip, username.lower())
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="اسم المستخدم أو كلمة المرور غلط")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="الحساب موقوف")
    if user.role is None or (not user.role.is_system and not user.role.is_active):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="الدور بتاعك موقوف، كلّم الأدمن")

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
    return schemas.Token(
        username=user.username, role=user.role_name, role_id=user.role_id, is_admin=user.is_admin,
        permissions=sorted(permissions.user_permissions(user)),
    )


@router.post("/logout")
def logout(
    response: Response,
    result: tuple = Depends(auth.get_current_user_and_session_any_role),
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
    _limit: None = Depends(limit_per_user("change_password", 10, 15 * 60)),
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


@router.get("/me", response_model=schemas.MeOut)
def me(request: Request, current_user: models.User = Depends(auth.get_current_user_any_role)):
    """بيانات المستخدم + صلاحياته (الواجهة بتستخدمها كتلميحات بس؛ السيرفر هو اللي بيفرض). شغّال حتى لو الدور موقوف."""
    return schemas.MeOut(
        id=current_user.id, username=current_user.username, full_name=current_user.full_name, role=current_user.role_name,
        role_id=current_user.role_id, is_admin=current_user.is_admin, is_active=bool(current_user.is_active),
        role_active=bool(current_user.is_admin or (current_user.role is not None and current_user.role.is_active)),
        permissions=sorted(auth.current_permissions(request, current_user)),
    )


@router.post("/users", response_model=schemas.UserOut)
def create_user(
    payload: schemas.UserCreate,
    request: Request,
    db: Session = Depends(get_db),
    actor: models.User = Depends(auth.require_permission("users.manage")),
    _limit: None = Depends(limit_per_user("create_user", 60, 60 * 60)),
):
    role = _get_assignable_role(db, payload.role_id, actor, auth.current_permissions(request, actor))
    if db.query(models.User).filter(models.User.username == payload.username).first():
        raise HTTPException(status_code=400, detail="اسم المستخدم موجود بالفعل")
    user = models.User(
        username=payload.username,
        full_name=payload.full_name,
        password_hash=auth.get_password_hash(payload.password),
        role_id=role.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_out(user)


@router.get("/roles-lite")
def roles_lite(db: Session = Depends(get_db), actor: models.User = Depends(auth.require_permission("users.manage"))):
    """الأدوار الفعّالة (id, name, is_system) لقايمة اختيار الدور في شاشة المستخدمين."""
    rows = db.query(models.Role).filter(models.Role.is_active.is_(True)).order_by(models.Role.is_system.desc(), models.Role.name).all()
    return [{"id": r.id, "name": r.name, "is_system": bool(r.is_system)} for r in rows]


@router.get("/usernames")
def list_usernames(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_any_permission("invoices.view", "users.manage")),
):
    """قايمة خفيفة بأسماء المستخدمين بس (لأي مستخدم مسجل دخول) - تستخدم في اقتراحات البحث
    (زي البحث في سجل الحركات عن مين سلّم/استلم)، مش لإدارة المستخدمين الكاملة."""
    users = db.query(models.User.username).filter(models.User.is_active == True).order_by(models.User.username).all()
    return [u[0] for u in users]


@router.get("/users/stats")
def users_stats(db: Session = Depends(get_db), actor: models.User = Depends(auth.require_permission("users.manage"))):
    """أعداد سريعة للكروت: الكل / المديرين / الموقوفين / توزيع الأدوار (بضربة واحدة لكل جزء)."""
    U, R = models.User, models.Role
    total = db.query(func.count(U.id)).scalar() or 0
    inactive = db.query(func.count(U.id)).filter(U.is_active.is_(False)).scalar() or 0
    admins = db.query(func.count(U.id)).join(R, R.id == U.role_id).filter(R.is_system.is_(True)).scalar() or 0
    by_role = [
        {"role_id": rid, "name": name, "count": cnt}
        for rid, name, cnt in db.query(R.id, R.name, func.count(U.id)).outerjoin(U, U.role_id == R.id)
        .group_by(R.id, R.name).order_by(R.name).all()
    ]
    return {"total": total, "admins": admins, "inactive": inactive, "active": total - inactive, "by_role": by_role}


@router.get("/users")
def list_users(
    search: Optional[str] = None,
    role_id: Optional[int] = None,
    status: Optional[str] = None,  # active / inactive
    page: Optional[int] = None,  # من غير page = القايمة كلها (للتوافق)
    page_size: int = 15,
    db: Session = Depends(get_db),
    actor: models.User = Depends(auth.require_permission("users.manage")),
):
    """المستخدمين. مع page: ترقيم وبحث وفلترة بالدور/الحالة من قاعدة البيانات."""
    U = models.User
    q = db.query(U).options(joinedload(U.role))
    if search and search.strip():
        q = q.filter(contains(U.username, search) | contains(U.full_name, search))
    if role_id:
        q = q.filter(U.role_id == role_id)
    if status == "active":
        q = q.filter(U.is_active.is_(True))
    elif status == "inactive":
        q = q.filter(U.is_active.is_(False))
    q = q.order_by(U.username)
    if page is None:
        return [_user_out(u) for u in q.all()]
    pg, size = page_args(page, page_size)
    total = q.count()
    return paged([_user_out(u) for u in q.offset((pg - 1) * size).limit(size).all()], total, pg, size)


@router.put("/users/{user_id}", response_model=schemas.UserOut)
def update_user(
    user_id: int,
    payload: schemas.UserUpdate,
    request: Request,
    db: Session = Depends(get_db),
    session_pair: tuple = Depends(auth.get_current_user_and_session),
    actor: models.User = Depends(auth.require_permission("users.manage")),
):
    _, current_session = session_pair
    perms = auth.current_permissions(request, actor)
    user = db.query(models.User).options(joinedload(models.User.role)).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="المستخدم مش موجود")
    _forbid_touching_admin(actor, user)

    data = payload.model_dump(exclude_unset=True)
    is_self = user.id == actor.id
    if is_self and not actor.is_admin and data.get("role_id") not in (None, user.role_id):
        raise HTTPException(status_code=403, detail="مينفعش تغيّر دورك بنفسك")
    if is_self and data.get("is_active") is False:
        raise HTTPException(status_code=400, detail="متقدرش توقف حسابك انت وانت مسجل دخول بيه")

    new_role = None
    if data.get("role_id") is not None and data["role_id"] != user.role_id:
        new_role = _get_assignable_role(db, data["role_id"], actor, perms)

    # حماية آخر أدمن: تعطيل/تنزيل دور أدمن نشط لازم يفضل بعده أدمن تاني نشط (الفحص بقفل صفوف الأدمنز)
    will_be_admin = new_role.is_system if new_role is not None else user.is_admin
    will_be_active = data["is_active"] if data.get("is_active") is not None else bool(user.is_active)
    if user.is_admin and user.is_active and (not will_be_admin or not will_be_active):
        if _other_active_admins(db, exclude_user_id=user.id) == 0:
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
            ).update({"logout_time": now_cairo(), "closed_by_id": actor.id}, synchronize_session=False)
        user.username = new_username

    if "full_name" in data:
        user.full_name = data["full_name"]

    if new_role is not None:
        user.role_id = new_role.id  # الصلاحيات بتتحمّل كل request فالتغيير بيسري فورًا من غير ما نقفل الجلسات

    if "is_active" in data and data["is_active"] is not None:
        user.is_active = data["is_active"]
        if data["is_active"] is False:
            # لو الأدمن وقف الحساب، اقفل أي جلسات مفتوحة ليه فورًا (مش بس تمنع دخول جديد)
            db.query(models.LoginLog).filter(
                models.LoginLog.user_id == user.id, models.LoginLog.logout_time.is_(None)
            ).update({"logout_time": now_cairo(), "closed_by_id": actor.id}, synchronize_session=False)

    if "password" in data and data["password"]:
        user.password_hash = auth.get_password_hash(data["password"])
        # تغيير الباسورد بيقفل كل الجلسات المفتوحة للمستخدم ده (ما عدا الجلسة الحالية لو بيغيّر باسورده هو)
        others = db.query(models.LoginLog).filter(models.LoginLog.user_id == user.id, models.LoginLog.logout_time.is_(None))
        if user.id == actor.id:
            others = others.filter(models.LoginLog.id != current_session.id)
        others.update({"logout_time": now_cairo(), "closed_by_id": actor.id}, synchronize_session=False)

    db.commit()
    db.refresh(user)
    return _user_out(user)


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    actor: models.User = Depends(auth.require_permission("users.manage")),
):
    if user_id == actor.id:
        raise HTTPException(status_code=400, detail="متقدرش تحذف حسابك انت وانت مسجل دخول بيه")
    user = db.query(models.User).options(joinedload(models.User.role)).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="المستخدم مش موجود")
    _forbid_touching_admin(actor, user)
    if user.is_admin and user.is_active and _other_active_admins(db, exclude_user_id=user.id) == 0:
        raise HTTPException(status_code=400, detail="دي آخر أدمن شغّال في النظام - مينفعش تتحذف")

    # فصل المستخدم عن كل الفواتير/السجلات المرتبطة بيه بدل ما نمنع الحذف -
    # اسمه فضل محفوظ كنسخة (username snapshot) في كل واحدة منها فهيفضل يظهر في العرض
    # بس هيتوضح جنبه إن الحساب اتمسح
    db.query(models.StockInInvoice).filter(models.StockInInvoice.received_by_id == user_id).update({"received_by_id": None})
    db.query(models.StockOutInvoice).filter(models.StockOutInvoice.delivered_by_id == user_id).update({"delivered_by_id": None})
    db.query(models.QuantityAdjustment).filter(models.QuantityAdjustment.changed_by_id == user_id).update({"changed_by_id": None})
    db.query(models.LocationTransfer).filter(models.LocationTransfer.moved_by_id == user_id).update({"moved_by_id": None})
    db.query(models.LoginLog).filter(models.LoginLog.closed_by_id == user_id).update({"closed_by_id": None})
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
    admin: models.User = Depends(auth.require_permission("logs.view")),
):
    """
    سجل جلسات الدخول - بـ pagination وفلاتر على السيرفر، فيفضل سريع مهما كبر السجل
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
    admin: models.User = Depends(auth.require_permission("sessions.revoke")),
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
