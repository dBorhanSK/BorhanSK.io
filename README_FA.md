# Borhan_SK — Telegram Desktop Cloud (Netlify + Oracle + Supabase)

این نسخه Render و Telethon را حذف می‌کند. رابط سایت روی Netlify، کد در GitHub، اطلاعات مدیریتی و زمان‌بندی‌ها در Supabase و اجرای Telegram Desktop روی ماشین Oracle Cloud قرار می‌گیرد.

## معماری و نکات مهم

- هر حساب در پوشه‌ای مستقل در `/data/telegram/<account-id>` و نمایشگر مجازی جدا اجرا می‌شود. Telegram Desktop رسمی با گزینه `-workdir` داده‌های هر نمونه را در همان پوشه نگه می‌دارد و طبق راهنمای رسمی امکان اجرای نمونه‌های مستقل هم‌زمان را می‌دهد؛ بنابراین ورود اولیه یک‌بار انجام می‌شود و با راه‌اندازی دوباره کانتینر، پروفایل باقی می‌ماند.
- ورود با QR یا شماره مستقیماً داخل Telegram Desktop انجام می‌شود. `API_ID`، `API_HASH` و Telethon لازم نیست.
- Netlify فایل‌های سایت و API عادی را میزبانی/پراکسی می‌کند. تصویر و کنترل دسکتاپ از طریق noVNC/WebSocket مستقیماً از hostname امن Oracle ارائه می‌شود؛ WebSocket را از Netlify عبور نمی‌دهیم.
- Supabase اطلاعات مدیران، اکانت‌ها، کارها، رویدادها و وضعیت سرویس را نگه می‌دارد. داده‌های خود Telegram Desktop روی دیسک پایدار Oracle هستند.
- ماژول سلف و زمان‌بندی در کد نگه داشته شده؛ ارسال متن و کلیک دکمه از راه کنترل رابط کاربری تلگرام انجام می‌شود. این روش به اندازه اتصال پروتکلی قابل‌اتکا نیست و ممکن است در اثر تغییر UI یا تأخیر جست‌وجو نیاز به تنظیم داشته باشد.
- Docker سرویس‌ها را پس از Crash راه‌اندازی می‌کند و یک heartbeat هر ۵ دقیقه در Supabase ثبت می‌شود. این heartbeat سیاست idle reclaim اوراکل را دور نمی‌زند و Uptime صددرصد را تضمین نمی‌کند.

## ۱) بکاپ و Supabase

1. از مخزن GitHub و پروژه Supabase فعلی بکاپ بگیر.
2. در Supabase → SQL Editor فایل `supabase_schema.sql` را کامل اجرا کن.
3. اسکریپت جدول‌های اعضا، حساب‌ها، کارها و رویدادها را حفظ می‌کند. فقط نشست‌های قدیمی Telethon/Telegram-Web را پاک می‌کند؛ هر حساب باید یک‌بار در Telegram Desktop وارد شود.
4. `service_role` فقط روی Oracle در فایل `.env` قرار می‌گیرد. آن را داخل فرانت‌اند، GitHub یا متغیر عمومی Netlify قرار نده.

## ۲) ساخت ماشین Oracle Cloud

1. در Oracle Cloud یک VM از نوع **VM.Standard.A1.Flex (Ampere ARM64)** در Home Region بساز. برای شروع 2 OCPU و 12 GB RAM انتخاب کن، اگر سهمیه رایگان حسابت اجازه می‌دهد.
2. Ubuntu 24.04 ARM64 را انتخاب کن.
3. یک Public IPv4 اختصاص بده.
4. در VCN / Security List یا Network Security Group فقط این ورودی‌ها را باز کن:
   - TCP 22 برای SSH (ترجیحاً فقط IP خودت)
   - TCP 80 برای صدور/تمدید گواهی HTTPS
   - TCP 443 برای سایت دسکتاپ و API
   - پورت‌های 8000، 6080 و 5901 به بعد را **عمومی باز نکن**.
5. اگر Oracle خطای Out of host capacity داد، Availability Domain دیگری را امتحان کن یا بعداً دوباره تلاش کن.

## ۳) نصب Docker روی Oracle

با SSH به VM وصل شو و اجرا کن:

