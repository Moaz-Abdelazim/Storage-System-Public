# مخزني — الواجهة (Frontend)

موقع static: **HTML + ES Modules + Tailwind (مبني) + Alpine.js** — كل المكتبات والخطوط والأيقونات محلية (**صفر CDN**).
جاهز للـ production: مفيش inline scripts/styles (يشتغل تحت CSP صارم)، والجلسة HttpOnly cookie + هيدر CSRF.

## التشغيل والنشر
```bash
npm ci               # (أو npm install)
npm run build        # بينسخ المكتبات لـ public/vendor ويبني public/css/app.css (minified)
```
الناتج الجاهز للنشر كله جوه **`public/`** (مبني بالفعل في الـ zip ده — تقدر ترفعه مباشرة من غير npm).
- ارفع محتوى `public/` على السيرفر (Nginx) — شوف `nginx.conf.example` (HTTPS + proxy لـ `/api` + CSP + كاش).
- للتطوير: `npm run dev` (بيراقب Tailwind) وافتح `public/` بأي static server.

## الصفحات
| الصفحة | الملف | ملاحظات |
|---|---|---|
| تسجيل الدخول | `login.html` | |
| لوحة القيادة | `index.html` | طلب واحد `/api/dashboard/overview`، المالي للأدمن بس |
| المنتجات | `products.html` | ترقيم/فلترة/ترتيب من السيرفر، توزيع الكمية، تفاصيل وحركات |
| التصنيفات / الماركات | `categories.html` `brands.html` | |
| المخازن والمواقع | `locations.html` | شجرة، مكان استلام افتراضي |
| النقل بين المواقع | `transfers.html` | + قايمة الرص + السجل |
| البحث عن موقع | `location-lookup.html` | drill-down بالمستويات |
| وارد / صادر | `stock-in.html` `stock-out.html` | واجهة فاتورة بسطور |
| سجل الحركات | `movements.html` | وارد/صادر/نقل بفلاتر |
| تفاصيل فاتورة | `invoice.html?kind=in/out&id=` | قابلة للطباعة |
| الموردين / جهات الاستلام | `suppliers.html` `recipients.html` | + تفاصيل المنتجات والفواتير |
| **التقارير (أدمن)** | `reports.html` | ABC، الراكد، إعادة الطلب، الربحية، أسعار الموردين، الموظفين، التعديلات + خريطة النشاط |
| المستخدمين / سجل الدخول (أدمن) | `users.html` `login-logs.html` | إنهاء جلسات بالقوة |
| الإعدادات | `settings.html` | الثيم، تغيير كلمة المرور، استيراد/تصدير Excel |

## الهيكلة
```
public/
  *.html                صفحة لكل قسم
  js/api.js             كل طلبات الـ API (CSRF + cookie + 401 + كاش قصير)
  js/auth.js            حالة المستخدم والصلاحيات (تلميحات واجهة؛ السيرفر هو اللي بيفرض)
  js/utils.js           تنسيق أرقام/تواريخ، toast، confirm، CSV
  js/data.js            مصادر القوايم المنسدلة (تصنيفات، مواقع، منتجات...) مع كاش
  js/ui.js              <ui-select> (بحث + remote) و <ui-pager>
  js/layout.js          السايدبار والشريط العلوي (مشترك)
  js/<page>.js          منطق كل صفحة
src/input.css           مصدر الـ CSS (ألوان الهوية + components)
```
عنوان الـ API: HTTPS => نفس الـ origin (الـ proxy بيوصّل `/api`)، HTTP (تطوير) => نفس الـ host على بورت 8000، أو `window.API_BASE_OVERRIDE`.

## ملاحظات
- **الروابط الداخلية بتستخدم الـ hash** (`products.html#stock=low`) عشان الفلاتر تفضل شغالة حتى لو الـ web server عمل redirect وقصّ الـ query string (clean URLs). الـ `?query` لسه مدعوم كمان.
- **الترقيم (Pagination) من السيرفر في كل الجداول:** المنتجات، الحركات، الموردين/الجهات، التصنيفات/الماركات، المستخدمين، سجل الدخول، النقل، الرص، المخزون حسب الموقع، **وكل تقارير الأدمن**. القوايم المنسدلة (منتجات/موردين/مواقع...) بتبحث على السيرفر بحد أقصى للنتايج، وشجرة المواقع بتحمّل الفروع لما تفتحها.
- الأنيميشن بين الوضع الليلي والنهاري (`js/theme.js`) بيستخدم View Transitions API وبيرجع لـ fade عادي في المتصفحات القديمة، وبيحترم `prefers-reduced-motion`.
- Alpine.js (النسخة العادية) محتاج `'unsafe-eval'` في `script-src` (موجود في مثال Nginx). مفيش inline scripts/styles.
- إخفاء عناصر الأدمن في الواجهة تلميح بس — السيرفر بيفرض الصلاحيات على كل endpoint.
- خطوط Tajawal وأيقونات Font Awesome (solid + regular) وChart.js وAlpine كلها في `public/vendor` و`public/fonts`.
