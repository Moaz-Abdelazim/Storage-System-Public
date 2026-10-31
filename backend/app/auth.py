import logging
import os
from datetime import datetime, timedelta
from typing import Optional, Tuple

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordBearer
import jwt  # PyJWT (بديل python-jose: الأخير غير مصان وعليه ثغرات)
from jwt import PyJWTError as JWTError
from passlib.context import CryptContext
from sqlalchemy.orm import Session, joinedload, selectinload

from .database import get_db
from . import models, permissions

from .env import app_env, is_production

APP_ENV = app_env()
IS_PRODUCTION = is_production()  # fail-closed: APP_ENV غير متحدد = production (التطوير لازم يحدد APP_ENV=development صراحة)
_DEV_SECRET = "dev-only-secret-key-not-for-production-use-0123456789"
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
if not SECRET_KEY or SECRET_KEY == _DEV_SECRET:
    if IS_PRODUCTION:
        raise RuntimeError("SECRET_KEY لازم يتحدد في .env (قيمة عشوائية طويلة) لما APP_ENV=production")
    SECRET_KEY = _DEV_SECRET
    logging.getLogger("app.auth").warning("بيستخدم SECRET_KEY تجريبي - مسموح في التطوير بس")
elif IS_PRODUCTION and len(SECRET_KEY) < 32:
    raise RuntimeError("SECRET_KEY قصير - لازم 32 حرف على الأقل (مثال: python -c \"import secrets; print(secrets.token_urlsafe(48))\")")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
# auto_error=False: التوكن بيجي من الـ HttpOnly cookie (الواجهة) أو من هيدر Authorization (أدوات/سكربتات)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

# ---------- الجلسة في HttpOnly cookie ----------
# Secure: الكوكي ما بتتبعتش غير على HTTPS (افتراضيًا شغّالة في production). في التطوير على http بتتقفل.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1" if IS_PRODUCTION else "0").strip() == "1"
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "strict").strip().lower()
if COOKIE_SAMESITE not in ("strict", "lax"):  # none ممنوعة: بتفتح الباب لـ CSRF
    raise RuntimeError("COOKIE_SAMESITE لازم تكون strict أو lax")
# البادئة __Host- بتخلي المتصفح يرفض أي كوكي بنفس الاسم من subdomain أو من مسار تاني (Secure + Path=/ + من غير Domain)
COOKIE_NAME = "__Host-session" if COOKIE_SECURE else "session"
CSRF_HEADER = "x-csrf-protection"  # الواجهة بتبعته مع كل طلب تعديل (انظر main.py)


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
        httponly=True,  # الـ JavaScript ما يقدرش يقرأها (بتحمي التوكن من السرقة بـ XSS)
        secure=COOKIE_SECURE ,  # في التطوير على http بتتقفل
        samesite=COOKIE_SAMESITE,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/", httponly=True, secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    ملحوظة: التوقيت هنا (exp) لازم يفضل UTC عادي (مش توقيت القاهرة) عشان مكتبة الـ JWT بتحسب
    صلاحية التوكن بمقارنته بـ UTC داخليًا - ده مش نفس توقيت العرض للمستخدم، ده مجرد
    آلية داخلية للتوكن، فمفيش داعي يتغير.
    """
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    token = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return token if isinstance(token, str) else token.decode("ascii")


def _resolve_session(request: Request, bearer: Optional[str], db: Session) -> Tuple[models.User, models.LoginLog]:
    """
    بيتحقق من التوكن **وكمان** من إن الجلسة (login_log) لسه مفتوحة في قاعدة البيانات.
    ده اللي بيخلي إغلاق جلسة من الأدمن (أو تسجيل الخروج) يأثر فورًا على أي طلب جاي،
    حتى لو التوكن نفسه لسه صالح من ناحية تاريخ الانتهاء - مفيش داعي لـ refresh tokens
    عشان التحقق ده بيحصل على قاعدة البيانات مباشرة مع كل طلب.
    الدور وصلاحياته بيتحمّلوا من الـ DB مع كل طلب (مفيش cache بين الطلبات) فأي تغيير في الأدوار بيسري فورًا.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="بيانات الدخول غير صحيحة أو الجلسة انتهت",
        headers={"WWW-Authenticate": "Bearer"},
    )
    token = bearer or request.cookies.get(COOKIE_NAME)
    if not token:
        raise credentials_exception
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        session_id = payload.get("sid")
        if username is None or session_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = (
        db.query(models.User)
        .options(joinedload(models.User.role).selectinload(models.Role.permissions))
        .filter(models.User.username == username)
        .first()
    )
    if user is None or not user.is_active:
        raise credentials_exception

    session = db.query(models.LoginLog).filter(models.LoginLog.id == session_id).first()
    if session is None or session.logout_time is not None:
        raise credentials_exception  # الجلسة دي مقفولة (خروج عادي أو إغلاق من الأدمن)

    request.state.permissions = permissions.user_permissions(user)
    return user, session


