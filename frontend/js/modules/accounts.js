import { api, esc, fail, toast, state, icons, ago, confirmBox, initials, $ } from "../core.js";

const STAGE_TEXT = { desktop_login: "ورود در Telegram Desktop" };

export function pill(a) {
  if (a.connected) return `<span class="pill ok"><i></i>ورود تأیید شده</span>`;
  if (a.desktop_running) return `<span class="pill info"><i></i>Telegram Desktop باز است</span>`;
  if (STAGE_TEXT[a.stage]) return `<span class="pill info"><i></i>${STAGE_TEXT[a.stage]}</span>`;
  if (a.status === "error") return `<span class="pill bad"><i></i>خطا</span>`;
  return `<span class="pill warn"><i></i>نیازمند ورود</span>`;
}

function card(a, user) {
  const owner = user.role === "owner";
  const consent = a.can_toggle_access
    ? `<div class="consent ${a.owner_access ? "on" : ""}"><div class="row"><div class="grow">${a.owner_access
        ? "مالک می‌تواند چت‌ها و پیام‌های این اکانت را ببیند و برایش پیام/سلف بسازد."
        : "مالک فقط نام و وضعیت اتصال را می‌بیند؛ محتوای چت‌ها و کارهایت پنهان است."}</div>
        <button class="btn sm ${a.owner_access ? "danger" : ""}" data-consent="${a.id}" data-on="${a.owner_access}">${a.owner_access ? "لغو دسترسی مالک" : "اجازهٔ دسترسی به مالک"}</button></div></div>`
    : (owner && !a.mine ? `<div class="consent ${a.owner_access ? "on" : ""}">${a.owner_access ? "✔ ادمین به تو دسترسی کامل به این اکانت داده است." : "🔒 محتوای این اکانت بدون اجازهٔ ادمین باز نمی‌شود."}</div>` : "");
  const actions = [];
  if (a.can_use) actions.push(`<button class="btn primary sm" data-login="${a.id}">${a.desktop_running ? "باز کردن Telegram Desktop" : "اجرای Telegram Desktop"}</button>`);
  if (a.connected && a.can_use) actions.push(`<button class="btn sm" data-open="self" data-id="${a.id}">${icons.bolt} سلف و اتوماسیون</button>`);
  if (a.can_manage) actions.push(`<button class="btn sm icon" title="اتصال دوباره" data-restart="${a.id}">${icons.refresh}</button>`, `<button class="btn sm danger icon" title="حذف" data-del="${a.id}" data-label="${esc(a.label)}">${icons.trash}</button>`);
  return `<div class="card acc">
    <div class="acc-top"><div class="avatar">${esc(initials(a.tg_name || a.label))}</div>
      <div class="grow"><b style="font-size:15px">${esc(a.label)}</b><div class="small muted row" style="gap:6px"><span class="tg">✈</span>Telegram${owner && !a.mine ? ` · ادمین: <b>${esc(a.holder)}</b>` : ""}</div></div>${pill(a)}</div>
    <div class="kv"><span>محیط دسکتاپ</span><b>${a.desktop_running ? "در حال اجرا" : "خاموش"}</b>
      <span>ورود</span><b>${a.connected ? "تأیید شده" : "نیازمند ورود یا تأیید"}</b>
      <span>آخرین فعالیت</span><b>${ago(a.last_seen_at)}</b></div>
    ${a.last_error && !a.connected ? `<div class="err small">${esc(a.last_error)}</div>` : ""}
    ${consent}
    <div class="row wrap">${actions.join("")}</div></div>`;
}

