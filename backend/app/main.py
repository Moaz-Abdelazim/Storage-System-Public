import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from urllib.parse import urlparse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from dotenv import load_dotenv

import socket

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


from . import models, auth
from .database import engine, SessionLocal, Base
from .migrations import ensure_schema
from .routers import auth as auth_router
from .routers import products, suppliers, recipients, stock, reports, excel_io, categories, brands, locations, location_nav, dashboard, analytics, executive_report, company, invoice_print

load_dotenv()

# ينشئ الجداول لو مش موجودة + يضيف الأعمدة الجديدة لو الجداول موجودة من إصدار أقدم
# Base.metadata.create_all(bind=engine)
# ensure_schema(engine)

IS_PRODUCTION = auth.IS_PRODUCTION
# في الـ production صفحات التوثيق التفاعلية (/docs و /redoc و /openapi.json) بتتقفل عشان ما تكشفش شكل الـ API
app = FastAPI(
    title="نظام إدارة المخازن - Inventory System",
    version="1.1.0",
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)

# رسالة 422 مقروءة كنص واحد (الواجهة بتعرض detail مباشرة)، ومن غير ما نرجّع القيمة المدخلة نفسها
# (قيم زي NaN كانت بتوقع الـ serializer وتطلع 500)
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    messages = []
    for err in exc.errors():
        msg = str(err.get("msg", "قيمة غير صالحة"))
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        field = ".".join(str(p) for p in err.get("loc", []) if p not in ("body", "query", "path"))
        messages.append(f"{field}: {msg}" if field and not any("\u0600" <= ch <= "\u06ff" for ch in msg) else msg)
    return JSONResponse(status_code=422, content={"detail": " | ".join(messages[:5]) or "بيانات غير صالحة"})


# ---------- أمان: هيدرز + حد لحجم الطلب ----------
# (حماية تسجيل الدخول من التخمين جوه routers/auth.py عن طريق app/security.py)
MAX_BODY_BYTES = 12 * 1024 * 1024  # أكبر طلب مسموح (رفع Excel بحد أقصى 10 ميجا + هامش)


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _origin_allowed(origin: str, request: Request) -> bool:
    if origin in ALLOWED_ORIGINS:
        return True
    # نفس الأوريجن (الواجهة والـ API ورا نفس الـ proxy): الـ Origin بيطابق الـ Host اللي الطلب جه عليه
    return urlparse(origin).netloc == request.headers.get("host", "")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"detail": "حجم الطلب كبير جدًا"})

    # ---- حماية CSRF: الجلسة في cookie بتتبعت أوتوماتيك، فأي طلب تعديل (من غير Authorization header) لازم:
    #  1) لو فيه Origin يكون من الأوريجنات المسموحة (أو نفس الأوريجن)
    #  2) يبقى فيه هيدر مخصوص (X-CSRF-Protection). المتصفح ما يقدر يضيف هيدر مخصوص في طلب cross-site
    #     من غير preflight، والـ CORS عندنا بيرفض أي أوريجن غريب. بالإضافة لـ SameSite=Strict على الكوكي نفسها.
    if request.method in UNSAFE_METHODS and request.url.path.startswith("/api/") and "authorization" not in request.headers:
        origin = request.headers.get("origin")
        if origin is not None and not _origin_allowed(origin, request):
            return JSONResponse(status_code=403, content={"detail": "طلب مرفوض (أوريجن غير مسموح)"})
        if request.headers.get(auth.CSRF_HEADER) != "1":
            return JSONResponse(status_code=403, content={"detail": "طلب مرفوض (حماية CSRF)"})

    response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if request.url.path.startswith("/api/"):
        # setdefault: لو الـ endpoint حدد Cache-Control بنفسه (زي لوجو الشركة بـ ETag) ما نكتبش فوقه
        response.headers.setdefault("Cache-Control", "no-store")
    if request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


