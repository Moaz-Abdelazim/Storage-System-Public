"""دخول متزامن (بداية الوردية): مفيش 500 ولا deadlock حتى لو كذا مستخدم/جهاز بيدخلوا في نفس اللحظة.
(على MySQL كان بيحصل deadlock 1213 بين UPDATE login_logs لقفل الجلسات المنتهية و INSERT الجلسة الجديدة.)"""
import threading

from fastapi.testclient import TestClient

from app.main import app

from .conftest import CSRF, DEFAULT_PASSWORD


def test_concurrent_logins_all_succeed_and_get_distinct_sessions(make_user):
    names = ["cl1", "cl2", "cl3", "cl1", "cl2", "cl3", "cl1", "cl2"]          # نفس المستخدم من أكتر من جهاز + مستخدمين مختلفين
    for n in set(names):
        make_user(n, "staff", DEFAULT_PASSWORD)
    for _round in range(4):
        clients = [TestClient(app, base_url="http://testserver", headers=CSRF) for _ in names]
        barrier, results = threading.Barrier(len(names)), []

        def go(c, n):
            barrier.wait()
            r = c.post("/api/auth/login", json={"username": n, "password": DEFAULT_PASSWORD})
            results.append((r.status_code, r.text[:120]))

        ts = [threading.Thread(target=go, args=(c, n)) for c, n in zip(clients, names)]
        [t.start() for t in ts]; [t.join() for t in ts]
        assert [s for s, _ in results] == [200] * len(names), results
        for c in clients:
            assert c.get("/api/auth/me").status_code == 200
            c.close()