def get_current_user_and_session_any_role(
    request: Request, bearer: Optional[str] = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> Tuple[models.User, models.LoginLog]:
    """زي get_current_user_and_session لكن من غير فحص إن الدور فعّال - لـ /me و/logout بس (عشان الواجهة تعرض رسالة وتخرّج)."""
    return _resolve_session(request, bearer, db)


def get_current_user_and_session(
    request: Request, bearer: Optional[str] = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> Tuple[models.User, models.LoginLog]:
    user, session = _resolve_session(request, bearer, db)
    if user.role is None or (not user.role.is_system and not user.role.is_active):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="الدور بتاعك موقوف، كلّم الأدمن")
    return user, session


def get_current_user(result: Tuple[models.User, models.LoginLog] = Depends(get_current_user_and_session)) -> models.User:
    user, _session = result
    return user


def get_current_user_any_role(
    result: Tuple[models.User, models.LoginLog] = Depends(get_current_user_and_session_any_role),
) -> models.User:
    return result[0]


def current_permissions(request: Request, user: models.User) -> set:
    """صلاحيات المستخدم الحالي (متحسوبة مرة واحدة في الطلب)."""
    perms = getattr(request.state, "permissions", None)
    if perms is None:
        perms = request.state.permissions = permissions.user_permissions(user)
    return perms


def _deny(keys, any_of: bool = False):
    labels = (" أو " if any_of else " و").join(permissions.label_of(k) for k in keys)
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"ملكش صلاحية: {labels}")


def require_permission(*keys: str):
    """dependency: لازم المستخدم يملك **كل** المفاتيح (all-of). بترجّع المستخدم."""
    unknown = [k for k in keys if k not in permissions.PERMISSIONS]
    if unknown:  # خطأ برمجة بيظهر وقت الـ import مش وقت التشغيل
        raise RuntimeError(f"صلاحية غير معروفة في require_permission: {unknown}")

    def dependency(request: Request, user: models.User = Depends(get_current_user)) -> models.User:
        perms = current_permissions(request, user)
        if any(k not in perms for k in keys):
            raise _deny(keys)
        return user

    dependency._perm_rule = ("all", tuple(keys))
    return dependency


def require_any_permission(*keys: str):
    """dependency: لازم المستخدم يملك **واحدة على الأقل** من المفاتيح (any-of)."""
    unknown = [k for k in keys if k not in permissions.PERMISSIONS]
    if unknown:
        raise RuntimeError(f"صلاحية غير معروفة في require_any_permission: {unknown}")

    def dependency(request: Request, user: models.User = Depends(get_current_user)) -> models.User:
        perms = current_permissions(request, user)
        if not any(k in perms for k in keys):
            raise _deny(keys, any_of=True)
        return user

    dependency._perm_rule = ("any", tuple(keys))
    return dependency


def require_admin(user: models.User = Depends(get_current_user)) -> models.User:
    """الأدمن = دور is_system."""
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="الصلاحية دي للأدمن بس")
    return user


require_admin._perm_rule = ("admin", ())
get_current_user._perm_rule = ("session", ())
get_current_user_any_role._perm_rule = ("lenient", ())
get_current_user_and_session._perm_rule = ("session", ())
get_current_user_and_session_any_role._perm_rule = ("lenient", ())
