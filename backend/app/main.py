import logging
import os
import time
import socket
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from urllib.parse import urlparse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from dotenv import load_dotenv


def _dev_local_ip() -> str:
    """
    عنوان الجهاز على الشبكة المحلية. بيتستخدم في بيئة التطوير بس (عشان تفتح النظام من موبايل/جهاز تاني
    على نفس الشبكة). في production مبيتنادّاش خالص ومفيش أي socket بيتفتح. الـ UDP connect مبيبعتش
    أي بيانات - بيسأل نظام التشغيل بس عن الـ interface اللي هيطلع منه.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


from . import auth
from .services.inventory import InsufficientStock, InconsistentStock
from . import observability
from .database import engine
from .routers import auth as auth_router, roles as roles_router
from .routers import products, suppliers, recipients, stock, reports, excel_io, categories, brands, locations, location_nav, dashboard, analytics, executive_report, company, invoice_print

load_dotenv()

IS_PRODUCTION = auth.IS_PRODUCTION
# في الـ production صفحات التوثيق التفاعلية (/docs و /redoc و /openapi.json) بتتقفل عشان ما تكشفش شكل الـ API
observability.setup_logging()

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


@app.exception_handler(InsufficientStock)
async def insufficient_stock_handler(request: Request, exc: InsufficientStock):
    return JSONResponse(status_code=400, content={"detail": exc.message})


@app.exception_handler(InconsistentStock)
async def inconsistent_stock_handler(request: Request, exc: InconsistentStock):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


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


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    """request id لكل طلب (header + log) عشان أي خطأ يتتبع، وسطر log واحد لكل طلب (من غير query string)."""
    started = time.perf_counter()
    rid = observability.new_request_id(request.headers.get("x-request-id"))
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    if request.url.path.startswith("/api/"):
        observability.log_request(rid, request.method, request.url.path, response.status_code, started)
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """أي خطأ غير متوقع: بنسجّل التفاصيل في اللوج، والعميل ياخد رسالة عامة + request_id بس (من غير تسريب داخلي)."""
    rid = getattr(request.state, "request_id", None) or observability.new_request_id()
    logging.getLogger("app.error").exception("unhandled error rid=%s %s %s", rid, request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "حصل خطأ داخلي. لو المشكلة فضلت، ابعت رقم الطلب للدعم.", "request_id": rid},
                        headers={"X-Request-ID": rid})


# (الـ middleware ده بيتسجل قبل CORS عشان CORS يفضل الطبقة الخارجية وردود 429/413 توصل للمتصفح بشكل سليم)
# ---------- أمان: مفيش "allow_origins=*" هنا، بس الدومينات/العناوين اللي محددة ----------
# ممكن تتحكم فيها من .env بمتغير ALLOWED_ORIGINS (مفصولة بفاصلة)، ولو مش موجود بيستخدم الافتراضي ده
_env_origins = os.getenv("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip() for o in _env_origins.split(",") if o.strip()]
_DEV_LAN_IP = None if IS_PRODUCTION else _dev_local_ip()  # في الـ production بس اللي متحدد في ALLOWED_ORIGINS
if _DEV_LAN_IP:
    ALLOWED_ORIGINS.append(f"http://{_DEV_LAN_IP}")
    ALLOWED_ORIGINS.append(f"https://{_DEV_LAN_IP}")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Protection"],
)

app.add_middleware(GZipMiddleware, minimum_size=1024)

# ---------- أمان إضافي: رفض أي طلب بعنوان Host مش معروف (يمنع هجمات Host header) ----------
_env_hosts = os.getenv("ALLOWED_HOSTS", "")
ALLOWED_HOSTS = [h.strip() for h in _env_hosts.split(",") if h.strip()]
if _DEV_LAN_IP:
    ALLOWED_HOSTS.append(_DEV_LAN_IP)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

app.include_router(auth_router.router)
app.include_router(roles_router.router)
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

@app.get("/api/health")
def health_check():
    """liveness: التطبيق شغّال (من غير ما نلمس الـ DB)."""
    return {"status": "ok", "message": "السيرفر شغال"}


@app.get("/api/ready")
def readiness_check():
    """readiness: الـ DB بترد والـ schema على آخر migration. 503 لو لأ (الـ load balancer يشيل الـ instance)."""
    result = observability.readiness(engine)
    if not result["ready"]:
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": result["reason"]})
    return {"status": "ready"}