```bash
sudo apt update
sudo apt install -y git curl ca-certificates docker.io docker-compose-v2 openssl
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

از SSH خارج شو و دوباره وارد شو. سپس مخزن GitHub خودت را دریافت کن:

```bash
git clone https://github.com/USERNAME/REPOSITORY.git
cd REPOSITORY
```

`USERNAME/REPOSITORY` را با آدرس واقعی مخزن خودت عوض کن.

## ۴) تنظیم Supabase و رازها روی Oracle

```bash
cp .env.example .env
nano .env
```

این موارد را کامل کن:

- `API_HOST`: آی‌پی عمومی را با خط تیره جایگزین نقطه‌ها کن و `.nip.io` اضافه کن. مثال ساختاری: `123-45-67-89.nip.io`؛ از IP واقعی خودت استفاده کن.
- `SUPABASE_URL`: آدرس پروژه Supabase.
- `SUPABASE_SERVICE_ROLE_KEY`: کلید service_role همان پروژه.
- `DASHBOARD_PASSWORD`: رمز طولانی و یکتای مالک (حداقل ۱۶ نویسه؛ سرور با رمز نمونه اجرا نمی‌شود).
- `SECRET_KEY` و `WEBHOOK_TOKEN`: دو مقدار تصادفی مجزا بساز:

```bash
openssl rand -hex 32
openssl rand -hex 32
```

- `PUBLIC_ORIGINS` را `https://borhansk.netlify.app` بگذار.
- `MAX_DESKTOP_ACCOUNTS=2` یعنی به‌صورت پیش‌فرض دو دسکتاپ هم‌زمان روشن‌اند (برای روان‌ماندن روی ۲ OCPU). حساب‌های بیشتر می‌توانند پروفایل ذخیره‌شده داشته باشند، اما برای اجرا باید یک دسکتاپ دیگر بسته شود یا منابع VM افزایش یابد.

ذخیره کن و خارج شو. `.env` در `.gitignore` است؛ آن را Commit نکن.

## ۵) اجرای سرویس Oracle

از ریشه مخزن اجرا کن:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f backend
docker compose logs -f caddy
```

Caddy به‌صورت خودکار گواهی HTTPS می‌گیرد؛ DNS آی‌پی و پورت‌های 80/443 باید درست باشند. بعد از آماده‌شدن این آدرس را باز کن:

```text
https://YOUR-API-HOST/health
```

باید JSON با `ok: true` و `engine: ready` ببینی. اگر `desktop-not-ready` بود، اجرا کن:

```bash
docker compose exec backend sh -lc 'which telegram-desktop Xvfb xdpyinfo openbox x11vnc xdotool wmctrl xclip tesseract websockify'
```

## ۶) اتصال Netlify به GitHub

1. در Netlify گزینه **Add new site → Import an existing project** را بزن و مخزن GitHub را انتخاب کن.
2. Build command خالی و Publish directory برابر `frontend` باشد؛ `netlify.toml` همین مقدار را تنظیم کرده است.
3. در `netlify.toml` هر دو مقصد `YOUR-ORACLE-IP.nip.io` را با مقدار دقیق `API_HOST` از `.env` عوض کن. سپس Commit و Push کن.
4. نام سایت را `borhansk` انتخاب کن (اگر آزاد باشد) تا آدرس `https://borhansk.netlify.app` ساخته شود.
5. تست کن: `https://borhansk.netlify.app/api/ping` باید `{"ok":true,...}` برگرداند.

## ۷) ورود به Telegram Desktop

1. وارد `https://borhansk.netlify.app` شو و با نام کاربری `owner` و رمز مالک وارد شو.
2. از بخش اکانت‌ها یک اکانت جدید بساز.
3. Telegram Desktop در خود سایت باز می‌شود. برای QR، در تلگرام گوشی به Settings → Devices → Link Desktop Device برو و QR را اسکن کن؛ یا با شماره، کد و رمز دومرحله‌ای وارد شو.
4. پس از دیدن فهرست گفتگوها، دکمه «ورود انجام شد» را بزن تا اکانت برای سلف و زمان‌بندی فعال شود.
5. برای هر اکانت بعدی همین مراحل را تکرار کن؛ هر اکانت پوشه و نشست جدا دارد.
6. «بستن دسکتاپ» برنامه را می‌بندد اما پروفایل را نگه می‌دارد. حذف اکانت، پوشه نشست همان حساب را هم پاک می‌کند.

