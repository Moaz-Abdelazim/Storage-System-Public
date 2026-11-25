"""
منسّق الـ Load Test: بيشغّل السيرفر (uvicorn)، ولكل سيناريو ومستوى مستخدمين بيشغّل Locust مع مراقب الموارد ويحفظ النتايج في results/<label>.json.
بيوقف رفع الحمل لسيناريو لو نسبة الأخطاء > 5% أو p95 > 8 ثواني (حماية من استهلاك موارد غير آمن).

    python -m loadtest_features.run_load --label 5k --scenarios A,B,C,E --levels 1,5,10,25,50 --duration 40
المتغيرات (البيئة) نفس متغيرات التطبيق: DB_ENGINE=mysql MYSQL_* + MEDIA_DIR ، و APP_ENV=development.
"""
import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

from .monitor import Monitor

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
CLASSES = {"A": "ListingUser", "B": "DetailsUser", "C": "HistoryUser", "E": "MixedUser"}
NAMES = {"A": "Product Listing", "B": "Product Details", "C": "Movement History", "E": "Mixed Workload"}
PORT = 8765


def start_server(workers):
    env = dict(os.environ)
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT), "--workers", str(workers), "--log-level", "warning"],
                         cwd=HERE.parent, env=env, stdout=open(RESULTS / "server.log", "a"), stderr=subprocess.STDOUT, preexec_fn=os.setsid)
    for _ in range(60):
        try:
            if requests.get(f"http://127.0.0.1:{PORT}/api/health", timeout=1).status_code < 500:
                return p
        except Exception:
            time.sleep(1)
    raise RuntimeError("server did not start")


def stop_server(p):
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        p.wait(15)
    except Exception:
        pass


def parse_csv(prefix):
    rows = {}
    with open(f"{prefix}_stats.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows[r["Name"]] = {"requests": int(r["Request Count"]), "failures": int(r["Failure Count"]), "avg": float(r["Average Response Time"]), "min": float(r["Min Response Time"] or 0),
                               "max": float(r["Max Response Time"]), "rps": float(r["Requests/s"]), "p50": float(r["50%"]), "p90": float(r["90%"]), "p95": float(r["95%"]), "p99": float(r["99%"]),
                               "avg_size": float(r["Average Content Size"])}
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--scenarios", default="A,B,C,E")
    ap.add_argument("--levels", default="1,5,10,25,50")
    ap.add_argument("--duration", type=int, default=40)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--images", default="1")
    args = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)
    out_file = RESULTS / f"{args.label}.json"
    results = json.load(open(out_file)) if out_file.exists() else {"label": args.label, "runs": []}
    mon = Monitor(dict(host=os.environ.get("MYSQL_HOST", "127.0.0.1"), user=os.environ.get("MYSQL_USER", "root"), password=os.environ.get("MYSQL_PASSWORD", ""), database=os.environ.get("MYSQL_DB")))
    server = start_server(args.workers)
    try:
        for sc in args.scenarios.split(","):
            for level in [int(x) for x in args.levels.split(",")]:
                tag = f"{args.label}_{sc}_{level}"
                prefix, status_file = str(RESULTS / tag), str(RESULTS / f"{tag}_status.json")
                env = dict(os.environ, LOAD_OUT=status_file, LOAD_IMAGES=args.images)
                spawn = max(1, level // 4)
                mon.start()
                subprocess.run([sys.executable, "-m", "locust", "-f", str(HERE / "locustfile.py"), "--headless", "-u", str(level), "-r", str(spawn), "-t", f"{args.duration}s",
                                "--host", f"http://127.0.0.1:{PORT}", "--csv", prefix, "--only-summary", CLASSES[sc]], env=env, stdout=open(RESULTS / f"{tag}.log", "w"), stderr=subprocess.STDOUT)
                resources = mon.stop()
                try:
                    stats, st = parse_csv(prefix), json.load(open(status_file))
                except Exception as e:
                    print(f"[{tag}] نتايج ناقصة: {e}")
                    continue
                agg = stats["Aggregated"]
                err = 100 * agg["failures"] / max(1, agg["requests"])
                image_bytes = sum(v for k, v in st["bytes"].items() if "image" in k)
                run = {"scenario": sc, "name": NAMES[sc], "users": level, "duration": args.duration, "aggregate": agg, "endpoints": {k: v for k, v in stats.items() if k != "Aggregated"},
                       "error_rate_pct": round(err, 2), "status_codes": st["status"], "image_bytes": image_bytes, "total_bytes": sum(st["bytes"].values()), "resources": resources}
                results["runs"] = [r for r in results["runs"] if not (r["scenario"] == sc and r["users"] == level)] + [run]
                json.dump(results, open(out_file, "w"), ensure_ascii=False, indent=1)
                print(f"[{tag}] req={agg['requests']} rps={agg['rps']:.1f} avg={agg['avg']:.0f}ms p95={agg['p95']:.0f}ms p99={agg['p99']:.0f}ms err={err:.1f}% cpu={resources['cpu_pct']['avg']}% api_rss={resources['api_rss_mb']['max']}MB", flush=True)
                if err > 5 or agg["p95"] > 8000:
                    print(f"[{tag}] إيقاف رفع الحمل لـ {NAMES[sc]}: أخطاء/زمن مرتفع")
                    break
    finally:
        stop_server(server)


if __name__ == "__main__":
    main()
