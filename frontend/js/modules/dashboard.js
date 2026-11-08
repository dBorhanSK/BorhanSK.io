import { api, esc, fail, toast, state, icons, ago } from "../core.js";

export default {
  id: "dashboard", title: "نمای کلی", eyebrow: "OVERVIEW", icon: "home", roles: ["owner", "admin"],
  mount(el, ctx) {
    const owner = state.user.role === "owner";
    el.innerHTML = `
      <div class="hero">
        <img src="/assets/borhan-mark.svg" alt="">
        <h2>سلام ${esc(state.user.name)} 👋</h2>
        <p>اکانت‌های تلگرامت را وصل کن، مستقیم از همین سایت چت کن و کارهای سلف را زمان‌بندی کن. همه‌چیز رمزگذاری‌شده و فقط برای خودت است.</p>
        <div class="row wrap"><button class="btn primary" data-go="accounts">${icons.plus} اتصال اکانت</button>
          <button class="btn" data-go="chat">${icons.chat} چت‌های تلگرام</button>
          <button class="btn" data-go="self">${icons.bolt} سلف</button></div>
      </div>
      <div id="warn"></div>
      <div class="grid g4" id="stats"></div>
      <div class="grid g2" style="margin-top:16px">
        <div class="card"><h3>اکانت‌های من</h3><p class="sub">وضعیت اتصال حساب‌ها</p><div id="acc-list" class="col"></div></div>
        <div class="card" id="side-card"></div>
      </div>`;
    el.querySelectorAll("[data-go]").forEach((b) => (b.onclick = () => ctx.go(b.dataset.go)));

    const side = el.querySelector("#side-card");
    if (owner) {
      side.innerHTML = `<h3>دسترسی مالک</h3><p class="sub">چه چیزهایی را می‌بینی</p>
        <div class="col small muted" style="gap:8px">
          <div>✔ فهرست همه‌ی اکانت‌ها، نام، @username، وضعیت اتصال و ادمین صاحب هر اکانت</div>
          <div>✔ ساخت/غیرفعال/حذف ادمین‌ها و ریست رمزشان</div>
          <div>✔ حذف اکانت یا کار زمان‌بندی‌شدهٔ هر ادمین</div>
          <div>🔐 چت‌ها، پیام‌ها و کارهای سلف یک ادمین فقط وقتی باز می‌شود که خودش «دسترسی مالک» را روشن کرده باشد (هر لحظه قابل لغو) و هر اقدام در گزارش خودش ثبت می‌شود.</div>
        </div>`;
    } else {
      side.innerHTML = `<h3>تغییر رمز عبور</h3><p class="sub">حداقل ۱۲ نویسه</p>
        <form id="pw" class="col"><label>رمز فعلی<input type="password" name="current" class="ltr" required autocomplete="current-password"></label>
        <label>رمز جدید<input type="password" name="new" class="ltr" minlength="12" required autocomplete="new-password"></label>
        <button class="btn primary">ذخیره رمز جدید</button></form>`;
      side.querySelector("#pw").onsubmit = async (e) => {
        e.preventDefault(); const f = new FormData(e.target);
        try { await api("/api/me/password", { method: "POST", body: { current: f.get("current"), new: f.get("new") } }); toast("رمز تغییر کرد", "good"); e.target.reset(); } catch (err) { fail(err); }
      };
    }

    let alive = true;
    async function load() {
      try {
        const [o, accs] = await Promise.all([api("/api/overview"), api("/api/accounts")]);
        if (!alive) return;
        const cells = [["اکانت‌ها", o.accounts], ["متصل", o.connected], ["کار فعال سلف", o.jobs_active], owner ? ["ادمین‌ها", o.admins] : ["کل کارها", o.jobs]];
        el.querySelector("#stats").innerHTML = cells.map(([k, v]) => `<div class="card stat"><span>${k}</span><b>${v ?? 0}</b></div>`).join("");
        el.querySelector("#warn").innerHTML = o.engine_ready ? "" :
          `<div class="err" style="margin-bottom:16px">موتور Telegram Desktop آماده نیست. مقدار <b>API_HOST</b> و وضعیت سرویس‌های Docker روی Oracle را بررسی کن؛ سپس سرویس را دوباره راه‌اندازی کن.</div>`;
        const list = (owner ? accs.filter((a) => a.mine) : accs);
        el.querySelector("#acc-list").innerHTML = list.length ? list.map((a) => `
          <div class="row"><div class="avatar">${esc((a.tg_name || a.label)[0] || "?")}</div>
          <div class="grow"><b>${esc(a.label)}</b><div class="small muted">${esc(a.tg_name || "—")} ${a.tg_username ? "· @" + esc(a.tg_username) : ""}</div></div>
          <span class="pill ${a.connected ? "ok" : "warn"}"><i></i>${a.connected ? "متصل" : "نیازمند ورود"}</span></div>`).join("")
          : `<div class="empty">هنوز اکانتی اضافه نکرده‌ای.</div>`;
      } catch (e) { fail(e); }
    }
    load();
    const t = setInterval(load, 10000);
    return () => { alive = false; clearInterval(t); };
  },
};
