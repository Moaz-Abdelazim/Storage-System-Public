"""
عدد استعلامات الـ DB لكل request (لكشف N+1): بنقيس كل endpoint بأحجام صفحات مختلفة. لو العدد بيزيد مع حجم الصفحة = N+1.
    python -m loadtest_features.profile_queries         (على DB الـ load اللي اتعملها seed)
"""
import json
import time

from fastapi.testclient import TestClient
from sqlalchemy import event

from app.database import engine
from app.main import app

import random

DATA = json.load(open(__import__("pathlib").Path(__file__).parent / "data" / "ids.json", encoding="utf-8"))
state = {"n": 0, "t": 0.0}


@event.listens_for(engine, "before_cursor_execute")
def _before(conn, cur, stmt, params, ctx, many):
    ctx._t0 = time.perf_counter()


@event.listens_for(engine, "after_cursor_execute")
def _after(conn, cur, stmt, params, ctx, many):
    state["n"] += 1
    state["t"] += time.perf_counter() - ctx._t0


def measure(c, url, params=None, reps=3):
    best = None
    for _ in range(reps):
        state["n"], state["t"] = 0, 0.0
        t0 = time.perf_counter()
        r = c.get(url, params=params)
        wall = (time.perf_counter() - t0) * 1000
        assert r.status_code == 200, (url, r.status_code, r.text[:200])
        cand = (state["n"], round(state["t"] * 1000, 1), round(wall, 1), len(r.content))
        best = cand if best is None or cand[2] < best[2] else best
    return best


def main():
    out = []
    with TestClient(app, base_url="http://localhost") as c:
        c.headers["X-CSRF-Protection"] = "1"
        assert c.post("/api/auth/login", json={"username": DATA["username"], "password": DATA["password"]}).status_code == 200
        pid, heavy, img = DATA["ids"][len(DATA["ids"]) // 2], DATA["heavy_ids"][0], DATA["image_ids"][0]
        cases = [("list page_size=10", "/api/products", {"page": 1, "page_size": 10}), ("list page_size=25", "/api/products", {"page": 1, "page_size": 25}),
                 ("list page_size=100", "/api/products", {"page": 1, "page_size": 100}), ("list search=name", "/api/products", {"search": DATA["names"][0][:-1], "page_size": 25}),
                 ("list filter warehouse", "/api/products", {"warehouse_id": 1, "page_size": 25}), ("details", f"/api/products/{pid}", None),
                 ("history small page_size=15", f"/api/products/{pid}/history", {"page_size": 15}), ("history heavy page_size=15", f"/api/products/{heavy}/history", {"page_size": 15}),
                 ("history heavy page_size=100", f"/api/products/{heavy}/history", {"page_size": 100}),
                 ("history heavy filtered", f"/api/products/{heavy}/history", {"kind": "out", "warehouse_id": 1, "date_from": "2025-01-01", "page_size": 15}),
                 ("image thumb", f"/api/products/{img}/image", {"size": "thumb"}), ("image main", f"/api/products/{img}/image", {"size": "main"})]
        for name, url, params in cases:
            q, db_ms, wall, size = measure(c, url, params)
            out.append({"case": name, "queries": q, "db_ms": db_ms, "wall_ms": wall, "bytes": size})
            print(f"{name:34s} queries={q:4d} db={db_ms:8.1f}ms wall={wall:8.1f}ms size={size}")
    label = __import__("sys").argv[1] if len(__import__("sys").argv) > 1 else "run"
    json.dump(out, open(__import__("pathlib").Path(__file__).parent / "results" / f"query_profile_{label}.json", "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    (__import__("pathlib").Path(__file__).parent / "results").mkdir(exist_ok=True)
    main()
