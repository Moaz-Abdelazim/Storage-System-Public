# CHANGES — مخزني: الأساس + دقة المخزون + الأدوار والصلاحيات + الباركود + الأمان + Alembic + المراقبة

Branches (كل واحد مبني على اللي قبله، والأخير `step-6-migrations` فيه كل حاجة):
`step-1-foundation` → `step-2-inventory-atomic` → `step-3-roles` → `step-4-barcode` → `step-5-security` → `step-6-migrations`

## 0) نتيجة الاختبارات الحقيقية (على الـ stack المثبّت في requirements.txt)
| البيئة | النتيجة |
|---|---|
| SQLite | **544 passed, 4 skipped** (الـ 4 = migrations على MySQL) |
| MySQL 8.0.46 (REPEATABLE-READ، محلي) | **548 passed** (192 + مصفوفة الصلاحيات 356 على حدة بسبب وقت التشغيل) |
| `pip-audit` على البيئة كاملة | **No known vulnerabilities found** |
| `node --check` لكل `frontend/public/js/*.js` + إعادة بناء Tailwind | سليم، و`app.css` مطابق لآخر بناء |
قبل أي تعديل: مفيش ولا اختبار. **لم أشغّل:** الواجهة في متصفح فعلي (مفيش متصفح في البيئة)، ولا GitHub Actions، ولا Aiven، ولا أي أمر على داتا production.

## 1) أهم اكتشاف وإصلاح — الرصيد السالب على MySQL
اتأكدت بتجربة حقيقية على MySQL 8: 8 طلبات صرف متزامنة من رصيد 10 (5 لكل طلب) ← **8 من 8 نجحوا والرصيد -30** في 15 جولة من 15. دلوقتي (UPDATE ذرّي بشرط في `services/inventory.py` + قيود `CHECK >= 0`) نفس التجربة: **0 من 15 جولة فيها خطأ**، والنتيجة دايمًا ناجحين اثنين بس. اختبارات التزامن في `tests/test_concurrency.py` (صرف، وارد/صادر مختلط، نقل متعاكس، فواتير متعددة الأصناف بترتيب معكوس، جرد بالتوازي) بتشتغل على SQLite وMySQL.

## 2) اللي اتعمل
**الأساس (step-1):** `app/cli.py` (`init-db` تطوير فقط، `create-admin` بباسورد تفاعلي أو `--password-stdin`، بدون باسورد في env)، تنضيف `main.py` من الكود الميت وفتح socket وقت الـ import، توحيد التبعيات (`requirements.txt` + `requirements-dev.txt`)، `.gitignore` و`.env.example`.
**دقة المخزون (step-2):** `app/money.py` (ROUND_HALF_UP)، `app/services/inventory.py` (`add/remove/transfer/set_location_quantity/replace_locations`)، أنواع `DECIMAL(14,3)/(14,2)`، `CHECK quantity >= 0`، retry على deadlock، `python -m app.cli check-stock`، وتحويل `stock/products/locations/excel` للـ service.
**الأدوار والصلاحيات (step-3):** كتالوج 36 صلاحية بـ `requires` (`app/permissions.py`)، جداول `roles/role_permissions` و`users.role_id`، `routers/roles.py` (أدمن فقط)، صلاحية صريحة على **كل** endpoint (جدول `ENDPOINT_PERMS` المرجعي + اختبار مصفوفة بيتأكد من الـ dependencies الفعلية وبيجرّب مستخدم بالصلاحية وبدونها)، تقنيع القيم المالية (`finance.view`) في الوارد/السجل/الطباعة/الحركات/التصدير/الداشبورد، صلاحيات شرطية (كمية ابتدائية، السعر، الخصم/الضريبة، الاستيراد)، حماية آخر أدمن (بقفل صفوف)، منع تصعيد الصلاحيات من مستخدم عنده `users.manage`، إيقاف الدور = إغلاق الجلسات فورًا. الواجهة: قوائم/صفحات/أزرار حسب الصلاحية، صفحة `roles.html` جديدة، وصفحة المستخدمين بأدوار ديناميكية.
**الباركود (step-4):** حقل `products.barcode` (unique، case-insensitive في الـ API، ممنوع يساوي SKU منتج تاني)، بحث بالباركود، `GET /api/products/by-barcode/{code}` (باركود ثم SKU)، Excel (عمود نصي، رفض الصيغة العلمية، مطابقة ID ← SKU ← باركود ← اسم)، وخانة مسح في الوارد/الصادر/النقل (قارئ الباركود = كيبورد + Enter).
**الأمان (step-5):** `APP_ENV` **fail-closed** (غير متحدد = production)، ترحيل JWT من `python-jose` لـ **PyJWT**، ترقية التبعيات (ثغرات في jose/multipart/cryptography/dotenv/starlette)، رفض `COOKIE_SAMESITE=none`، rate limit لتغيير الباسورد/الاستيراد/إنشاء المستخدمين، حماية zip-bomb في رفع Excel و`.xlsx` فقط، escape لـ `%`/`_` في البحث، أخطاء الاستيراد من غير تسريب داخلي، إصلاح حذف مستخدم عنده نقل/جلسات (كان بيكسر على FK في MySQL).
**Migrations (step-6):** Alembic (`alembic.ini`, `env.py`, `0001_baseline`) + `python -m app.cli migrate`، `naming_convention`، فهارس التواريخ في الـ models، `locations.parent_key` (unique حقيقي للإخوة)، حذف `migrations.py`، و`init-db` بيعمل stamp.
**التشغيل (step-6):** `GET /api/ready` (DB + آخر migration، 503 لو لأ)، `X-Request-ID` ولوج لكل طلب، handler للأخطاء غير المتوقعة (رسالة عامة + request_id)، `cleanup` (retention)، إعدادات الـ pool من env، بناء URL آمن لباسورد MySQL، `.github/workflows/ci.yml`، `README.md`.

