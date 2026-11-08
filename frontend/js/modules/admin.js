import { api, esc, fail, toast, icons, fmt, confirmBox, ago, $ } from "../core.js";

export default {
  id: "admin", title: "پنل مالک", eyebrow: "OWNER CONTROL", icon: "shield", roles: ["owner"],
  mount(el, ctx) {
    let admins = [], accounts = [], alive = true;
    el.innerHTML = `<div class="grid g2" style="align-items:start">
        <div class="card"><h3>ساخت ادمین</h3><p class="sub">هر ادمین ورود مستقل دارد و فقط اکانت‌های خودش را می‌بیند</p>
          <form id="af" class="col"><label>نام کاربری<input name="username" class="ltr" required minlength="3" maxlength="32" placeholder="admin_01" autocapitalize="none"></label>
          <label>رمز عبور اولیه<input name="password" type="password" class="ltr" required minlength="12" autocomplete="new-password" placeholder="حداقل ۱۲ نویسه"></label>
          <button class="btn primary">${icons.plus} ساخت ادمین</button></form></div>
        <div class="card"><h3>راهنمای سریع مالک</h3><p class="sub">دسترسی و حریم خصوصی</p>
          <div class="col small muted" style="gap:8px"><div>• در جدول پایین همهٔ اکانت‌های تلگرام همهٔ ادمین‌ها را می‌بینی: نام، @username، شمارهٔ پوشیده، وضعیت و تعداد کار.</div>
          <div>• برای باز کردن چت یا کارهای سلف یک اکانت ادمین، خود ادمین باید «اجازهٔ دسترسی به مالک» را روشن کند؛ بعد از آن در بخش چت/سلف اکانتش در لیست تو ظاهر می‌شود.</div>
          <div>• می‌توانی هر اکانت یا هر ادمین را غیرفعال/حذف کنی (حذف، نشست تلگرام را هم می‌بندد).</div>
          <div>• وب‌هوک: <span class="mono ltr">POST /hooks/send</span> با هدر <span class="mono">X-Webhook-Token</span> و بدنهٔ JSON شامل target, text, account_id.</div></div></div>
      </div>
      <div class="card" style="margin-top:16px"><h3>ادمین‌ها</h3><p class="sub" id="asub"></p>
        <div class="tablewrap"><table><thead><tr><th>نام کاربری</th><th>وضعیت</th><th>اکانت‌ها</th><th>آخرین ورود</th><th></th></tr></thead><tbody id="ab"></tbody></table></div></div>
      <div class="card" style="margin-top:16px"><h3>همهٔ اکانت‌های تلگرام</h3><p class="sub">اکانت خودت و اکانت همهٔ ادمین‌ها</p>
        <div class="tablewrap"><table><thead><tr><th>برچسب</th><th>صاحب</th><th>اکانت تلگرام</th><th>شماره</th><th>وضعیت</th><th>دسترسی مالک</th><th></th></tr></thead><tbody id="tb"></tbody></table></div></div>`;

    async function load() {
      try {
        [admins, accounts] = await Promise.all([api("/api/admins"), api("/api/accounts")]);
        if (!alive) return;
        $("#asub", el).textContent = `${admins.length} ادمین`;
        $("#ab", el).innerHTML = admins.length ? admins.map((a) => `<tr><td><b class="ltr">${esc(a.username)}</b></td>
          <td><span class="pill ${a.active ? "ok" : "bad"}"><i></i>${a.active ? "فعال" : "غیرفعال"}</span></td>
          <td>${a.connected} متصل از ${a.accounts}</td><td class="small">${a.last_login_at ? fmt(a.last_login_at) : "—"}</td>
          <td><div class="row" style="gap:4px"><button class="btn sm" data-toggle="${a.id}" data-on="${a.active}">${a.active ? "غیرفعال‌سازی" : "فعال‌سازی"}</button>
          <button class="btn sm" data-pw="${a.id}" data-u="${esc(a.username)}">ریست رمز</button>
          <button class="btn sm danger icon" data-del="${a.id}" data-u="${esc(a.username)}">${icons.trash}</button></div></td></tr>`).join("")
          : `<tr><td colspan="5" class="empty">هنوز ادمینی نساخته‌ای.</td></tr>`;
        $("#tb", el).innerHTML = accounts.length ? accounts.map((a) => `<tr><td><b>${esc(a.label)}</b></td><td>${a.mine ? "مالک (تو)" : esc(a.holder)}</td>
          <td>${esc(a.tg_name || a.label || "—")} <span class="ltr small muted">${a.tg_username ? "@" + esc(a.tg_username) : ""}</span></td><td class="ltr small">${esc(a.tg_phone || "—")}</td>
          <td><span class="pill ${a.connected ? "ok" : "warn"}"><i></i>${a.connected ? "متصل" : "نیازمند ورود"}</span></td>
          <td>${a.mine ? "—" : (a.owner_access ? `<span class="pill warn">فعال</span>` : `<span class="pill">🔒 خاموش</span>`)}</td>
          <td><div class="row" style="gap:4px">${a.can_use && a.connected ? `<button class="btn sm" data-chat="${a.id}">${icons.chat} چت</button><button class="btn sm" data-self="${a.id}">${icons.bolt} سلف</button>` : ""}
          <button class="btn sm danger icon" data-delacc="${a.id}" data-l="${esc(a.label)}">${icons.trash}</button></div></td></tr>`).join("")
          : `<tr><td colspan="7" class="empty">اکانتی وجود ندارد.</td></tr>`;
      } catch (e) { fail(e); }
    }
    $("#af", el).onsubmit = async (e) => {
      e.preventDefault(); const f = new FormData(e.target);
      try { await api("/api/admins", { method: "POST", body: { username: f.get("username"), password: f.get("password") } }); toast("ادمین ساخته شد", "good"); e.target.reset(); load(); } catch (err) { fail(err); }
    };
    el.onclick = async (e) => {
      const b = e.target.closest("button"); if (!b) return; const d = b.dataset;
      try {
        if (d.toggle) { await api(`/api/admins/${d.toggle}`, { method: "PATCH", body: { active: d.on !== "true" } }); load(); }
        else if (d.pw) { const p = prompt(`رمز جدید برای ${d.u} (حداقل ۱۲ نویسه):`); if (p) { await api(`/api/admins/${d.pw}`, { method: "PATCH", body: { password: p } }); toast("رمز تغییر کرد", "good"); } }
        else if (d.del) { if (await confirmBox(`ادمین «${d.u}» با همهٔ اکانت‌ها و کارهایش حذف شود؟`, "حذف")) { await api(`/api/admins/${d.del}`, { method: "DELETE" }); toast("حذف شد"); load(); } }
        else if (d.delacc) { if (await confirmBox(`اکانت «${d.l}» حذف شود؟`, "حذف")) { await api(`/api/accounts/${d.delacc}`, { method: "DELETE" }); toast("حذف شد"); load(); } }
        else if (d.chat) ctx.go("chat", { account: d.chat });
        else if (d.self) ctx.go("self", { account: d.self });
      } catch (err) { fail(err); }
    };
    load(); const t = setInterval(load, 12000);
    return () => { alive = false; clearInterval(t); };
  },
};
