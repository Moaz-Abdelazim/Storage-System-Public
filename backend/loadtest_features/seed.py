"""
توليد بيانات Load Test (بيانات تجريبية بالكامل، مفيش أي بيانات عملاء).

    # DB منفصلة (اسمها لازم تحتوي load) + migrate الأول:
    export APP_ENV=development DB_ENGINE=mysql MYSQL_HOST=127.0.0.1 MYSQL_USER=root MYSQL_PASSWORD=root MYSQL_DB=jardy_load
    export MEDIA_DIR=/tmp/jardy_load_media
    python -m app.cli migrate
    python -m loadtest_features.seed --products 5000 --reset

بيعمل: 3 مخازن × 30 موقع، 20 تصنيف و20 ماركة، N منتج (SKU/باركود)، حركات متفاوتة لكل منتج (أغلبهم قليل، ~14% متوسط، وكام منتج "تقيل" بآلاف الحركات)،
وصور بأحجام وصيغ مختلفة (بتتعالج بنفس خط معالجة الإنتاج مرة واحدة وبتتنسخ لكل منتج). الأرصدة متسقة (product.quantity = مجموع الأماكن = مجموع الحركات + الرصيد الافتتاحي).
بيكتب loadtest_features/data/ids.json (ids وSKUs وباركودات وأسماء) للـ locustfile.
"""
import argparse
import io
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import insert, text

DATA_DIR = Path(__file__).resolve().parent / "data"
CHUNK = 4000


def _guard(db_name: str):
    if "load" not in (db_name or "").lower():
        sys.exit(f"رفض: اسم الـ DB ({db_name!r}) لازم يحتوي على 'load' (حماية من التشغيل على بيانات حقيقية).")