export default {
  id: "accounts", title: "اکانت‌ها", eyebrow: "ACCOUNTS", icon: "users", roles: ["owner", "admin"],
  mount(el, ctx) {
    const owner = state.user.role === "owner";
    let rows = [], filter = "all", alive = true;
    el.innerHTML = `
      <div class="grid g2" style="grid-template-columns:2fr 1fr;align-items:start">
        <div class="col">${owner ? `<div class="tabs" id="tabs"><button class="tab active" data-f="all">همه</button><button class="tab" data-f="mine">اکانت‌های من</button><button class="tab" data-f="admins">اکانت ادمین‌ها</button></div>` : ""}
          <div class="grid g2" id="list"></div></div>
        <div class="card"><h3>افزودن اکانت</h3><p class="sub">هر تعداد اکانت می‌توانی وصل کنی</p>
          <form id="add" class="col"><label>نام دلخواه<input name="label" maxlength="80" required placeholder="مثلاً اکانت اصلی"></label>
          <button class="btn primary">${icons.plus} ساخت و اتصال</button></form>
          <p class="hint" style="margin-top:12px">ورود داخل برنامه رسمی Telegram Desktop و با QR یا شماره انجام می‌شود. نشست هر اکانت جداگانه روی دیسک دائمی Oracle ذخیره می‌شود.</p></div>
      </div>`;

    const list = $("#list", el);
    function draw() {
      const shown = rows.filter((a) => filter === "all" || (filter === "mine" ? a.mine : !a.mine));
      list.innerHTML = shown.length ? shown.map((a) => card(a, state.user)).join("") : `<div class="card empty" style="grid-column:1/-1">اکانتی برای نمایش نیست.</div>`;
    }
    async function load() { try { rows = await api("/api/accounts"); if (alive) draw(); } catch (e) { fail(e); } }
    if (owner) $("#tabs", el).onclick = (e) => { const b = e.target.closest("[data-f]"); if (!b) return; filter = b.dataset.f; el.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === b)); draw(); };

    $("#add", el).onsubmit = async (e) => {
      e.preventDefault();
      try {
        const a = await api("/api/accounts", { method: "POST", body: { label: new FormData(e.target).get("label") } });
        e.target.reset(); await load();
        await api(`/api/accounts/${a.id}/desktop/open`, { method: "POST" });
        ctx.go("chat", { account: a.id });
      } catch (err) { fail(err); }
    };
    list.onclick = async (e) => {
      const t = e.target.closest("button"); if (!t) return;
      const d = t.dataset;
      try {
        if (d.login) {
          await api(`/api/accounts/${d.login}/desktop/open`, { method: "POST" });
          await load(); ctx.go("chat", { account: d.login });
        }
        else if (d.open) ctx.go(d.open, { account: d.id });
        else if (d.restart) { t.disabled = true; await api(`/api/accounts/${d.restart}/restart`, { method: "POST" }); toast("اتصال دوباره بررسی شد", "good"); await load(); }
        else if (d.del) { if (await confirmBox(`اکانت «${d.label}» و همهٔ کارهایش حذف شود؟ پوشه نشست Telegram Desktop هم از سرور پاک می‌شود.`, "حذف")) { await api(`/api/accounts/${d.del}`, { method: "DELETE" }); toast("حذف شد"); await load(); } }
        else if (d.consent) {
          const on = d.on !== "true";
          if (on && !(await confirmBox("با روشن‌کردن، مالک می‌تواند صفحهٔ Telegram Desktop این اکانت را ببیند و کنترل کند و کارهای زمان‌بندی‌شده را اجرا کند. هر لحظه می‌توانی اجازه را لغو کنی.", "اجازه می‌دهم"))) return;
          await api(`/api/accounts/${d.consent}/owner-access`, { method: "POST", body: { enabled: on } }); await load();
        }
      } catch (err) { fail(err); await load(); }
    };

    load();
    const timer = setInterval(load, 8000);
    if (ctx.params?.new) $("[name=label]", el).focus();
    return () => { alive = false; clearInterval(timer); };
  },
};

// Telegram Desktop is launched on the Oracle VM. The user completes login in the
// official GUI itself; this dashboard never collects Telegram login codes or passwords.
