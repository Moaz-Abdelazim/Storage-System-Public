"""
بيبني LOAD_TEST_REPORT.md + الرسوم البيانية من results/*.json.
    python -m loadtest_features.make_report --out ../LOAD_TEST_REPORT.md --charts ../loadtest_reports/charts --analysis analysis.md
الرسوم بالإنجليزي (matplotlib ما بيشكّلش الحروف العربي صح).
"""
import argparse
import json
import platform
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
RES = HERE / "results"
SC = {"A": "Product Listing", "B": "Product Details", "C": "Movement History", "E": "Mixed Workload"}
AR = {"A": "A. جدول المنتجات", "B": "B. تفاصيل المنتج", "C": "C. سجل الحركات", "E": "E. حمل مختلط"}


def sh(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "؟"


def cpu_ms(run):
    r, res = run["aggregate"]["rps"], run["resources"]
    return round(((res["api_cpu_pct"]["avg"] + res["mysql_cpu_pct"]["avg"]) / 100) * 1000 / r, 1) if r else None


def codes(run):
    return "، ".join(f"{k}: {v}" for k, v in sorted(run["status_codes"].items()))


def table(runs):
    rows = ["| مستخدمين | طلبات | RPS | متوسط | p50 | p90 | p95 | p99 | أقصى | خطأ % | HTTP | CPU جهاز | CPU-ms/طلب | RAM API | RAM MySQL | اتصالات DB (أقصى) | Slow | صور MB |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(runs, key=lambda x: x["users"]):
        a, rs = r["aggregate"], r["resources"]
        rows.append(f"| {r['users']} | {a['requests']} | {a['rps']:.1f} | {a['avg']:.0f}ms | {a['p50']:.0f}ms | {a['p90']:.0f}ms | {a['p95']:.0f}ms | {a['p99']:.0f}ms | {a['max']:.0f}ms | {r['error_rate_pct']} | {codes(r)} | "
                    f"{rs['cpu_pct']['avg']:.0f}% (أقصى {rs['cpu_pct']['max']:.0f}%) | {cpu_ms(r)} | {rs['api_rss_mb']['max']:.0f}MB | {rs['mysql_rss_mb']['max']:.0f}MB | {rs['db_connections']['max']:.0f} | {rs['slow_queries']} | {r['image_bytes'] / 2**20:.1f} |")
    return "\n".join(rows)


def endpoints_table(runs):
    top = max(runs, key=lambda x: x["users"])
    rows = [f"أبطأ الـ endpoints عند {top['users']} مستخدم:", "", "| endpoint | طلبات | متوسط | p95 | p99 | أقصى | حجم الرد المتوسط |", "|---|---|---|---|---|---|---|"]
    for name, e in sorted(top["endpoints"].items(), key=lambda kv: -kv[1]["p95"])[:6]:
        rows.append(f"| `{name}` | {e['requests']} | {e['avg']:.0f}ms | {e['p95']:.0f}ms | {e['p99']:.0f}ms | {e['max']:.0f}ms | {e['avg_size'] / 1024:.1f}KB |")
    return "\n".join(rows)


def charts(data, outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    made = []
    for label, d in data.items():
        for metric, title, ylabel in (("p95", "p95 response time", "ms"), ("rps", "Requests per second", "req/s"), ("cpu", "Machine CPU (avg)", "%")):
            fig, ax = plt.subplots(figsize=(6.4, 3.8))
            for sc, name in SC.items():
                runs = sorted([r for r in d["runs"] if r["scenario"] == sc], key=lambda x: x["users"])
                if not runs:
                    continue
                xs = [r["users"] for r in runs]
                ys = [r["aggregate"]["p95"] if metric == "p95" else r["aggregate"]["rps"] if metric == "rps" else r["resources"]["cpu_pct"]["avg"] for r in runs]
                ax.plot(xs, ys, marker="o", label=name)
            ax.set_title(f"{title} — {label} products")
            ax.set_xlabel("concurrent users"); ax.set_ylabel(ylabel); ax.grid(alpha=.3); ax.legend(fontsize=8)
            if metric == "p95":
                ax.set_yscale("log")
            fig.tight_layout()
            f = outdir / f"{label}_{metric}.png"
            fig.savefig(f, dpi=110); plt.close(fig)
            made.append((label, metric, f.name))
    return made


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--charts", required=True)
    ap.add_argument("--analysis", default="")
    a = ap.parse_args()
    data = {p.stem: json.load(open(p)) for p in sorted(RES.glob("*.json")) if p.stem in ("1k", "5k", "10k")}
    imp = json.load(open(RES / "import_bench.json")) if (RES / "import_bench.json").exists() else []
    made = charts(data, Path(a.charts))
    rel = Path(a.charts).resolve().relative_to(Path(a.out).resolve().parent)

    L = ["# تقرير اختبار الحمل (Load Test) — صفحة التفاصيل وسجل الحركة وصور المنتجات", ""]
    L += ["> ⚠️ **تحذير مهم:** النتايج دي من **جهاز واحد (1 vCPU / 4GB)** بيشغّل مولّد الحمل + الـ API + MySQL كلهم مع بعض. دي أرقام **مقارنة** (قبل/بعد، سيناريو/سيناريو) ومش دليل على قدرة النظام على تحمّل عدد معيّن من المستخدمين في الإنتاج. الرقم اللي بينتقل لأي سيرفر هو **CPU-ms/طلب** والتكلفة بالذاكرة، والأرقام النهائية لازم تتقاس على الـ VPS الفعلي.", ""]
    L += ["## 1) مواصفات جهاز الاختبار", "",
          f"- المعالج: **{sh('nproc')} vCPU** — الذاكرة: **{sh('free -m | awk \"/Mem/ {print $2}\"')} MB** — OS: {sh('. /etc/os-release; echo $PRETTY_NAME')} — Python {platform.python_version()}",
          f"- قاعدة البيانات: **MySQL {sh('mysqld --version | sed -E \"s/.*Ver ([0-9.]+).*/\\\\1/\"')}** على نفس الجهاز (`innodb_buffer_pool_size=512MB`، `max_connections=151`، باقي الإعدادات افتراضية)",
          "- الـ API: `uvicorn --workers 2` (زي توصية الـ README)، وضع `APP_ENV=development` (من غير TLS/وراء proxy؛ تأثيره على الأداء ضئيل)",
          "- مولّد الحمل: Locust (على نفس الجهاز، فبياخد جزء من الـ CPU: عمود CPU الجهاز بيشمله)", ""]
    L += ["## 2) إعدادات الاختبار", "",
          "- **البيانات (تجريبية بالكامل، مفيش بيانات عملاء):** 1,000 / 5,000 / 10,000 منتج، 3 مخازن × 33 موقع، حركات متفاوتة لكل منتج (أغلب المنتجات 3–15 حركة، ~14% بين 30–150، و1% «تقيلة» بـ 1,000–1,500 حركة)، و~70% من المنتجات عندها صورة بأحجام أصلية من 300×300 لـ 4000×3000 بصيغ JPEG/PNG/WebP (بتتعالج بخط الإنتاج: WebP + Thumbnail).",
          "- **مستويات الحمل:** تدريجي (1 ← 5 ← 10 ← 25 ← 50 مستخدم متزامن) مع وقف تلقائي لو الأخطاء > 5% أو p95 > 8 ثواني. كل مستوى 25–30 ثانية بعد الـ ramp.",
          "- **وقت التفكير:** 0.3–1 ثانية (سيناريوهات A/B/C، يعني ضغط أعلى من الاستخدام الفعلي) و1–3 ثواني (E المختلط).",
          "- **الصور:** كل مستخدم بيحمّل الـ thumbnails (حتى 25 لكل صفحة) والصورة الأساسية **من غير كاش المتصفح** (أسوأ حالة؛ المتصفح الحقيقي بيكاشها بـ ETag + `Cache-Control: immutable`).", ""]
    L += ["## 3) السيناريوهات", "", "| | السيناريو | بيعمل إيه |", "|---|---|---|",
          "| A | Product Listing | تحميل الجدول + بحث بالاسم + بحث بـ SKU وباركود + فلاتر (تصنيف/حالة مخزون/مخزن/ترتيب) + Pagination + تحميل الـ Thumbnails |",
          "| B | Product Details | بيانات المنتج + توزيع المخزون + الصورة الأساسية |",
          "| C | Movement History | فتح السجل + Pagination + فلترة (نوع/مخزن/فترة) لمنتجات صغيرة (3–15 حركة) وتقيلة (1000+) |",
          "| D | Excel Import | استيراد بدون صور / بصور / ملف كبير، زمن وذاكرة (قسم 6) |",
          "| E | Mixed | تصفح + بحث + تفاصيل + سجل + صور بوزن متنوع |", ""]
    L += ["## 4) النتائج", "", "CPU-ms/طلب = (CPU الـ API + CPU MySQL) ÷ RPS؛ HTTP = توزيع الـ status codes؛ Slow = استعلامات أبطأ من 0.2 ثانية على MySQL؛ صور MB = حجم الصور المنقولة.", ""]
    for label in ("1k", "5k", "10k"):
        if label not in data:
            continue
        L.append(f"### {label.replace('k', ',000')} منتج")
        for sc in "ABCE":
            runs = [r for r in data[label]["runs"] if r["scenario"] == sc]
            if runs:
                L += ["", f"**{AR[sc]}**", "", table(runs), "", endpoints_table(runs), ""]
        for m, t in (("p95", "p95"), ("rps", "RPS"), ("cpu", "CPU")):
            if (label, m, f"{label}_{m}.png") in made:
                L.append(f"![{t} {label}]({rel / (label + '_' + m + '.png')})")
        L.append("")
    qp = sorted(RES.glob("query_profile_*.json"))
    if qp:
        L += ["## 5) الاستعلامات لكل طلب (كشف N+1)", "", "عدد استعلامات الـ DB لكل طلب بأحجام صفحات مختلفة. لو العدد بيزيد مع حجم الصفحة يبقى N+1.", ""]
        L += ["| الحالة | " + " | ".join(p.stem.replace("query_profile_", "") for p in qp) + " |", "|---|" + "---|" * len(qp)]
        profiles = [{r["case"]: r for r in json.load(open(p))} for p in qp]
        for case in profiles[0]:
            L.append(f"| {case} | " + " | ".join(f"{pr[case]['queries']} استعلام / {pr[case]['wall_ms']:.0f}ms ({pr[case]['bytes'] / 1024:.0f}KB)" if case in pr else "—" for pr in profiles) + " |")
        L.append("")
    if imp:
        L += ["## 6) استيراد Excel (السيناريو D)", "", "| الحالة | حجم الملف | الصفوف | الزمن | صف/ث | RAM API قبل | RAM API ذروة | منتجات | صور اتربطت | مشاكل صور | أخطاء |", "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in imp:
            L.append(f"| {r['case']} | {r['file_mb']}MB | {r['rows']} | {r['seconds']}s | {r['rows_per_sec']} | {r['api_rss_before_mb']}MB | {r['api_rss_peak_mb']}MB | {r['created']} | {r['images_saved']} | {r['image_errors']} | {r['errors']} |")
        L.append("")
    if a.analysis and Path(a.analysis).exists():
        L += [Path(a.analysis).read_text(encoding="utf-8"), ""]
    Path(a.out).write_text("\n".join(L), encoding="utf-8")
    print("report written:", a.out, "charts:", len(made))


if __name__ == "__main__":
    main()
