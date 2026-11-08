import { api, esc, fmt, fail, icons } from "../core.js";

export default {
  id: "logs", title: "گزارش فعالیت", eyebrow: "ACTIVITY LOG", icon: "list", roles: ["owner", "admin"],
  mount(el) {
    el.innerHTML = `<div class="card"><div class="row" style="margin-bottom:12px"><div class="grow"><h3>رویدادهای اخیر</h3>
      <p class="sub" style="margin:0">ورودها، اتصال‌ها، اجرای کارها و هر اقدام مالک روی حساب‌های ادمین</p></div>
      <button class="btn sm" id="r">${icons.refresh} تازه‌سازی</button></div><div id="list" class="col" style="gap:0"></div></div>`;
    const mark = { error: ["bad", "!"], warning: ["warn", "⚠"], success: ["ok", "✓"], info: ["info", "·"] };
    async function load() {
      try {
        const rows = await api("/api/events");
        el.querySelector("#list").innerHTML = rows.length ? rows.map((x) => {
          const [c, s] = mark[x.level] || mark.info;
          return `<div class="row" style="padding:10px 4px;border-bottom:1px solid #12285a"><span class="pill ${c}" style="width:28px;justify-content:center">${s}</span>
            <div class="grow">${esc(x.message)}</div><span class="small muted">${fmt(x.created_at)}</span></div>`;
        }).join("") : `<div class="empty">هنوز رویدادی ثبت نشده است.</div>`;
      } catch (e) { fail(e); }
    }
    el.querySelector("#r").onclick = load; load();
    const t = setInterval(load, 12000);
    return () => clearInterval(t);
  },
};
