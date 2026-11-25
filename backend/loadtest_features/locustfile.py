"""
سيناريوهات Locust لصفحة المنتجات وتفاصيل المنتج وسجل الحركة. بتتشغل بواسطة loadtest_features/run_load.py (أو يدويًا):

    locust -f loadtest_features/locustfile.py --headless -u 10 -r 5 -t 40s --host http://127.0.0.1:8000 ListingUser

الكلاسات: ListingUser (A) | DetailsUser (B) | HistoryUser (C) | MixedUser (E).  (D = استيراد Excel: loadtest_features/bench_import.py)
متغيرات: LOAD_OUT (ملف JSON للـ status codes وحجم البيانات)، LOAD_IMAGES=0 لو عايز تقفل تحميل الصور.
المتصفح الحقيقي بيكاش الصور (ETag + Cache-Control)؛ هنا بنحمّلها في كل مرة (أسوأ حالة) عشان نقيس تكلفة الـ API نفسه.
"""
import collections
import json
import os
import random
import threading
from pathlib import Path

from locust import HttpUser, between, events, task

DATA = json.load(open(Path(__file__).parent / "data" / "ids.json", encoding="utf-8"))
LOAD_IMAGES = os.getenv("LOAD_IMAGES", "1") != "0"
_lock = threading.Lock()
STATUS = collections.Counter()
BYTES = collections.Counter()
HITS = collections.Counter()


@events.request.add_listener
def _count(request_type, name, response_time, response_length, response, exception, **kw):
    code = getattr(response, "status_code", 0) if response is not None else 0
    with _lock:
        STATUS[str(code)] += 1
        BYTES[name] += int(response_length or 0)
        HITS[name] += 1


@events.quitting.add_listener
def _dump(environment, **kw):
    out = os.getenv("LOAD_OUT")
    if out:
        json.dump({"status": dict(STATUS), "bytes": dict(BYTES), "hits": dict(HITS)}, open(out, "w"))


SESSION = {}


@events.test_start.add_listener
def _login_once(environment, **kw):
    """دخول واحد في أول الاختبار والمستخدمين كلهم بيشاركوا نفس الجلسة (الكوكي).
    السبب: الدخول بيكلّف ~0.3 ثانية CPU (تجزئة الباسورد)، ولو كل مستخدم Locust دخل لوحده فـ 50 مستخدم = 15 ثانية CPU على core واحد
    جوه فترة القياس، فالقياس يبقى «عاصفة دخول» مش صفحات المنتجات. عاصفة الدخول ليها قياس منفصل (loadtest/login_burst.py)."""
    import requests

    r = requests.post(environment.host + "/api/auth/login", json={"username": DATA["username"], "password": DATA["password"]}, headers={"X-CSRF-Protection": "1"}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"login failed: {r.status_code} {r.text[:120]}")
    SESSION.update(r.cookies.get_dict())


class Base(HttpUser):
    abstract = True
    wait_time = between(0.3, 1.0)

    def on_start(self):
        self.client.headers["X-CSRF-Protection"] = "1"
        for k, v in SESSION.items():
            self.client.cookies.set(k, v)

    # ---- مساعدات ----
    def list_products(self, **params):
        params.setdefault("page", 1)
        params.setdefault("page_size", 25)
        params.setdefault("status", "active")
        params.setdefault("sort", "name")
        params.setdefault("order", "asc")
        with self.client.get("/api/products", params=params, name="GET /api/products (list)", catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"status {r.status_code}")
                return None
            r.success()
            return r.json()

    def thumbs(self, payload, limit=25):
        if not LOAD_IMAGES or not payload:
            return
        for p in payload["items"][:limit]:
            if p.get("thumb_url"):
                self.client.get(p["thumb_url"], name="GET /api/products/[id]/image (thumb)")

    def pid(self, heavy=0.0, with_image=False):
        if heavy and random.random() < heavy:
            return random.choice(DATA["heavy_ids"])
        return random.choice(DATA["image_ids"] if with_image and DATA["image_ids"] else DATA["ids"])

    def details(self):
        pid = self.pid(with_image=True)
        r = self.client.get(f"/api/products/{pid}", name="GET /api/products/[id] (details)")
        if LOAD_IMAGES and r.status_code == 200:
            url = (r.json() or {}).get("image_url")
            if url:
                self.client.get(url, name="GET /api/products/[id]/image (main)")
        return pid

    def history(self, pid=None, **params):
        pid = pid or self.pid(heavy=0.3)
        params.setdefault("page", 1)
        params.setdefault("page_size", 15)
        return self.client.get(f"/api/products/{pid}/history", params=params, name="GET /api/products/[id]/history")