def _image_pool():
    """صور تجريبية بأحجام/صيغ مختلفة (تدرّج + ضوضاء عشان الحجم بعد الضغط يبقى واقعي) بتتعالج بخط الإنتاج."""
    from PIL import Image, ImageDraw
    from app.services.images import process_image

    rnd = random.Random(7)
    sizes = [(300, 300), (640, 480), (800, 800), (1200, 900), (1600, 1200), (2000, 1500), (3000, 2000), (4000, 3000)]
    formats = ["JPEG", "PNG", "WEBP"]
    pool, raw_bytes = [], 0
    for i, (w, h) in enumerate(sizes * 3):
        img = Image.effect_noise((w, h), 18).convert("RGB") if i % 3 == 0 else Image.new("RGB", (w, h))
        d = ImageDraw.Draw(img)
        for _ in range(40):
            x0, y0 = rnd.randint(0, w - 2), rnd.randint(0, h - 2)
            d.rectangle([x0, y0, min(w, x0 + rnd.randint(10, w // 2)), min(h, y0 + rnd.randint(10, h // 2))], fill=tuple(rnd.randint(0, 255) for _ in range(3)))
        buf = io.BytesIO()
        fmt = formats[i % 3]
        img.save(buf, fmt, **({"quality": 90} if fmt != "PNG" else {}))
        data = buf.getvalue()
        if len(data) > 7 * 1024 * 1024:  # فوق حد الرفع الفعلي: ما نختبرش بيه
            continue
        raw_bytes += len(data)
        pool.append(process_image(data))
    return pool, raw_bytes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--products", type=int, default=1000)
    ap.add_argument("--images-ratio", type=float, default=0.7)
    ap.add_argument("--heavy", type=int, default=0, help="عدد المنتجات التقيلة (افتراضي 1%% من المنتجات بحد أقصى 40)")
    ap.add_argument("--heavy-movements", type=int, default=1500)
    ap.add_argument("--reset", action="store_true", help="امسح كل الجداول المتأثرة قبل التوليد")
    ap.add_argument("--password", default="LoadTest#2026")
    args = ap.parse_args()

    from app import models
    from app.cli import create_admin
    from app.database import SessionLocal, engine
    from app.services.media_store import get_store

    _guard(engine.url.database or "")
    rnd = random.Random(42)
    db = SessionLocal()
    t0 = time.time()
    if args.reset:
        db.execute(text("SET FOREIGN_KEY_CHECKS=0")) if engine.dialect.name == "mysql" else db.execute(text("PRAGMA foreign_keys=OFF"))
        for tbl in ["stocktake_lines", "stocktakes", "quantity_adjustments", "location_transfers", "stock_in_items", "stock_in_invoices", "stock_out_items", "stock_out_invoices",
                    "product_locations", "products", "locations", "warehouses", "categories", "brands", "suppliers", "recipients"]:
            try:
                db.execute(text(f"DELETE FROM {tbl}"))
            except Exception:
                db.rollback()
        db.execute(text("SET FOREIGN_KEY_CHECKS=1")) if engine.dialect.name == "mysql" else db.execute(text("PRAGMA foreign_keys=ON"))
        db.commit()
        import shutil
        shutil.rmtree(get_store().root, ignore_errors=True)
    if db.query(models.Product).count():
        sys.exit("الـ DB فيها منتجات بالفعل (استخدم --reset)")

    admin = db.query(models.User).filter_by(username="loadadmin").first() or create_admin(db, "loadadmin", args.password, "Load Admin")
    now = datetime.now()

    db.execute(insert(models.Category), [{"id": i, "name": f"تصنيف {i}"} for i in range(1, 21)])
    db.execute(insert(models.Brand), [{"id": i, "name": f"ماركة {i}"} for i in range(1, 21)])
    db.execute(insert(models.Supplier), [{"id": 1, "name": "مورد تجريبي"}])
    db.execute(insert(models.Recipient), [{"id": 1, "name": "عميل تجريبي"}])
    leaves, loc_id = [], 0
    for w in range(1, 4):
        db.execute(insert(models.Warehouse), [{"id": w, "name": f"مخزن {w}", "code": f"W{w}"}])
        rows = []
        for f in range(1, 4):
            loc_id += 1
            floor_id = loc_id
            rows.append({"id": floor_id, "warehouse_id": w, "parent_id": None, "parent_key": 0, "name": f"دور {f}", "code": f"F{f}", "type": "floor"})
            for r in range(1, 11):
                loc_id += 1
                rows.append({"id": loc_id, "warehouse_id": w, "parent_id": floor_id, "parent_key": floor_id, "name": f"رف {r}", "code": f"R{r}", "type": "rack"})
                leaves.append(loc_id)
        db.execute(insert(models.Location), rows)
    db.commit()

    n = args.products
    heavy_n = args.heavy or min(40, max(2, n // 100))
    heavy_ids = set(rnd.sample(range(1, n + 1), heavy_n))
    pool, pool_bytes = (_image_pool() if args.images_ratio > 0 else ([], 0))
    print(f"صور مصدر: {len(pool)} (إجمالي {pool_bytes / 1024 / 1024:.1f}MB خام) - {time.time() - t0:.0f}s")

    products, plocs, in_items, out_items, transfers, adjusts = [], [], [], [], [], []
    store = get_store()
    with_images = 0
    names_sample, skus_sample, barcodes_sample = [], [], []
    for pid in range(1, n + 1):
        movements = rnd.randint(1000, args.heavy_movements) if pid in heavy_ids else (rnd.randint(30, 150) if rnd.random() < 0.14 else rnd.randint(3, 15))
        l1, l2 = rnd.sample(leaves, 2)
        qty = {l1: float(rnd.randint(0, 100)), l2: 0.0}
        opening = qty[l1]
        start = now - timedelta(days=730)
        step = timedelta(seconds=(730 * 86400) / (movements + 1))
        t = start
        for _ in range(movements):
            t += step
            op = rnd.random()
            tot = sum(qty.values())
            if op < 0.38:
                loc, q = rnd.choice((l1, l2)), float(rnd.randint(1, 50))
                qty[loc] += q
                in_items.append({"product_id": pid, "quantity": q, "unit_price": round(rnd.uniform(1, 200), 2), "location_id": loc, "_date": t})
            elif op < 0.72 and tot >= 1:
                loc = l1 if qty[l1] >= qty[l2] else l2
                q = float(rnd.randint(1, max(1, int(qty[loc]))))
                qty[loc] -= q
                out_items.append({"product_id": pid, "quantity": q, "unit_price": round(rnd.uniform(1, 250), 2), "location_id": loc, "_date": t})
            elif op < 0.9 and tot >= 1:
                src = l1 if qty[l1] >= qty[l2] else l2
                dst = l2 if src == l1 else l1
                q = float(rnd.randint(1, max(1, int(qty[src]))))
                qty[src] -= q
                qty[dst] += q
                transfers.append({"product_id": pid, "from_location_id": src, "to_location_id": dst, "quantity": q, "moved_by_id": admin.id, "moved_by_username": admin.username, "date": t})
            else:
                loc = rnd.choice((l1, l2))
                new = float(rnd.randint(0, int(qty[loc]) + 10))
                diff = new - qty[loc]
                if diff:
                    adjusts.append({"product_id": pid, "old_quantity": tot, "new_quantity": tot + diff, "difference": diff, "changed_by_id": admin.id, "changed_by_username": admin.username,
                                    "reason": "تسوية تجريبية", "date": t})
                    qty[loc] = new
        total = sum(qty.values())
        key = None
        if pool and rnd.random() < args.images_ratio:
            key = uuid.uuid4().hex
            img = rnd.choice(pool)
            d = store._dir(key)
            d.mkdir(parents=True, exist_ok=True)
            (d / "main.webp").write_bytes(img.main)
            (d / "thumb.webp").write_bytes(img.thumb)
            with_images += 1
        products.append({
            "id": pid, "name": f"منتج تجريبي {pid:05d}", "sku": f"LT-{pid:06d}", "barcode": f"{6220000000000 + pid}", "price": round(rnd.uniform(5, 900), 2),
            "quantity": total, "min_quantity": float(rnd.choice((0, 5, 10, 20))), "unit": "قطعة", "category_id": rnd.randint(1, 20), "brand_id": rnd.randint(1, 20),
            "is_active": True, "image_key": key, "created_at": start, "updated_at": now,
        })
        for loc, q in qty.items():
            if q > 0:
                plocs.append({"product_id": pid, "location_id": loc, "quantity": q, "reserved": 0.0})
        if pid % 500 == 0 or pid == n:
            print(f"  generated {pid}/{n} products ({len(in_items) + len(out_items) + len(transfers) + len(adjusts)} movements)", flush=True)
        if len(names_sample) < 300 and rnd.random() < 0.1:
            names_sample.append(products[-1]["name"]); skus_sample.append(products[-1]["sku"]); barcodes_sample.append(products[-1]["barcode"])
        _ = opening

    def bulk(model, rows):
        for i in range(0, len(rows), CHUNK):
            db.execute(insert(model), rows[i:i + CHUNK])
        db.commit()

    bulk(models.Product, products)
    bulk(models.ProductLocation, plocs)

    def invoices(items, inv_model, item_model, extra):
        items.sort(key=lambda r: r["_date"])
        inv_rows, item_rows = [], []
        for i in range(0, len(items), 3):
            group = items[i:i + 3]
            inv_id = i // 3 + 1
            inv_rows.append({"id": inv_id, "date": group[0]["_date"], "status": "completed", **extra(group[0]["_date"])})
            for it in group:
                item_rows.append({"invoice_id": inv_id, **{k: v for k, v in it.items() if k != "_date"}})
        bulk(inv_model, inv_rows)
        bulk(item_model, item_rows)

    invoices(in_items, models.StockInInvoice, models.StockInItem, lambda d: {"supplier_id": 1, "received_by_id": admin.id, "received_by_username": admin.username})
    invoices(out_items, models.StockOutInvoice, models.StockOutItem, lambda d: {"recipient_id": 1, "delivered_by_id": admin.id, "delivered_by_username": admin.username, "created_at": d, "completed_at": d})
    bulk(models.LocationTransfer, transfers)
    bulk(models.QuantityAdjustment, adjusts)

    DATA_DIR.mkdir(exist_ok=True)
    heavy_sorted = sorted(heavy_ids)
    json.dump({
        "products": n, "ids": list(range(1, n + 1)), "heavy_ids": heavy_sorted, "image_ids": [p["id"] for p in products if p["image_key"]],
        "names": names_sample, "skus": skus_sample, "barcodes": barcodes_sample, "warehouses": [1, 2, 3], "categories": list(range(1, 21)),
        "username": "loadadmin", "password": args.password,
    }, open(DATA_DIR / "ids.json", "w", encoding="utf-8"), ensure_ascii=False)
    total_mv = len(in_items) + len(out_items) + len(transfers) + len(adjusts)
    print(f"✔ {n} منتج ({with_images} بصورة) | حركات: {total_mv} (وارد {len(in_items)}، صادر {len(out_items)}، نقل {len(transfers)}، تعديل {len(adjusts)}) | منتجات تقيلة: {heavy_n} | {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
