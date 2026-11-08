// Shared helpers: API client (with retry handling), toasts, modal, formatting, icons.
export const state = { user: null, csrf: "" };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => [...r.querySelectorAll(s)];
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export class ApiError extends Error { constructor(m, status) { super(m); this.status = status; } }
let onUnauth = () => {};
export const setUnauthHandler = (fn) => { onUnauth = fn; };

export async function api(path, { method = "GET", body, form, quiet = false } = {}) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (method !== "GET" && state.csrf) opts.headers["X-CSRF-Token"] = state.csrf;
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  if (form) opts.body = form;
  const attempts = method === "GET" ? 3 : 1;
  for (let i = 0; i < attempts; i++) {
    let r;
    try { r = await fetch(path, opts); }
    catch { if (i < attempts - 1) { await sleep(1500); continue; } throw new ApiError("اتصال به سرور برقرار نشد", 0); }
    if ([502, 503, 504].includes(r.status) && i < attempts - 1) { await sleep(2500); continue; }
    let data = null;
    try { data = await r.json(); } catch { /* non-JSON */ }
    if (!r.ok) {
      if (r.status === 401 && !path.endsWith("/login") && !quiet) onUnauth();
      const msg = data?.error || (r.status >= 502 ? "سرور هنوز آماده نیست؛ چند ثانیه بعد دوباره تلاش کن" : `خطا (${r.status})`);
      throw new ApiError(msg, r.status);
    }
    return data;
  }
}

/** Wait for the Oracle API/container to become healthy before showing the login. */
export async function waitForServer(onWait, maxMs = 150000) {
  const t0 = Date.now();
  while (Date.now() - t0 < maxMs) {
    try {
      const r = await fetch("/api/ping", { cache: "no-store" });
      if (r.ok && (await r.json()).ok) return true;
    } catch { /* retry */ }
    onWait?.(Math.round((Date.now() - t0) / 1000));
    await sleep(2500);
  }
  return false;
}

export function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "bad" ? 6000 : 3500);
}
export const fail = (e) => toast(e?.message || "خطای ناشناخته", "bad");

export function modal(html, { onClose } = {}) {
  const bg = document.createElement("div");
  bg.className = "modal-bg";
  bg.innerHTML = `<div class="modal" role="dialog" aria-modal="true">${html}</div>`;
  const close = () => { bg.remove(); onClose?.(); };
  bg.addEventListener("mousedown", (e) => { if (e.target === bg) close(); });
  document.body.append(bg);
  return { el: bg.firstElementChild, close };
}
export function confirmBox(text, okLabel = "تأیید") {
  return new Promise((res) => {
    const m = modal(`<h3>مطمئنی؟</h3><p class="muted">${esc(text)}</p>
      <div class="row" style="margin-top:16px;justify-content:flex-end"><button class="btn ghost" data-n>انصراف</button><button class="btn danger" data-y>${esc(okLabel)}</button></div>`,
      { onClose: () => res(false) });
    $("[data-n]", m.el).onclick = () => m.close();
    $("[data-y]", m.el).onclick = () => { res(true); m.el.parentElement.remove(); };
  });
}

const nf = new Intl.DateTimeFormat("fa-IR", { dateStyle: "short", timeStyle: "short" });
const tf = new Intl.DateTimeFormat("fa-IR", { hour: "2-digit", minute: "2-digit" });
export const fmt = (v) => { if (!v) return "—"; try { return nf.format(new Date(v)); } catch { return v; } };
export const fmtTime = (v) => { if (!v) return ""; try { return tf.format(new Date(v)); } catch { return ""; } };
export const ago = (v) => {
  if (!v) return "—"; const s = (Date.now() - new Date(v)) / 1000;
  if (s < 60) return "چند ثانیه پیش"; if (s < 3600) return `${Math.floor(s / 60)} دقیقه پیش`;
  if (s < 86400) return `${Math.floor(s / 3600)} ساعت پیش`; return `${Math.floor(s / 86400)} روز پیش`;
};
export const initials = (s) => (String(s || "?").trim().match(/\p{L}|\p{N}/u)?.[0] || "?").toUpperCase();
export const sizeOf = (b) => (b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1024)) + " KB");

const I = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
export const icons = {
  home: I('<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/>'),
  users: I('<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6"/><path d="M16 4.5a3.5 3.5 0 010 7M18 14.2c2.2.6 3.5 2.4 3.5 5.8"/>'),
  chat: I('<path d="M21 12a8 8 0 01-11.5 7.2L3 21l1.8-5.5A8 8 0 1121 12z"/>'),
  bolt: I('<path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z"/>'),
  shield: I('<path d="M12 3l8 3v6c0 4.5-3.2 8-8 9-4.8-1-8-4.5-8-9V6l8-3z"/><path d="M9 12l2.2 2.2L15.5 10"/>'),
  list: I('<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1"/><circle cx="3.5" cy="12" r="1"/><circle cx="3.5" cy="18" r="1"/>'),
  menu: I('<path d="M4 7h16M4 12h16M4 17h16"/>'),
  send: I('<path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/>'),
  clip: I('<path d="M21 11.5l-8.6 8.6a5 5 0 01-7-7l9-9a3.3 3.3 0 014.7 4.7l-9 9a1.7 1.7 0 01-2.4-2.4l8.3-8.3"/>'),
  refresh: I('<path d="M21 12a9 9 0 11-3-6.7L21 8"/><path d="M21 3v5h-5"/>'),
  trash: I('<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>'),
  play: I('<path d="M7 4l13 8-13 8V4z"/>'),
  pause: I('<path d="M8 4v16M16 4v16"/>'),
  back: I('<path d="M9 5l7 7-7 7"/>'),
  plus: I('<path d="M12 5v14M5 12h14"/>'),
  lock: I('<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 018 0v3"/>'),
  search: I('<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>'),
  x: I('<path d="M6 6l12 12M18 6L6 18"/>'),
  reply: I('<path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 016 6v3"/>'),
  eye: I('<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>'),
  file: I('<path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8l-5-5z"/><path d="M14 3v5h5"/>'),
};
