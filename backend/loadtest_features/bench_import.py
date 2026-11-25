"""
قياس استيراد Excel (السيناريو D): الزمن وذروة ذاكرة الـ API، بدون صور وبصور.
    python -m loadtest_features.bench_import --rows 500
بيشغّل السيرفر لنفسه ويعمل المنتجات من الصفر لكل حالة (SKU مميز لكل تشغيل).
"""
import argparse
import io
import json
import os
import random
import threading
import time
import zipfile
from pathlib import Path

import psutil
import requests
import xlsxwriter

from .monitor import _procs
from .run_load import PORT, RESULTS, start_server, stop_server

DATA = json.load(open(Path(__file__).parent / "data" / "ids.json", encoding="utf-8"))
BASE = f"http://127.0.0.1:{PORT}"


def noise_png(size):
    from PIL import Image
    buf = io.BytesIO()
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(buf, "PNG")
    return buf.getvalue()


def build(prefix, rows, image_px=None, in_cell=False):
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet()
    ws.write_row(0, 0, ["SKU", "الاسم", "السعر", "الكمية", "الصورة"])
    pool = [noise_png((image_px, image_px)) for _ in range(min(rows, 20))] if image_px else []
    for i in range(rows):
        ws.write_row(i + 1, 0, [f"{prefix}-{i:05d}", f"منتج استيراد {prefix} {i}", round(random.uniform(5, 500), 2), 0])
        if pool:
            if in_cell:
                ws.embed_image(i + 1, 4, "i.png", {"image_data": io.BytesIO(random.choice(pool))})
            else:
                ws.set_row(i + 1, max(40, image_px * 0.75 + 8))   # الصف لازم يشيل الصورة (صورة أكبر من الصف بتتبلّغ مش بتتربط)
                ws.insert_image(i + 1, 4, "i.png", {"image_data": io.BytesIO(random.choice(pool)), "object_position": 1})
    wb.close()
    return _like_excel(buf.getvalue())


def _like_excel(data):
    """XlsxWriter بيكتب بعض أسماء الأجزاء بـ / في الأول (/xl/richData/...) وExcel الحقيقي لأ (والرفع بيرفض المسارات المطلقة): بنشيلها."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            zout.writestr(item.filename.lstrip("/"), zin.read(item.filename))
    return out.getvalue()


def run_case(session, label, content, expect_rows):
    peak, stop = [0.0], threading.Event()

    def sample():
        while not stop.is_set():
            rss = 0
            for p in _procs("uvicorn"):
                try:
                    rss += p.memory_info().rss
                except Exception:
                    pass
            peak[0] = max(peak[0], rss / 2**20)
            time.sleep(0.05)

    base_rss = 0
    for p in _procs("uvicorn"):
        try:
            base_rss += p.memory_info().rss / 2**20
        except Exception:
            pass
    th = threading.Thread(target=sample, daemon=True)
    th.start()
    t0 = time.time()
    r = session.post(f"{BASE}/api/excel/products/import", files={"file": ("p.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}, timeout=600)
    dt = time.time() - t0
    stop.set()
    th.join(1)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    res = {"case": label, "file_mb": round(len(content) / 2**20, 2), "rows": expect_rows, "status": r.status_code, "seconds": round(dt, 2), "rows_per_sec": round(expect_rows / dt, 1) if dt else 0,
           "api_rss_before_mb": round(base_rss, 1), "api_rss_peak_mb": round(peak[0], 1), "created": body.get("created"), "images_saved": body.get("images_saved"),
           "image_errors": body.get("image_errors_total"), "errors": body.get("errors_total")}
    print(res, flush=True)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=500)
    ap.add_argument("--workers", type=int, default=1)
    args = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)
    server = start_server(args.workers)
    out = []
    try:
        s = requests.Session()
        s.headers["X-CSRF-Protection"] = "1"
        assert s.post(f"{BASE}/api/auth/login", json={"username": DATA["username"], "password": DATA["password"]}).status_code == 200
        stamp = int(time.time()) % 100000
        n = args.rows
        out.append(run_case(s, f"{n} صف بدون صور", build(f"A{stamp}", n), n))
        out.append(run_case(s, f"{n} صف مع صور عايمة (60×60)", build(f"B{stamp}", n, 60), n))
        out.append(run_case(s, f"{n} صف مع صور داخل الخلية (60×60)", build(f"C{stamp}", n, 60, in_cell=True), n))
        out.append(run_case(s, f"150 صف بصور 300×300 (ضوضاء، ملف كبير)", build(f"D{stamp}", 150, 300), 150))
        out.append(run_case(s, f"{n * 4} صف بدون صور", build(f"E{stamp}", n * 4), n * 4))
    finally:
        stop_server(server)
        json.dump(out, open(RESULTS / "import_bench.json", "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