## 3) الملفات
معدّل/جديد: كل ملفات الـ zip (بنفس مسارات الـ repo). **محذوف — احذفهم يدوي (الـ zip مش بيعبّر عن الحذف):** `backend/Pipfile`، `backend/req.txt`، `backend/app/migrations.py`.

## 4) خطوات التشغيل
```bash
cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt
python -m pytest -q
# MySQL (DBs فاضية!):
TEST_DATABASE_URL='mysql+pymysql://U:P@127.0.0.1:3306/test_db' TEST_MIGRATION_DATABASE_URL='mysql+pymysql://U:P@127.0.0.1:3306/mig_db' python -m pytest -q
```
**Production (أنت تنفّذها، أنا مشغّلتش حاجة):** `python -m app.cli migrate` ← `python -m app.cli create-admin --username <اسمك>`. التفاصيل في `README.md`.
الواجهة: لو غيّرت كلاسات Tailwind شغّل `cd frontend && npm ci && npm run css` (`app.css` اتبنى فعلاً مع التعديلات دي).

## 5) تغييرات في شكل الـ API (تنبّه الواجهة/أي عميل)
- `users`: `role` (نص) اتشال، بقى `role_id` + `role_name` + `is_admin`. `/auth/me` و`/auth/login` بيرجّعوا `permissions[]` و`is_admin`. `POST/PUT /auth/users` بياخدوا `role_id`. `GET /auth/users?role=` بقى `role_id=`.
- حقول مالية ممكن تبقى `null` بدون `finance.view` (`unit_price`, `total_value`, `in_total_value`, أسعار الطباعة، قيمة المخزون).
- `GET /reports/product-movements/{id}` لمنتج مش موجود: `404` بدل `200 {"error"}`.
- رسالة نقص الرصيد في النقل اتوحّدت مع الصرف. أي خصم/ضريبة على الصادر محتاج `stock_out.discount`.
- `products`: حقل `barcode` جديد. `/excel/products/import` بيقبل `.xlsx` فقط.
- الأخطاء غير المتوقعة بترجع `{detail, request_id}`.

## 6) المخاطر والافتراضات
- **تبعيات:** `starlette 1.10` و`fastapi 0.162` إصدارات جديدة جدًا؛ اتختبرت بالكامل لكن راجع الـ CI عندك. اختبار المصفوفة بقى مبني على الـ routers (مش `app.routes`) عشان شكل الـ routes اتغيّر.
- **الواجهة غير مجرّبة في متصفح.** كل ملفات JS سليمة syntax، لكن اضغط على كل صفحة بدور محدود قبل الإنتاج. الـ CSP: مثال nginx فيه واحد جاهز؛ **لم أغيّر `frontend/vercel.json`** (مفيهوش CSP) عشان ما أكسرش الـ demo من غير تجربة متصفح.
- **Vercel:** هيستخدم `requirements.txt`. في الـ demo: `/api/ready` هيطلع 503 لحد ما تعمل `migrate`.
- `localStorage` بيحمل `perms` كتلميحات للواجهة فقط؛ السيرفر هو الحكم.
- التفرّد case-insensitive للباركود في الـ API (مش في SQLite نفسها). على MySQL الـ collation بتساعد كمان.
- `login_attempts.attempted_at` UTC و`login_logs` توقيت القاهرة؛ الـ cleanup بالأيام فالفرق ساعات مش مؤثر.
- لو الباسورد الافتراضي القديم (`insecure_dev_key_change_me`) كان في `.env` عندك: في production هيتمنع، وده المطلوب.

## 7) اللي **لسه ماتعملش** من الـ spec (بشفافية)
T3 (إلغاء الفواتير `void` + `audit_log` + idempotency + حالة الوارد)، T4 (الضريبة نسبة فقط + تسمية "إذن إضافة/صرف")، T5 (`app_settings` + لوجو)، T6 (الجرد `stocktake`)، T8 (شاشة الـ setup + تحسينات الاستيراد على دفعات)، `stock_in.manage_held`، ومراجعة `node --test`. الكتالوج فيه مفاتيح `invoices.void` و`stocktake.*` جاهزة. **الـ baseline `0001` اتولّد قبل T3/T5/T6**، فأي جداول جديدة منهم = revision جديد (أو نعيد توليد الـ baseline لأن مفيش DB قائمة).