class ListingUser(Base):
    """A: تحميل جدول المنتجات + البحث بالاسم/SKU/باركود + الفلاتر + Pagination + الـ thumbnails"""

    @task(4)
    def first_page(self):
        self.thumbs(self.list_products())

    @task(3)
    def search_name(self):
        self.thumbs(self.list_products(search=random.choice(DATA["names"])[:-1]), limit=10)

    @task(2)
    def search_sku(self):
        self.list_products(search=random.choice(DATA["skus"]))

    @task(2)
    def search_barcode(self):
        self.list_products(search=random.choice(DATA["barcodes"]))

    @task(3)
    def filters(self):
        f = random.choice([{"category_id": random.choice(DATA["categories"])}, {"stock": random.choice(["low", "out", "ok"])},
                           {"warehouse_id": random.choice(DATA["warehouses"])}, {"sort": "quantity", "order": "desc"}])
        self.thumbs(self.list_products(**f), limit=10)

    @task(3)
    def paginate(self):
        first = self.list_products()
        if first and first["total_pages"] > 1:
            self.thumbs(self.list_products(page=random.randint(2, min(first["total_pages"], 400))), limit=10)


class DetailsUser(Base):
    """B: فتح تفاصيل منتجات مختلفة (بيانات + توزيع المخزون + الصورة الأساسية)"""

    @task
    def open_details(self):
        self.details()


class HistoryUser(Base):
    """C: سجل الحركات: فتح، Pagination، فلاتر (نوع/مخزن/فترة)، منتجات صغيرة وتقيلة"""

    @task(4)
    def open(self):
        self.history()

    @task(3)
    def paging(self):
        pid = self.pid(heavy=0.5)
        r = self.history(pid)
        if r.status_code == 200:
            pages = r.json()["total_pages"]
            if pages > 1:
                self.history(pid, page=random.randint(2, min(pages, 60)))

    @task(3)
    def filtered(self):
        f = random.choice([{"kind": random.choice(["in", "out", "transfer", "adjustment"])}, {"warehouse_id": random.choice(DATA["warehouses"])},
                           {"date_from": "2025-06-01", "date_to": "2026-01-01"}, {"kind": "out", "warehouse_id": random.choice(DATA["warehouses"]), "date_from": "2025-01-01"},
                           {"order": "asc"}])
        self.history(self.pid(heavy=0.4), **f)


class MixedUser(Base):
    """E: مستخدم مختلط: تصفح + بحث/فلاتر + تفاصيل + سجل + صور"""
    wait_time = between(1, 3)

    @task(5)
    def browse(self):
        self.thumbs(self.list_products(page=random.randint(1, 20)), limit=25)

    @task(3)
    def search(self):
        self.thumbs(self.list_products(search=random.choice(DATA["names"])[:-1]), limit=10)

    @task(3)
    def open_details(self):
        pid = self.details()
        if random.random() < 0.7:
            self.history(pid)

    @task(2)
    def history_filters(self):
        self.history(self.pid(heavy=0.2), kind=random.choice(["in", "out", "transfer"]), page=random.randint(1, 3))