# (الـ middleware ده بيتسجل قبل CORS عشان CORS يفضل الطبقة الخارجية وردود 429/413 توصل للمتصفح بشكل سليم)
LOCAL_IP = get_local_ip()
# ---------- أمان: مفيش "allow_origins=*" هنا، بس الدومينات/العناوين اللي محددة ----------
# ممكن تتحكم فيها من .env بمتغير ALLOWED_ORIGINS (مفصولة بفاصلة)، ولو مش موجود بيستخدم الافتراضي ده
# DEFAULT_ORIGINS = [
#     "http://100.79.19.41",
#     "https://100.79.19.41",

#     "http://192.168.1.12",
#     "https://192.168.1.12",

#     "http://localhost:80",
#     "https://localhost:80",
#     "http://localhost",
#     "https://localhost",
#     "http://127.0.0.1:80",
#     "https://127.0.0.1:80",

#     "http://stock-management-system.taild502ee.ts.net",
#     "https://stock-management-system.taild502ee.ts.net",
# ]

_env_origins = os.getenv("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip() for o in _env_origins.split(",") if o.strip()]
if not IS_PRODUCTION:  # في الـ production بس اللي متحدد في ALLOWED_ORIGINS
    ALLOWED_ORIGINS.append(f"http://{LOCAL_IP}")
    ALLOWED_ORIGINS.append(f"https://{LOCAL_IP}")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Protection"],
)

app.add_middleware(GZipMiddleware, minimum_size=1024)

# ---------- أمان إضافي: رفض أي طلب بعنوان Host مش معروف (يمنع هجمات Host header) ----------
# DEFAULT_HOSTS = ["100.79.19.41", "localhost", "127.0.0.1", "stock-management-system.taild502ee.ts.net"]
_env_hosts = os.getenv("ALLOWED_HOSTS", "")
ALLOWED_HOSTS = [h.strip() for h in _env_hosts.split(",") if h.strip()]
if not IS_PRODUCTION:
    ALLOWED_HOSTS.append(f"{LOCAL_IP}")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

app.include_router(auth_router.router)
app.include_router(categories.router)
app.include_router(brands.router)
app.include_router(products.router)
app.include_router(suppliers.router)
app.include_router(recipients.router)
app.include_router(stock.router)
app.include_router(reports.router)
app.include_router(excel_io.router)
app.include_router(location_nav.router)
app.include_router(locations.router)
app.include_router(dashboard.router)
app.include_router(analytics.router)
app.include_router(executive_report.router)
app.include_router(company.router)
app.include_router(invoice_print.router)

# @app.on_event("startup")
# def create_first_admin():
#     db = SessionLocal()
#     try:
#         admin_username = (os.getenv("FIRST_ADMIN_USERNAME") or "").strip()
#         admin_password = os.getenv("FIRST_ADMIN_PASSWORD") or ""
#         if db.query(models.User).count() > 0 and not admin_username:
#             return  # فيه مستخدمين بالفعل ومفيش أدمن أولي مطلوب
#         if not admin_username or not admin_password:
#             print("⚠ FIRST_ADMIN_USERNAME / FIRST_ADMIN_PASSWORD مش متحددين في .env - مفيش حساب أدمن اتعمل")
#             return
#         if IS_PRODUCTION and len(admin_password) < 10:
#             raise RuntimeError("FIRST_ADMIN_PASSWORD لازم يكون 10 حروف على الأقل في الـ production")
#         existing = db.query(models.User).filter(models.User.username == admin_username).first()
#         if not existing:
#             admin = models.User(
#                 username=admin_username,
#                 full_name="مدير النظام",
#                 password_hash=auth.get_password_hash(admin_password),
#                 role="admin",
#             )
#             db.add(admin)
#             db.commit()
#             print(f"✔ تم إنشاء حساب الأدمن الأول: {admin_username}")
#     finally:
#         db.close()


@app.get("/api/health")
def health_check():
    return {"status": "ok", "message": "السيرفر شغال"}
print("Restart")