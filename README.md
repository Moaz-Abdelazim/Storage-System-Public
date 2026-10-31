# مخزني — نظام إدارة المخازن

نظام ويب لإدارة المخزون (منتجات، مخازن ومواقع تخزين هرمية، وارد/صادر/نقل، موردين وجهات استلام، تقارير) بواجهة عربي RTL.

- **Backend:** FastAPI + SQLAlchemy + Alembic — MySQL 8 في الإنتاج، SQLite للتطوير.
- **Frontend:** HTML + Alpine.js + Tailwind (ثابتة، من غير CDN) — فولدر `frontend/public`.
- الأرقام الدقيقة: الكميات `DECIMAL(14,3)` والفلوس `DECIMAL(14,2)`، وتحديث الأرصدة كله **atomic** في `backend/app/services/inventory.py`.

## الصلاحيات
الأدمن (دور **مدير النظام**) بيعمل أدوار بصلاحيات من الكتالوج (`backend/app/permissions.py`) من صفحة «الأدوار والصلاحيات»، وبيعيّنها للمستخدمين. السيرفر هو اللي بيفرض الصلاحيات على كل endpoint؛ الواجهة بتخفي الأزرار والصفحات كتلميحات بس. القيم المالية (أسعار الشراء، إجماليات الوارد، قيمة المخزون، التقارير) محجوبة (`null`) بدون صلاحية `finance.view`.

## تشغيل للتطوير
```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # واتأكد إن APP_ENV=development
python -m app.cli init-db       # ينشئ الجداول (تطوير فقط) ويعلّم الـ schema على آخر migration
python -m app.cli create-admin --username owner      # بيسأل عن الباسورد
uvicorn app.main:app --reload
```
الواجهة: افتح `frontend/public` بأي static server (أو nginx حسب `frontend/nginx.conf.example`). لو غيّرت كلاسات Tailwind: `cd frontend && npm ci && npm run css` (الـ `public/css/app.css` متترجم ومترفوع).

## تشغيل Production
1. اضبط متغيرات البيئة (راجع `backend/.env.example`). **لازم:** `APP_ENV=production`، `SECRET_KEY` عشوائي ≥ 32 حرف (`python -c "import secrets; print(secrets.token_urlsafe(48))"`)، `ALLOWED_HOSTS` و`ALLOWED_ORIGINS`، وبيانات MySQL. (`APP_ENV` غير متحدد = production تلقائيًا.)
2. `pip install -r requirements.txt`
3. إنشاء/تحديث الجداول: `python -m app.cli migrate`  *(ممنوع `init-db` في production)*
4. أول أدمن: `python -m app.cli create-admin --username <اسمك>` *(بيسأل عن الباسورد؛ مفيش باسورد في env)*
5. تشغيل: `uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2` ورا nginx/Caddy بـ HTTPS (ظبط `TRUST_PROXY_HEADERS=1` و`TRUSTED_PROXY_COUNT` لو ورا proxy موثوق).
6. فحوص التشغيل: `GET /api/health` (التطبيق شغّال) و`GET /api/ready` (الـ DB والـ schema جاهزين، 503 لو لأ).

### صيانة
| الأمر | الغرض |
|---|---|
| `python -m app.cli check-stock` | يفحص إن إجمالي كل منتج = مجموع أماكنه (exit 1 لو فيه فرق) |
| `python -m app.cli cleanup --dry-run` | يعدّ سجلات الدخول/المحاولات القديمة (شيل `--dry-run` للمسح) — جدولها بـ cron |
| `python -m app.cli migrate` | تطبيق أي migration جديد (خدّ **backup** قبله) |

**Backup:** خد نسخة احتياطية يومية من MySQL (`mysqldump --single-transaction`) وجرّب الاسترجاع دوريًا — النظام نفسه مبيعملش backup.
**Rate limits:** حد الدخول ثابت في الـ DB؛ حدود تغيير الباسورد/الاستيراد/إنشاء المستخدمين في ذاكرة كل worker (الحد الفعلي = الحد × عدد الـ workers).

## الاختبارات
```bash
cd backend
python -m pytest -q                                   # SQLite
TEST_DATABASE_URL='mysql+pymysql://user:pass@127.0.0.1:3306/test_db' \
TEST_MIGRATION_DATABASE_URL='mysql+pymysql://user:pass@127.0.0.1:3306/mig_db' python -m pytest -q   # MySQL 8 (DBs فاضية!)
pip-audit -r requirements.txt --no-deps --disable-pip # ثغرات التبعيات
```
اختبارات التزامن (صرف متزامن، نقل متعاكس، جرد بالتوازي) بتشتغل على الاتنين وهي اللي بتثبت إن الرصيد عمره ما بيبقى سالب.

## هيكل المشروع
```
backend/app/        main.py, auth.py, permissions.py, models.py, services/ (inventory, roles), routers/, cli.py
backend/alembic/    migrations (0001_baseline)
backend/tests/      pytest (مصفوفة صلاحيات لكل endpoint + تزامن + migrations)
frontend/public/    صفحات HTML + js/ + css/app.css
```