## ۸) بروزرسانی کد

پس از Commit/Push به GitHub، روی Oracle اجرا کن:

```bash
cd REPOSITORY
git pull
docker compose up -d --build
```

Netlify خودکار Deploy می‌کند. پوشه `data/` یا volume `./data:/data` را حذف نکن؛ نشست Telegram Desktop آنجاست.

## ۹) محدودیت‌ها و امنیت

- Telegram Desktop رسمی در سایت نمایش داده می‌شود؛ قابلیت‌های GUI آن در دسترس‌اند، اما تماس صوتی/تصویری، میکروفون/وب‌کم مانند اجرای محلی تضمین‌شده نیست. برای ارسال فایل از کامپیوتر، از دکمه بارگذاری فایل استفاده کن؛ فایل روی Oracle کپی می‌شود و مسیرش را در پنجره انتخاب فایل تلگرام وارد می‌کنی. فایل‌های دریافت‌شده در پوشه Downloads از دکمه «فایل‌های دریافت‌شده» قابل دانلودند.
- ارسال خودکار متن با کنترل ماوس/کیبورد و انتخاب چت انجام می‌شود. مقصد را به شکل `@username` یا عنوان دقیق چت (مثلاً `Saved Messages`) بگذار و ابتدا فقط به خودت پیام آزمایشی بفرست. برای ارسال انبوه یا حساس بدون نظارت از آن استفاده نکن.
- کلیک دکمه‌ها با OCR فارسی/انگلیسی انجام می‌شود و best-effort است؛ تغییر UI یا متن تکراری ممکن است باعث کلیک اشتباه شود.
- تعداد دسکتاپ‌های هم‌زمان به RAM و CPU بستگی دارد. Telegram Desktop چند نمونه GUI مصرف منابع بیشتری از Bot API دارد.
- چون اتصال فقط از طریق رابط رسمی Desktop است، نام/شمارهٔ واقعی حساب به‌صورت خودکار از پروتکل استخراج نمی‌شود؛ برای شناسایی بهتر، هنگام افزودن حساب یک برچسب مثل `اصلی — @username` وارد کن. خود اطلاعات کامل حساب را داخل Telegram Desktop می‌بینی.
- نشست‌ها روی دیسک Oracle ذخیره می‌شوند. SSH را محدود کن، رازهای Supabase را محرمانه نگه دار و از `data/telegram` پشتیبان رمزگذاری‌شده بگیر.
- Oracle Always Free تضمین Uptime صددرصد ندارد. Oracle می‌تواند VM رایگان را در صورت پایین‌بودن هم‌زمان معیارهای CPU، شبکه و (برای A1) حافظه در بازهٔ ۷ روز بازیابی کند. heartbeat پایگاه داده و Restart خودکار برای خطاهای معمول مفیدند، اما سیاست Oracle را دور نمی‌زنند.
- پروژهٔ Supabase روی Free نیز اگر در یک هفته فعالیت دیتابیسی کافی نداشته باشد ممکن است Pause شود. این پروژه هر ۵ دقیقه heartbeat دیتابیس می‌نویسد تا فعالیت و پایش منظم داشته باشد، اما Supabase هم تضمین نمی‌کند هر نوع فعالیتی مانع Pause شود؛ وضعیت پروژه و ایمیل‌های هشدار Supabase را بررسی کن.
- تست‌های ایستا و تست‌های محلی هرجا وابستگی‌های لازم موجود باشد اجرا می‌شوند؛ ورود واقعی به حساب تلگرام، Oracle و Supabase فقط پس از نصب قابل آزمایش است. هیچ‌کس نمی‌تواند بدون آزمایش واقعی «بدون باگ» بودن را تضمین کند.

## عیب‌یابی سریع

```bash
docker compose ps
docker compose logs --tail=150 backend
docker compose logs --tail=100 caddy
```

- iframe خالی: پورت 443، گواهی HTTPS، مقدار `API_HOST`، مقاصد `netlify.toml` و CSP را بررسی کن.
- `engine: desktop-not-ready`: بررسی کن تمام فرمان‌های بالا نصب باشند.
- خطای دیتابیس: URL، service_role و اجرای SQL را بررسی کن.
- نشست حفظ نشده: وجود volume `./data:/data` و پوشه `data/telegram` را بررسی کن.
