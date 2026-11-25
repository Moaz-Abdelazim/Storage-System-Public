"""
أين بيروح الـ CPU في endpoint معيّن؟ cProfile على N طلب متتالي (TestClient، نفس العملية).
    python -m loadtest_features.profile_cpu history 150      # history | list | details | thumb | main
بيطبع أعلى الدوال بالوقت التراكمي (من كود المشروع والمكتبات).
"""
import cProfile
import io
import json
import pstats
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

DATA = json.load(open(Path(__file__).parent / "data" / "ids.json", encoding="utf-8"))


def main():
    which, n = (sys.argv[1] if len(sys.argv) > 1 else "history"), int(sys.argv[2] if len(sys.argv) > 2 else 100)
    heavy, mid, img = DATA["heavy_ids"][0], DATA["ids"][len(DATA["ids"]) // 2], DATA["image_ids"][0]
    url, params = {"history": (f"/api/products/{heavy}/history", {"page_size": 15}), "list": ("/api/products", {"page": 1, "page_size": 25}),
                   "details": (f"/api/products/{mid}", None), "thumb": (f"/api/products/{img}/image", {"size": "thumb"}), "main": (f"/api/products/{img}/image", {"size": "main"})}[which]
    with TestClient(app, base_url="http://localhost") as c:
        c.headers["X-CSRF-Protection"] = "1"
        assert c.post("/api/auth/login", json={"username": DATA["username"], "password": DATA["password"]}).status_code == 200
        for _ in range(5):
            c.get(url, params=params)
        pr = cProfile.Profile()
        pr.enable()
        for _ in range(n):
            assert c.get(url, params=params).status_code == 200
        pr.disable()
    s = io.StringIO()
    st = pstats.Stats(pr, stream=s).sort_stats("cumulative")
    st.print_stats(28)
    total = st.total_tt
    print(f"{which}: {n} requests, total {total * 1000 / n:.1f} ms/request (profiler overhead included)")
    print("\n".join(l for l in s.getvalue().splitlines() if l.strip())[:6000])


if __name__ == "__main__":
    main()
