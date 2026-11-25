# Load Test لميزات صفحة تفاصيل المنتج + سجل الحركة + صور المنتجات

ده مكمّل لـ `loadtest/` (اللي في جذر الريبو: سيناريو الاستخدام العام + عاصفة الدخول). هنا بنقيس تحديدًا:
جدول المنتجات مع الـ Thumbnails، تفاصيل المنتج، سجل الحركات (منتجات صغيرة وتقيلة)، حمل مختلط، واستيراد Excel بالصور.

| ملف | وظيفته |
|---|---|
| `seed.py` | بيولّد 1k/5k/10k منتج + حركات متفاوتة + صور بأحجام وصيغ مختلفة (بتتعالج بخط الإنتاج). **بيرفض يشتغل على DB اسمها مفيهاش `load`** |
| `locustfile.py` | سيناريوهات A (القائمة) / B (التفاصيل) / C (السجل) / E (مختلط) |
| `monitor.py` | CPU/RAM (الجهاز، الـ API بكل workers، MySQL، Locust) + اتصالات DB + أبطأ الاستعلامات من `performance_schema` |
| `run_load.py` | بيشغّل uvicorn ولكل سيناريو/مستوى بيشغّل Locust + المراقب، وبيوقف رفع الحمل لو أخطاء > 5% أو p95 > 8s |
| `profile_queries.py` | عدد استعلامات الـ DB لكل طلب بأحجام صفحات مختلفة (كشف N+1) |
| `bench_import.py` | السيناريو D: زمن وذاكرة استيراد Excel (بدون صور / بصور عايمة / داخل الخلية / ملف كبير) |
| `make_report.py` | بيبني `LOAD_TEST_REPORT.md` والرسوم من `results/*.json` |
| `run_all.sh` | كل ده متسلسل |

## التشغيل
```bash
# DB منفصلة اسمها فيها load (مهم!)
mysql -uroot -p -e "create database jardy_load character set utf8mb4"
export APP_ENV=development DB_ENGINE=mysql MYSQL_HOST=127.0.0.1 MYSQL_USER=root MYSQL_PASSWORD=... MYSQL_DB=jardy_load
export ALLOWED_HOSTS=127.0.0.1,localhost,testserver MEDIA_DIR=/tmp/jardy_load_media
pip install locust psutil matplotlib          # للاختبار بس (مش في requirements)
cd backend && python -m app.cli migrate
SPECS="5000:5k:1,5,10,25,50:30" bash loadtest_features/run_all.sh     # حجم واحد
python -m loadtest_features.make_report --out ../LOAD_TEST_REPORT.md --charts ../loadtest_reports/charts
```
⚠️ مستخدم MySQL لازم يقدر يقرا `performance_schema` ويعمل `SET GLOBAL long_query_time` (الـ root محليًا تمام). **متشغّلش ده على قاعدة الإنتاج.** مولّد الحمل الأفضل يكون على جهاز تاني عن السيرفر.
