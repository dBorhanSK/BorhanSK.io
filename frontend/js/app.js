import { $, $$, api, state, toast, fail, esc, icons, initials, setUnauthHandler, waitForServer } from "./core.js";
import dashboard from "./modules/dashboard.js";
import accounts from "./modules/accounts.js";
import chat from "./modules/chat.js";
import selfMod from "./modules/self.js";
import admin from "./modules/admin.js";
import logs from "./modules/logs.js";

// ---- Module registry --------------------------------------------------------------------------
// To add a new section later (e.g. "management"): create js/modules/management.js exporting
// { id, title, eyebrow, icon, roles, mount(el, ctx) -> cleanup } and add it to this list.
const MODULES = [dashboard, accounts, chat, selfMod, admin, logs];

let current = null, cleanup = null;
const ctx = { go, refreshEngine, get user() { return state.user; } };

function go(id, params = {}) {
  const mod = MODULES.find((m) => m.id === id && m.roles.includes(state.user.role)) || MODULES[0];
  try { cleanup?.(); } catch { /* ignore */ }
  current = mod;
  $$(".nav").forEach((n) => n.classList.toggle("active", n.dataset.id === mod.id));
  $("#title").textContent = mod.title;
  $("#eyebrow").textContent = mod.eyebrow;
  $("#side").classList.remove("open");
  const view = $("#view");
  view.innerHTML = "";
  cleanup = mod.mount(view, { ...ctx, params }) || null;
  if (location.hash !== "#" + mod.id) history.replaceState(null, "", "#" + mod.id);
}

function buildNav() {
  const mine = MODULES.filter((m) => m.roles.includes(state.user.role));
  $("#nav").innerHTML = mine.map((m) => `<button class="nav" data-id="${m.id}">${icons[m.icon] || ""}<span>${esc(m.title)}</span></button>`).join("");
  $$(".nav").forEach((n) => (n.onclick = () => go(n.dataset.id)));
  $("#me-name").textContent = state.user.name;
  $("#me-role").textContent = state.user.role === "owner" ? "مالک اصلی" : "ادمین";
  $("#me-av").textContent = initials(state.user.name);
  $("#menu-btn").innerHTML = icons.menu;
}

async function refreshEngine() {
  try {
    const o = await api("/api/overview", { quiet: true });
    const pill = $("#engine-pill");
    pill.className = "pill " + (o.engine_ready ? "ok" : "bad");
    pill.lastElementChild.textContent = o.engine_ready ? "موتور تلگرام فعال" : "موتور تلگرام تنظیم نشده";
  } catch { /* ignore */ }
}

function showLogin() {
  cleanup?.(); cleanup = null; state.user = null; state.csrf = "";
  $("#app").hidden = true; $("#login").hidden = false; $("#splash").hidden = true;
}

async function showApp(user) {
  state.user = user; state.csrf = user.csrf;
  $("#login").hidden = true; $("#app").hidden = false; $("#splash").hidden = true;
  buildNav();
  go(location.hash.slice(1) || "dashboard");
  refreshEngine();
}

setUnauthHandler(() => { if (state.user) { toast("نشست تمام شد؛ دوباره وارد شو", "bad"); showLogin(); } });

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target), btn = $("#login-btn"), errEl = $("#login-err"), wake = $("#login-wake");
  errEl.hidden = true; btn.disabled = true; btn.textContent = "در حال ورود…";
  const hint = setTimeout(() => (wake.hidden = false), 4000);
  try {
    const user = await api("/api/login", { method: "POST", body: { username: f.get("username"), password: f.get("password") } });
    e.target.reset(); await showApp(user);
  } catch (err) { errEl.textContent = err.message; errEl.hidden = false; }
  finally { clearTimeout(hint); wake.hidden = true; btn.disabled = false; btn.textContent = "ورود به پنل"; }
});

$("#logout-btn").onclick = async () => { try { await api("/api/logout", { method: "POST" }); } catch { /* ignore */ } showLogin(); };
$("#menu-btn").onclick = () => $("#side").classList.toggle("open");
window.addEventListener("hashchange", () => { if (state.user && current && location.hash.slice(1) !== current.id) go(location.hash.slice(1)); });

(async function boot() {
  const ok = await waitForServer((s) => { $("#splash-text").textContent = `در حال اتصال امن به سرور… (${s} ثانیه)`; });
  if (!ok) { $("#splash-text").textContent = "اتصال به سرور برقرار نشد؛ وضعیت سرویس Oracle و اینترنت را بررسی کن و دوباره تلاش کن."; return; }
  try { await showApp(await api("/api/me", { quiet: true })); } catch { showLogin(); }
})();
