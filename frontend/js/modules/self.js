// "Self" module — scheduled messages & button clicks. Independent from the chat client.
import { api, esc, fail, toast, state, icons, fmt, confirmBox, $ } from "../core.js";

export default {
  id: "self", title: "سلف و اتوماسیون", eyebrow: "SELF · AUTOMATION", icon: "bolt", roles: ["owner", "admin"],
  mount(el, ctx) {
    let accounts = [], jobs = [], alive = true;
    el.innerHTML = `<div id="noacc"></div>
      <div class="grid g2" id="top" style="align-items:start">
        <div class="card"><h3>ساخت کار جدید</h3><p class="sub">پیام یا کلیک روی دکمهٔ ربات، به‌صورت تکرارشونده</p>
          <form id="f" class="form">
            <label class="full">اکانت<select name="account_id" id="acc" required></select></label>
            <label>نام کار<input name="name" maxlength="80" required placeholder="مثلاً میو ۵ دقیقه‌ای"></label>
            <label>مقصد<input name="target" class="ltr" maxlength="160" required placeholder="@username یا عنوان دقیق چت / Saved Messages"></label>
            <label>نوع عملیات<select name="action" id="action"><option value="send">ارسال پیام</option><option value="click">کلیک روی دکمهٔ اینلاین</option></select></label>
            <label>هر چند دقیقه<input name="interval_minutes" type="number" min="1" max="1440" step="1" value="5" required></label>
            <label class="full"><span id="plabel">متن پیام (هر خط = یک متن؛ اگر چند خط بنویسی یکی‌یکی/تصادفی ارسال می‌شود)</span>
              <textarea name="payload" maxlength="3000" required placeholder="میو"></textarea></label>
            <label id="modewrap">انتخاب متن<select name="mode"><option value="random">تصادفی</option><option value="sequential">به‌ترتیب</option></select></label>
            <label>تعداد اجرا <span class="hint">(خالی = نامحدود)</span><input name="max_runs" type="number" min="1" placeholder="∞"></label>
            <label>از ساعت <span class="hint">(اختیاری)</span><input name="active_from" type="number" min="0" max="24" placeholder="مثلاً 9"></label>
            <label>تا ساعت <span class="hint">(اختیاری)</span><input name="active_to" type="number" min="0" max="24" placeholder="مثلاً 23"></label>
            <div class="full"><button class="btn primary">${icons.plus} ذخیره و فعال‌سازی</button></div>
          </form><p class="hint" style="margin-top:10px">ساعت‌ها به وقت <b>${esc(state.user.timezone || "")}</b>. دکمه با «متن دقیق» در ۸ پیام آخر گفتگو پیدا و کلیک می‌شود. حداقل فاصله ۱ دقیقه است.</p></div>
        <div class="card"><h3>ارسال آزمایشی</h3><p class="sub">قبل از ساخت کار یک‌بار تست کن</p>
          <form id="t" class="col"><label>اکانت<select name="account_id" id="tacc"></select></label>
            <label>مقصد<input name="target" class="ltr" required placeholder="@username"></label>
            <label>متن<input name="text" required placeholder="سلام"></label><button class="btn">${icons.send} ارسال آزمایشی</button></form></div>
      </div>
      <div class="card" style="margin-top:16px"><div class="row" style="margin-bottom:10px"><div class="grow"><h3>کارهای ذخیره‌شده</h3>
        <p class="sub" style="margin:0" id="jsub"></p></div><button class="btn sm" id="rj">${icons.refresh}</button></div>
        <div class="tablewrap"><table><thead><tr><th>کار</th><th>اکانت</th><th>مقصد</th><th>فاصله</th><th>وضعیت</th><th>اجرا</th><th>بعدی</th><th></th></tr></thead><tbody id="jb"></tbody></table></div></div>`;

    const accSel = [$("#acc", el), $("#tacc", el)];
    const updateAction = () => { const c = $("#action", el).value === "click"; $("#plabel", el).textContent = c ? "متن دقیق دکمه" : "متن پیام (هر خط = یک متن؛ اگر چند خط بنویسی یکی‌یکی/تصادفی ارسال می‌شود)"; $("#modewrap", el).hidden = c; };
    $("#action", el).onchange = updateAction;

    async function load() {
      try {
        const [a, j] = await Promise.all([api("/api/accounts"), api("/api/self/jobs")]);
        if (!alive) return; accounts = a; jobs = j;
        const usable = a.filter((x) => x.can_use && x.connected);
        const cur = accSel.map((s) => s.value);
        accSel.forEach((s, i) => { s.innerHTML = usable.map((x) => `<option value="${x.id}">${esc(x.label)}${!x.mine ? " (ادمین " + esc(x.holder) + ")" : ""}</option>`).join(""); if (cur[i] && usable.some((x) => x.id === cur[i])) s.value = cur[i]; else if (ctx.params?.account) s.value = ctx.params.account; });
        $("#noacc", el).innerHTML = usable.length ? "" : `<div class="err" style="margin-bottom:16px">اکانت متصلی نداری. اول در بخش «اکانت‌ها» وصل کن.</div>`;
        drawJobs();
      } catch (e) { fail(e); }
    }
    function drawJobs() {
      $("#jsub", el).textContent = `${jobs.length} کار · ${jobs.filter((j) => j.status === "active").length} فعال`;
      $("#jb", el).innerHTML = jobs.length ? jobs.map((j) => {
        const mine = j.owner_id === state.user.id, hidden = j.hidden;
        const canRun = mine || (!hidden);
        return `<tr><td><b>${esc(j.name)}</b>${j.last_error ? `<div class="small" style="color:var(--red)">${esc(j.last_error)}</div>` : ""}
          <div class="small muted">${hidden ? "🔒 محتوا پنهان (بدون اجازهٔ ادمین)" : esc(j.action === "click" ? "کلیک: " + j.payload : (j.payload.split("\n")[0].slice(0, 40) + (j.payload.includes("\n") ? " …" : "")))}</div></td>
          <td>${esc(j.account_label)}</td><td class="ltr">${esc(j.target)}</td><td>${Math.round(j.interval_seconds / 60)} د</td>
          <td><span class="pill ${j.status === "active" ? "ok" : "warn"}"><i></i>${j.status === "active" ? "فعال" : "متوقف"}</span></td>
          <td>${j.runs_count || 0}${j.max_runs ? " / " + j.max_runs : ""}</td><td class="small">${j.status === "active" ? fmt(j.next_run_at) : "—"}</td>
          <td><div class="row" style="gap:4px">${canRun ? `<button class="btn sm icon" title="اجرای فوری" data-a="run" data-id="${j.id}">${icons.bolt}</button>` : ""}
            ${j.status === "active" ? `<button class="btn sm icon" title="توقف" data-a="pause" data-id="${j.id}">${icons.pause}</button>` : (canRun ? `<button class="btn sm icon" title="ادامه" data-a="resume" data-id="${j.id}">${icons.play}</button>` : "")}
            <button class="btn sm danger icon" title="حذف" data-a="delete" data-id="${j.id}">${icons.trash}</button></div></td></tr>`;
      }).join("") : `<tr><td colspan="8" class="empty">هنوز کاری نساخته‌ای.</td></tr>`;
    }
    $("#jb", el).onclick = async (e) => {
      const b = e.target.closest("[data-a]"); if (!b) return;
      try {
        if (b.dataset.a === "delete" && !(await confirmBox("این کار حذف شود؟", "حذف"))) return;
        await api(`/api/self/jobs/${b.dataset.id}/${b.dataset.a}`, { method: "POST" });
        toast(b.dataset.a === "run" ? "تا چند ثانیه دیگر اجرا می‌شود" : "انجام شد", "good"); load();
      } catch (err) { fail(err); }
    };
    $("#f", el).onsubmit = async (e) => {
      e.preventDefault(); const o = Object.fromEntries(new FormData(e.target).entries());
      try { await api("/api/self/jobs", { method: "POST", body: o }); toast("کار ساخته و فعال شد", "good"); e.target.reset(); updateAction(); load(); } catch (err) { fail(err); }
    };
    $("#t", el).onsubmit = async (e) => {
      e.preventDefault(); const o = Object.fromEntries(new FormData(e.target).entries());
      try { await api("/api/self/send-test", { method: "POST", body: o }); toast("پیام آزمایشی ارسال شد", "good"); } catch (err) { fail(err); }
    };
    $("#rj", el).onclick = load;
    load(); updateAction();
    const t = setInterval(load, 15000);
    return () => { alive = false; clearInterval(t); };
  },
};
