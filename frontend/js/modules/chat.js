import { api, esc, fail, toast, $ } from "../core.js";

export default {
  id: "chat", title: "Telegram Desktop", eyebrow: "OFFICIAL DESKTOP SESSION", icon: "chat", roles: ["owner", "admin"],
  mount(el, ctx) {
    let accounts = [], accountId = ctx.params?.account || "", alive = true, timer = null;
    el.innerHTML = `
      <div class="card desktop-card">
        <div class="row wrap desktop-toolbar">
          <label class="grow" style="min-width:230px">اکانت تلگرام<select id="desktop-account"></select></label>
          <span class="pill info" id="desktop-status"><i></i>در حال بررسی</span>
          <button class="btn sm" id="desktop-reopen">بازکردن پنجره</button>
          <button class="btn sm danger" id="desktop-close">بستن دسکتاپ</button>
          <button class="btn sm primary" id="desktop-ready">ورود انجام شد</button>
          <button class="btn sm ghost" id="desktop-full">تمام‌صفحه</button>
        </div>
        <p class="hint" id="desktop-help">Telegram Desktop رسمی در همین صفحه اجرا می‌شود. بار اول با QR یا شماره وارد شو؛ اطلاعات نشست روی دیسک پایدار Oracle ذخیره می‌شود. لینک نمایش دسکتاپ موقت است و برای امنیت هر بار تازه ساخته می‌شود.</p>
        <div class="desktop-frame" id="desktop-frame">
          <div class="desktop-empty" id="desktop-empty">در حال راه‌اندازی محیط دسکتاپ…</div>
          <iframe id="desktop-view" title="Telegram Desktop" referrerpolicy="no-referrer" allow="clipboard-read; clipboard-write; fullscreen" allowfullscreen></iframe>
        </div>
        <div class="row wrap desktop-bottom">
          <span class="hint">برای کار با تلگرام از ماوس و صفحه‌کلید عادی استفاده کن. در اولین ورود، پس از کامل‌شدن ورود دکمهٔ «ورود انجام شد» را بزن.</span>
          <label class="btn sm ghost" for="desktop-upload-input">بارگذاری فایل از کامپیوتر</label>
          <input id="desktop-upload-input" type="file" multiple hidden>
          <button class="btn sm ghost" id="desktop-downloads">فایل‌های دریافت‌شده</button>
          <button class="btn sm ghost" id="desktop-new-token">تازه‌سازی اتصال تصویر</button>
        </div>
        <div class="card desktop-upload-list" id="desktop-upload-list" hidden></div>
        <div class="card desktop-upload-list" id="desktop-download-list" hidden></div>
      </div>`;

    const sel = $("#desktop-account", el), frame = $("#desktop-frame", el);
    const iframe = $("#desktop-view", el), empty = $("#desktop-empty", el), status = $("#desktop-status", el);
    const uploadInput = $("#desktop-upload-input", el), uploadList = $("#desktop-upload-list", el);
    const downloadList = $("#desktop-download-list", el);

    async function loadAccounts() {
      try {
        accounts = (await api("/api/accounts")).filter((a) => a.can_use);
        if (!accounts.length) {
          empty.textContent = "اکانت قابل دسترسی وجود ندارد. ابتدا از بخش اکانت‌ها یک محیط دسکتاپ بساز.";
          iframe.hidden = true; sel.innerHTML = ""; return;
        }
        sel.innerHTML = accounts.map((a) => `<option value="${esc(a.id)}">${esc(a.label)}${!a.mine ? " — ادمین: " + esc(a.holder) : ""}</option>`).join("");
        if (!accounts.some((a) => a.id === accountId)) accountId = accounts[0].id;
        sel.value = accountId;
        await openDesktop();
      } catch (e) { fail(e); empty.textContent = e.message || "خطا در دریافت اکانت‌ها"; }
    }

    function stopTimer() { if (timer) clearInterval(timer); timer = null; }

    async function issueViewToken() {
      const data = await api(`/api/accounts/${accountId}/desktop/view-token`, { method: "POST" });
      if (!data.url || !/^https:\/\//i.test(data.url)) throw new Error("آدرس امن دسکتاپ معتبر نیست.");
      iframe.src = data.url;
      iframe.hidden = false;
      empty.hidden = true;
    }

    async function refreshStatus() {
      if (!alive || !accountId) return;
      try {
        const s = await api(`/api/accounts/${accountId}/desktop/status`);
        status.className = "pill " + (s.ready ? "ok" : s.running ? "info" : "warn");
        status.innerHTML = `<i></i>${s.ready ? "ورود تأیید شده" : s.running ? "تلگرام باز است؛ ورود کن" : "در حال اجرا نیست"}`;
        $("#desktop-ready", el).hidden = !!s.ready;
      } catch (e) {
        status.className = "pill warn"; status.innerHTML = "<i></i>ارتباط با سرور قطع است";
      }
    }

    async function openDesktop() {
      if (!accountId) return;
      stopTimer(); iframe.hidden = true; iframe.removeAttribute("src");
      empty.hidden = false; empty.textContent = "در حال اجرای Telegram Desktop روی Oracle…";
      try {
        await api(`/api/accounts/${accountId}/desktop/open`, { method: "POST" });
        await issueViewToken(); await refreshStatus();
        timer = setInterval(refreshStatus, 5000);
      } catch (e) { empty.hidden = false; empty.textContent = e.message || "راه‌اندازی دسکتاپ ناموفق بود."; fail(e); }
    }

    sel.onchange = async () => { accountId = sel.value; await openDesktop(); };
    $("#desktop-reopen", el).onclick = openDesktop;
    $("#desktop-close", el).onclick = async () => {
      if (!accountId) return;
      try {
        await api(`/api/accounts/${accountId}/desktop/close`, { method: "POST" });
        iframe.removeAttribute("src"); iframe.hidden = true;
        empty.hidden = false; empty.textContent = "دسکتاپ بسته شد؛ نشست روی Oracle حفظ شده است. برای بازکردن دوباره دکمهٔ اجرای دسکتاپ را بزن.";
        await refreshStatus();
      } catch (e) { fail(e); }
    };
    $("#desktop-downloads", el).onclick = async () => {
      try {
        const items = await api(`/api/accounts/${accountId}/desktop/downloads`);
        downloadList.hidden = false;
        downloadList.innerHTML = items.length ? `<b>فایل‌های موجود در تلگرام دسکتاپ</b>` + items.map((f) => {
          const hrefName = f.name.split("/").map(encodeURIComponent).join("/");
          return `<div class="desktop-upload-item"><b>${esc(f.name)}</b><span>${(f.size / 1048576).toFixed(2)} MB</span><a class="btn sm ghost" href="/api/accounts/${accountId}/desktop/downloads/${hrefName}">دریافت</a></div>`;
        }).join("") : "هنوز فایلی در پوشه Downloads تلگرام ذخیره نشده است.";
      } catch (e) { fail(e); }
    };
    $("#desktop-new-token", el).onclick = async () => {
      try { await issueViewToken(); toast("اتصال تصویر تازه شد", "good"); }
      catch (e) { fail(e); }
    };
    $("#desktop-ready", el).onclick = async () => {
      try {
        await api(`/api/accounts/${accountId}/desktop/ready`, { method: "POST" });
        toast("ورود ثبت شد؛ قابلیت‌های سلف و اتوماسیون فعال شدند.", "good");
        await refreshStatus();
      } catch (e) { fail(e); }
    };
    $("#desktop-full", el).onclick = () => {
      if (frame.requestFullscreen) frame.requestFullscreen().catch(() => {});
    };
    uploadInput.onchange = async () => {
      const files = [...(uploadInput.files || [])];
      if (!files.length || !accountId) return;
      uploadList.hidden = false;
      uploadList.innerHTML = "<b>در حال بارگذاری فایل‌ها…</b>";
      const results = [];
      for (const file of files) {
        if (file.size > 25 * 1024 * 1024) { results.push({ error: `${file.name}: حداکثر ۲۵ مگابایت` }); continue; }
        try {
          const form = new FormData(); form.append("file", file, file.name);
          const item = await api(`/api/accounts/${accountId}/desktop/upload`, { method: "POST", form });
          results.push(item);
        } catch (e) { results.push({ error: `${file.name}: ${e.message}` }); }
      }
      uploadList.innerHTML = results.map((r) => r.path
        ? `<div class="desktop-upload-item"><b>${esc(r.name)}</b><code>${esc(r.path)}</code><button class="btn sm ghost" data-copy-path="${esc(r.path)}">کپی مسیر</button></div>`
        : `<div class="err small">${esc(r.error)}</div>`).join("");
      toast("بارگذاری فایل تمام شد؛ برای پیوست، مسیر را در پنجره انتخاب فایل تلگرام وارد کن.", "good");
      uploadInput.value = "";
    };
    uploadList.onclick = async (e) => {
      const b = e.target.closest("[data-copy-path]"); if (!b) return;
      try { await navigator.clipboard.writeText(b.dataset.copyPath); toast("مسیر فایل کپی شد", "good"); }
      catch { toast("کپی خودکار ممکن نیست؛ مسیر فایل را انتخاب و کپی کن.", "bad"); }
    };
    iframe.addEventListener("load", () => { if (alive) empty.hidden = true; });
    iframe.addEventListener("error", () => { if (alive) { empty.hidden = false; empty.textContent = "بارگذاری دسکتاپ ناموفق بود؛ اتصال Oracle و گواهی HTTPS را بررسی کن."; } });

    loadAccounts();
    return () => { alive = false; stopTimer(); iframe.removeAttribute("src"); };
  },
};
