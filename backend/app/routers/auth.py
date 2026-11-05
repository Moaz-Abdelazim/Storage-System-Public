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
from ..services import sessions as session_svc
from ..services.audit import audit
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
    session_svc.expire_stale_sessions(db)  # جلسات عدّى وقتها بتتقفل كمنتهية (مش تفضل "مفتوحة")

    # الجلسة (login_log) بتتعمل الأول عشان ناخد الـ id بتاعها ونحطه جوه التوكن نفسه (sid) -
    # ده اللي بيخلي أي إغلاق للجلسة دي (من المستخدم أو من الأدمن) يبطل التوكن فورًا
    now = now_cairo()
    log = models.LoginLog(
        user_id=user.id,
        username=user.username,
        login_time=now,
        expires_at=session_svc.expiry_for(now, auth.ACCESS_TOKEN_EXPIRE_MINUTES),  # نفس مدة التوكن
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
    session.end_reason = "logout"  # خروج يدوي فعلي (الانتهاء التلقائي end_reason=expired)
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
    session_svc.close_open_sessions(db, models.LoginLog.user_id == user.id, models.LoginLog.id != session.id, reason="system")
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
    db.flush()
    audit(db, actor, "user", user.id, "create", {"username": user.username, "role": role.name})
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
    audit_changes = {k: v for k, v in data.items() if k not in ("password", "role_id") and v is not None}
    if data.get("password"):
        audit_changes["password"] = "changed"
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
            session_svc.close_open_sessions(db, models.LoginLog.user_id == user.id, reason="revoked", closed_by_id=actor.id)
        user.username = new_username

    if "full_name" in data:
        user.full_name = data["full_name"]

    if new_role is not None:
        audit_changes["role"] = {"old": user.role.name if user.role else None, "new": new_role.name}
        user.role_id = new_role.id  # الصلاحيات بتتحمّل كل request فالتغيير بيسري فورًا من غير ما نقفل الجلسات

    if "is_active" in data and data["is_active"] is not None:
        user.is_active = data["is_active"]
        if data["is_active"] is False:
            # لو الأدمن وقف الحساب، اقفل أي جلسات مفتوحة ليه فورًا (مش بس تمنع دخول جديد)
            session_svc.close_open_sessions(db, models.LoginLog.user_id == user.id, reason="revoked", closed_by_id=actor.id)

    if "password" in data and data["password"]:
        user.password_hash = auth.get_password_hash(data["password"])
        # تغيير الباسورد بيقفل كل الجلسات المفتوحة للمستخدم ده (ما عدا الجلسة الحالية لو بيغيّر باسورده هو)
        extra = [models.LoginLog.id != current_session.id] if user.id == actor.id else []
        session_svc.close_open_sessions(db, models.LoginLog.user_id == user.id, *extra, reason="revoked", closed_by_id=actor.id)

    audit(db, actor, "user", user.id, "update", audit_changes)
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
    db.query(models.StockInInvoice).filter(models.StockInInvoice.voided_by_id == user_id).update({"voided_by_id": None})
    db.query(models.StockOutInvoice).filter(models.StockOutInvoice.voided_by_id == user_id).update({"voided_by_id": None})
    db.query(models.Stocktake).filter(models.Stocktake.created_by_id == user_id).update({"created_by_id": None})
    db.query(models.Stocktake).filter(models.Stocktake.approved_by_id == user_id).update({"approved_by_id": None})
    db.query(models.QuantityAdjustment).filter(models.QuantityAdjustment.changed_by_id == user_id).update({"changed_by_id": None})
    db.query(models.LocationTransfer).filter(models.LocationTransfer.moved_by_id == user_id).update({"moved_by_id": None})
    db.query(models.LoginLog).filter(models.LoginLog.closed_by_id == user_id).update({"closed_by_id": None})
    session_svc.close_open_sessions(db, models.LoginLog.user_id == user_id, reason="system")  # المفتوحة بس (مبنكتبش فوق وقت خروج قديم)
    db.query(models.LoginLog).filter(models.LoginLog.user_id == user_id).update({"user_id": None})

    audit(db, actor, "user", user_id, "delete", {"username": user.username})
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

    L = models.LoginLog
    now = now_cairo()
    session_svc.expire_stale_sessions(db, now)  # تحديث آمن (UPDATE شرطي) للجلسات اللي عدّى وقتها قبل العرض
    db.commit()

    q = db.query(L)
    if user and user.strip():
        # بنبحث في الاسم وقت الدخول (النسخة المحفوظة) وفي الاسم الحالي للحساب (لو اتغيّر بعدين)
        like = f"%{_escape_like(user.strip())}%"
        q = q.outerjoin(models.User, models.User.id == L.user_id).filter(
            or_(L.username.like(like, escape="\\"), models.User.username.like(like, escape="\\"))
        )
    if ip and ip.strip():
        q = q.filter(L.ip_address.like(f"%{_escape_like(ip.strip())}%", escape="\\"))
    # الحالة بتتحسب من end_reason/expires_at مش من logout_time بس
    if status == "active":
        q = q.filter(L.logout_time.is_(None), or_(L.expires_at.is_(None), L.expires_at > now))
    elif status == "expired":
        q = q.filter(or_(L.end_reason == "expired", (L.logout_time.is_(None)) & (L.expires_at <= now)))
    elif status == "logged_out":
        q = q.filter(L.logout_time.isnot(None), or_(L.end_reason == "logout", (L.end_reason.is_(None)) & (L.closed_by_id.is_(None))))
    elif status == "revoked":
        q = q.filter(L.logout_time.isnot(None), or_(L.end_reason == "revoked", (L.end_reason.is_(None)) & (L.closed_by_id.isnot(None))))
    elif status == "closed":  # أي حاجة اتقفلت يدويًا/من النظام (غير المنتهية تلقائيًا)
        q = q.filter(L.logout_time.isnot(None), or_(L.end_reason.is_(None), L.end_reason != "expired"))
    elif status not in (None, ""):
        raise HTTPException(status_code=400, detail="قيمة الحالة لازم تكون active أو expired أو logged_out أو revoked أو closed")
    try:
        if date_from:
            q = q.filter(L.login_time >= datetime.fromisoformat(date_from))
        if date_to:
            end = datetime.fromisoformat(date_to)
            q = q.filter(L.login_time < (end + timedelta(days=1) if len(date_to) <= 10 else end))
    except ValueError:
        raise HTTPException(status_code=400, detail="صيغة التاريخ غير صحيحة")

    total = q.count()
    rows = (
        q.options(joinedload(L.user), joinedload(L.closed_by))
        .order_by(L.login_time.desc(), L.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = []
    for l in rows:
        st = session_svc.session_status(l, now)
        items.append({
            "id": l.id,
            "username": format_user_display(l.user, l.username),
            "login_time": l.login_time,
            "expires_at": l.expires_at,
            "logout_time": l.logout_time,
            "ended_at": session_svc.ended_at(l, now),  # وقت الخروج/الإنهاء أو وقت انتهاء الصلاحية
            "ip_address": l.ip_address,
            "status": st,  # active / expired / logged_out / revoked / system
            "end_reason": l.end_reason,
            "is_active": st == "active",
            "closed_by": l.closed_by.username if l.closed_by else None,
        })
    return {
        "items": items,
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
    session_svc.expire_stale_sessions(db)
    db.refresh(session)
    if session.logout_time is not None:
        db.commit()
        return {"message": "الجلسة دي انتهت صلاحيتها بالفعل" if session.end_reason == "expired" else "الجلسة دي مقفولة بالفعل"}
    session.logout_time = now_cairo()
    session.end_reason = "revoked"
    session.closed_by_id = admin.id
    audit(db, admin, "session", session.id, "revoke", {"user": session.username})
    db.commit()
    return {"message": "تم إنهاء الجلسة"}
