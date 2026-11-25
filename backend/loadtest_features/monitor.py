"""مراقب الموارد أثناء الاختبار: CPU/RAM للجهاز والـ API والـ MySQL والـ Locust، واتصالات الـ DB، وأبطأ الاستعلامات (performance_schema)."""
import statistics
import threading
import time

import psutil
import pymysql


def _procs(pattern):
    """العمليات اللي cmdline بتاعها فيه pattern + أبناءها (workers بتاعة uvicorn بتتشغل بـ multiprocessing spawn ومفيش كلمة uvicorn في cmdline بتاعها)."""
    found = {}
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if pattern in " ".join(p.info["cmdline"] or []) or pattern == p.info["name"]:
                found[p.pid] = p
                for ch in p.children(recursive=True):
                    found[ch.pid] = ch
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return list(found.values())


class Monitor:
    def __init__(self, mysql_args, interval=1.0):
        self.args, self.interval, self.samples, self._stop = mysql_args, interval, [], threading.Event()
        self.conn = pymysql.connect(**mysql_args, autocommit=True)
        self.t = None

    def _status(self, name):
        with self.conn.cursor() as c:
            c.execute("SHOW GLOBAL STATUS LIKE %s", (name,))
            return int(c.fetchone()[1])

    def reset_digests(self):
        with self.conn.cursor() as c:
            c.execute("TRUNCATE TABLE performance_schema.events_statements_summary_by_digest")
            c.execute("SET GLOBAL long_query_time = 0.2")
        self.slow0 = self._status("Slow_queries")

    def _loop(self):
        psutil.cpu_percent(None)
        procs = {}
        while not self._stop.is_set():
            time.sleep(self.interval)
            row = {"t": time.time(), "cpu": psutil.cpu_percent(None), "ram_used_mb": psutil.virtual_memory().used / 2**20}
            for key, pat in (("api", "uvicorn"), ("mysql", "mysqld"), ("locust", "locust")):
                rss = cpu = 0.0
                for p in _procs(pat):
                    try:
                        procs.setdefault(p.pid, p)
                        rss += p.memory_info().rss
                        cpu += procs[p.pid].cpu_percent(None)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                row[f"{key}_rss_mb"], row[f"{key}_cpu"] = rss / 2**20, cpu
            try:
                row["db_conn"], row["db_running"] = self._status("Threads_connected"), self._status("Threads_running")
            except Exception:
                row["db_conn"] = row["db_running"] = 0
            self.samples.append(row)

    def start(self):
        self.samples, self._stop = [], threading.Event()
        self.reset_digests()
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def stop(self):
        self._stop.set()
        self.t.join(5)
        s = self.samples or [{}]

        def agg(k):
            v = [x[k] for x in s if k in x]
            return {"avg": round(statistics.mean(v), 1), "max": round(max(v), 1)} if v else {"avg": 0, "max": 0}

        digests = []
        with self.conn.cursor() as c:
            c.execute("""SELECT LEFT(DIGEST_TEXT, 220), COUNT_STAR, ROUND(SUM_TIMER_WAIT/1e9,1), ROUND(AVG_TIMER_WAIT/1e9,2), ROUND(MAX_TIMER_WAIT/1e9,1), SUM_ROWS_EXAMINED, SUM_NO_INDEX_USED
                         FROM performance_schema.events_statements_summary_by_digest WHERE SCHEMA_NAME IS NOT NULL AND DIGEST_TEXT NOT LIKE 'SHOW%%' AND DIGEST_TEXT NOT LIKE 'TRUNCATE%%'
                         AND DIGEST_TEXT NOT LIKE 'SET GLOBAL%%' AND DIGEST_TEXT NOT LIKE '%%performance_schema%%' ORDER BY SUM_TIMER_WAIT DESC LIMIT 6""")
            for r in c.fetchall():
                digests.append({"sql": r[0], "count": r[1], "total_ms": float(r[2]), "avg_ms": float(r[3]), "max_ms": float(r[4]), "rows_examined": int(r[5]), "no_index": int(r[6])})
        return {"cpu_pct": agg("cpu"), "ram_used_mb": agg("ram_used_mb"), "api_rss_mb": agg("api_rss_mb"), "api_cpu_pct": agg("api_cpu"), "mysql_rss_mb": agg("mysql_rss_mb"),
                "mysql_cpu_pct": agg("mysql_cpu"), "locust_cpu_pct": agg("locust_cpu"), "db_connections": agg("db_conn"), "db_running": agg("db_running"),
                "slow_queries": self._status("Slow_queries") - self.slow0, "top_queries": digests, "samples": len(self.samples)}
