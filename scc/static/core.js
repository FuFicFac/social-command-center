// core.js — shared plumbing: DOM helper, icons, API, store, time utils, toasts, modals.
// No frameworks. Every server string goes through textContent / text nodes (never innerHTML).

// ───────────────────────────────────────────────────────────── constants ──
export const ORDER = ["tiktok", "instagram", "linkedin", "x", "facebook", "youtube"];
export const PLAT = {
  youtube:   { name: "YouTube",   color: "#FF0033", limit: 5000,  limitWhat: "description" },
  instagram: { name: "Instagram", color: "#E1306C", limit: 2200,  limitWhat: "caption" },
  facebook:  { name: "Facebook",  color: "#1877F2", limit: 63206, limitWhat: "caption" },
  tiktok:    { name: "TikTok",    color: "#FE2C55", limit: 2200,  limitWhat: "caption" },
  linkedin:  { name: "LinkedIn",  color: "#0A66C2", limit: 3000,  limitWhat: "caption" },
  x:         { name: "X",         color: "var(--text)", limit: 280, limitWhat: "caption" },
};
export const ROUTE_LABEL = { dryrun: "Dry run", direct: "Direct", upload_post: "Upload-Post" };
export const STATUS = {
  pending:      ["Waiting", "muted"],
  uploading:    ["Uploading…", "info"],
  processing:   ["Processing…", "info"],
  published:    ["Live", "ok"],
  private:      ["Uploaded (private)", "ok"],
  inbox:        ["In TikTok inbox — tap Post", "warn"],
  simulated:    ["Dry run", "muted"],
  failed:       ["Failed", "bad"],
  needs_review: ["Check manually", "warn"],
  skipped:      ["Skipped", "muted"],
};
export const DAY_KEYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];
export const DAY_NAMES = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };

// UI state that survives view switches (module singleton).
export const ui = {
  libFilter: "all", libQuery: "", selected: new Set(),
  calMode: "week", calAnchor: null,
};

// ───────────────────────────────────────────────────────────────── DOM ──
const PROPS = new Set(["value", "checked", "disabled", "hidden", "draggable", "selected", "indeterminate", "readOnly", "textContent"]);
/** h("div", {class:"x", onclick:fn, dataset:{a:1}}, child, [children]) */
export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) { if (PROPS.has(k) && v === false) el[k] = false; continue; }
      if (k === "class") el.className = v;
      else if (k === "dataset") Object.assign(el.dataset, v);
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (PROPS.has(k)) el[k] = v;
      else el.setAttribute(k, v === true ? "" : String(v));
    }
  }
  append(el, kids);
  return el;
}
export function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
export function clear(el) { el.replaceChildren(); return el; }
export function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
export function safeUrl(u) {
  try { const x = new URL(String(u)); return x.protocol === "http:" || x.protocol === "https:" ? x.href : null; }
  catch { return null; }
}
export const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + "s")}`;

// Small per-viewer prefs; storage can throw (private windows) so everything is wrapped.
export const pref = {
  get(k, d = null) { try { const v = localStorage.getItem("scc." + k); return v == null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("scc." + k, v); } catch { /* ignore */ } },
};

// ──────────────────────────────────────────────────────────────── icons ──
const I = {
  library: '<rect x="3" y="3" width="7" height="7" rx="1.8"/><rect x="14" y="3" width="7" height="7" rx="1.8"/><rect x="3" y="14" width="7" height="7" rx="1.8"/><rect x="14" y="14" width="7" height="7" rx="1.8"/>',
  calendar: '<rect x="3" y="4.5" width="18" height="16" rx="3"/><path d="M3 9.5h18M8 3v3M16 3v3"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  settings: '<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>',
  sparkle: '<path d="M11 3l1.9 5.6L18.5 10.5 12.9 12.4 11 18l-1.9-5.6L3.5 10.5 9.1 8.6z"/><path d="M19 15.5l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  chevL: '<path d="M14.5 6l-6 6 6 6"/>',
  chevR: '<path d="M9.5 6l6 6-6 6"/>',
  chevD: '<path d="M6 9.5l6 6 6-6"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  grip: '<circle cx="9" cy="6" r="1.2"/><circle cx="15" cy="6" r="1.2"/><circle cx="9" cy="12" r="1.2"/><circle cx="15" cy="12" r="1.2"/><circle cx="9" cy="18" r="1.2"/><circle cx="15" cy="18" r="1.2"/>',
  alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17.2v.1"/>',
  shuffle: '<path d="M3 7h3c4 0 5 10 9 10h3M3 17h3c1.3 0 2.2-.8 3-2M15 7h3c-1 0 0 0 0 0M13 9c.8-1.2 1.6-2 3-2h2"/><path d="M16 4l3 3-3 3M16 14l3 3-3 3"/>',
  up: '<path d="M6 14.5l6-6 6 6"/>',
  down: '<path d="M6 9.5l6 6 6-6"/>',
  video: '<rect x="3" y="5" width="13" height="14" rx="3"/><path d="M16 10l5-3v10l-5-3z"/>',
  bolt: '<path d="M13 3L5 13.5h6L10 21l8-10.5h-6z"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2.5"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5M12 7.8v.1"/>',
};
export function icon(name, size = 18) {
  const s = h("span", { class: "ico", "aria-hidden": "true" });
  s.style.setProperty("--s", size + "px");
  s.innerHTML = `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${I[name] || ""}</svg>`;
  return s;
}
const G = {
  linkedin: '<rect x="2" y="2" width="20" height="20" rx="3" fill="currentColor"/><path fill="#fff" d="M5 10h3v9H5zm1.5-4.6a1.6 1.6 0 1 1 0 3.2 1.6 1.6 0 0 1 0-3.2zM10 10h2.8v1.3c.5-.9 1.5-1.5 2.9-1.5 2.5 0 3.4 1.5 3.4 4.2v5h-3v-4.6c0-1.4-.4-2-1.5-2-1.2 0-1.6.8-1.6 2V19h-3z"/>',
  x: '<path fill="currentColor" d="M3 3h5.2l5 6.7L19 3h2l-6.9 8 7 10h-5.2l-5.5-7.5L4 21H2l7.6-8.9L3 3zm4.4 1.5H5.9l10.8 15H18z"/>',
  youtube: '<rect x="2" y="5" width="20" height="14" rx="4.5" fill="currentColor"/><path d="M10 9v6l5.2-3z" fill="#fff"/>',
  instagram: '<rect x="3.5" y="3.5" width="17" height="17" rx="5.2" fill="none" stroke="currentColor" stroke-width="2.2"/><circle cx="12" cy="12" r="3.9" fill="none" stroke="currentColor" stroke-width="2.2"/><circle cx="17.1" cy="6.9" r="1.3" fill="currentColor"/>',
  facebook: '<circle cx="12" cy="12" r="10" fill="currentColor"/><path d="M13.2 21v-7h2.3l.4-2.8h-2.7V9.5c0-.8.3-1.3 1.4-1.3h1.4V5.7c-.3 0-1.1-.1-2.1-.1-2.1 0-3.4 1.2-3.4 3.5v2.1H8v2.8h2.5v7z" fill="#fff"/>',
  tiktok: '<path d="M14.2 3v10.6a3.4 3.4 0 1 1-3.4-3.4" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/><path d="M14.2 3c.3 2.6 1.9 4.3 4.6 4.6" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/>',
};
export function platIcon(p, size = 16) {
  const s = h("span", { class: "pico", title: PLAT[p]?.name || p });
  s.style.color = PLAT[p]?.color || "currentColor";
  s.innerHTML = `<svg viewBox="0 0 24 24" width="${size}" height="${size}" aria-hidden="true">${G[p] || ""}</svg>`;
  return s;
}

// ───────────────────────────────────────────────────────────────── API ──
export class ApiError extends Error {
  constructor(message, status, issues) { super(message); this.status = status; this.issues = issues || []; }
}
const TOKEN = document.querySelector('meta[name="scc-token"]')?.content ?? "";
async function request(method, path, body) {
  let r;
  try {
    r = await fetch(path, {
      method, cache: "no-store",
      headers: method === "POST" ? { "Content-Type": "application/json", "X-SCC-Token": TOKEN } : undefined,
      body: method === "POST" ? JSON.stringify(body ?? {}) : undefined,
    });
  } catch { throw new ApiError("Can't reach the dashboard server", 0); }
  const data = await r.json().catch(() => ({}));
  if (r.status === 403 && data.error === "forbidden") {
    // The server was restarted and issued a new token — this page needs a reload.
    throw new ApiError("The dashboard restarted. Reload this page (⌘R) and try again.", 403);
  }
  if (!r.ok) throw new ApiError(data.error || r.statusText || `HTTP ${r.status}`, r.status, data.issues);
  return data;
}
export const apiGet = (path) => request("GET", path);
export const apiPost = (path, body) => request("POST", path, body);
export function errText(e) {
  if (!(e instanceof ApiError)) return String(e?.message || e);
  if (e.status === 404) return `${e.message} (this server doesn't support that yet)`;
  return e.message;
}

// ──────────────────────────────────────────────────────────────── store ──
export const store = { state: null, offline: false, listeners: new Set() };
let inflight = null;
export function refreshState() {
  if (inflight) return inflight;
  inflight = apiGet("/api/state").then(
    (s) => { store.state = s; store.offline = false; },
    () => { store.offline = true; },
  ).finally(() => { inflight = null; for (const fn of [...store.listeners]) { try { fn(); } catch (e) { console.error(e); } } });
  return inflight;
}
export function onState(fn) { store.listeners.add(fn); return () => store.listeners.delete(fn); }
export function startPolling() { refreshState(); setInterval(() => { if (!document.hidden) refreshState(); }, 4000); }
export const S = () => store.state;
export const posts = () => store.state?.posts || [];
export const postById = (id) => posts().find((p) => p.id === id);
export const settings = () => store.state?.settings || {};
export const tz = () => settings().timezone || Intl.DateTimeFormat().resolvedOptions().timeZone;

// ───────────────────────────────────────────────────────────── post utils ──
export const activeDeliveries = (p) => Object.entries(p.deliveries || {}).filter(([, d]) => d.status !== "skipped");
// A destination is a platform plus the account that receives it. Its key is "tiktok:<account id>",
// or just "tiktok" while no account has been chosen.
export const platOf = (key) => String(key).split(":")[0];
export const destinationsOf = (p) => (p.destinations || []).filter((d) => d.status !== "skipped");
export const accounts = () => store.state?.accounts || [];
export const brands = () => store.state?.brands || [];
export const brandName = (id) => brands().find((b) => b.id === id)?.name || "";
export const accountById = (id) => accounts().find((a) => a.id === id);
/** "@handle" (or the channel name) for an account or a destination. */
export const accountLabel = (x) => x?.label || x?.account || x?.handle || x?.name || "";
/** "A-List · TikTok @handle, Instagram @handle" — where a short is going. */
export function destinationSummary(p, { short = false } = {}) {
  const live = destinationsOf(p);
  const named = live.filter((d) => d.account);
  const names = named.map((d) => (short ? "" : PLAT[d.platform].name + " ") + d.account);
  const open = live.length - named.length;
  if (open) names.push(named.length ? `${open} more with no account` : "no accounts chosen yet");
  return [p.brand_name, names.join(", ")].filter(Boolean).join(" · ");
}
export const targetsOf = (p) => activeDeliveries(p).map(([k]) => k);
export const titleOf = (p) => (p.title || "").trim() || (p.filename || "").replace(/\.[^.]+$/, "") || p.id;
export const isLocked = (p) => p.bucket === "posted" || p.bucket === "posting";
export const isUnscheduled = (p) => !p.scheduled_at && (p.bucket === "draft" || p.bucket === "scheduled");
export function effectiveCaption(p, pl) { return (p.overrides?.[pl]?.caption || "").trim() || p.caption || ""; }
export function jobOf(id) {
  const s = store.state; if (!s) return null;
  if (s.jobs?.[id]) return s.jobs[id];
  const q = s.caption_queue;
  if (q?.running === id) return "drafting captions";
  if (q?.queued?.includes(id)) return "queued for captions";
  return null;
}

// ──────────────────────────────────────────────────────────── time utils ──
const dtfCache = new Map();
function dtf(zone, opts, key) {
  const k = zone + "|" + key;
  if (!dtfCache.has(k)) dtfCache.set(k, new Intl.DateTimeFormat("en-US", { timeZone: zone, ...opts }));
  return dtfCache.get(k);
}
const PARTS = { hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", second: "numeric" };
export function zoneParts(date, zone = tz()) {
  const o = {};
  for (const p of dtf(zone, PARTS, "parts").formatToParts(date)) o[p.type] = p.value;
  const y = +o.year, mo = +o.month, d = +o.day;
  return { y, mo, d, h: +o.hour % 24, mi: +o.minute, s: +o.second, dow: new Date(Date.UTC(y, mo - 1, d)).getUTCDay() };
}
function offsetMs(ts, zone) {
  const p = zoneParts(new Date(ts), zone);
  return Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi, p.s) - Math.floor(ts / 1000) * 1000;
}
/** Local wall-clock (ymd + "HH:MM") in `zone` → UTC Date. Handles DST by re-checking the offset. */
export function zonedToUtc(ymd, hhmm, zone = tz()) {
  const [y, m, d] = ymd.split("-").map(Number);
  const [hh, mm] = (hhmm || "00:00").split(":").map(Number);
  const guess = Date.UTC(y, m - 1, d, hh, mm);
  let t = guess - offsetMs(guess, zone);
  const off2 = offsetMs(t, zone);
  if (guess - off2 !== t) t = guess - off2;
  return new Date(t);
}
export const toWire = (date) => date.toISOString().replace(/\.\d{3}Z$/, "+00:00");
const pad = (n) => String(n).padStart(2, "0");
export function ymdIn(dateOrIso, zone = tz()) {
  const p = zoneParts(dateOrIso instanceof Date ? dateOrIso : new Date(dateOrIso), zone);
  return `${p.y}-${pad(p.mo)}-${pad(p.d)}`;
}
export function hhmmIn(dateOrIso, zone = tz()) {
  const p = zoneParts(dateOrIso instanceof Date ? dateOrIso : new Date(dateOrIso), zone);
  return `${pad(p.h)}:${pad(p.mi)}`;
}
export const todayYmd = () => ymdIn(new Date());
const ymdDate = (ymd) => { const [y, m, d] = ymd.split("-").map(Number); return new Date(Date.UTC(y, m - 1, d)); };
const dateYmd = (dt) => `${dt.getUTCFullYear()}-${pad(dt.getUTCMonth() + 1)}-${pad(dt.getUTCDate())}`;
export function addDays(ymd, n) { const d = ymdDate(ymd); d.setUTCDate(d.getUTCDate() + n); return dateYmd(d); }
export function addMonths(ymd, n) { const d = ymdDate(ymd); d.setUTCDate(1); d.setUTCMonth(d.getUTCMonth() + n); return dateYmd(d); }
export const dowOf = (ymd) => ymdDate(ymd).getUTCDay();
/** Monday-first week start. */
export function weekStart(ymd) { return addDays(ymd, -((dowOf(ymd) + 6) % 7)); }
export const minuteKey = (iso) => Math.floor(Date.parse(iso) / 60000);

const fmtOpt = {
  time: { hour: "numeric", minute: "2-digit" },
  slot: { weekday: "short", month: "short", day: "numeric" },
};
export function fmtTime(iso) { return dtf(tz(), fmtOpt.time, "t").format(new Date(iso)); }
/** "Fri Oct 31 · 9:00 AM" */
export function fmtSlot(iso, sep = " · ") {
  const d = new Date(iso);
  const day = dtf(tz(), fmtOpt.slot, "s").format(d).replace(",", "");
  return `${day}${sep}${fmtTime(iso)}`;
}
export function fmt12(hhmm) {
  const [h24, m] = hhmm.split(":").map(Number);
  return `${((h24 + 11) % 12) + 1}:${pad(m)} ${h24 < 12 ? "AM" : "PM"}`;
}
export function fmtYmd(ymd, opts = { month: "short", day: "numeric" }) {
  return new Intl.DateTimeFormat("en-US", { timeZone: "UTC", ...opts }).format(ymdDate(ymd));
}
export function fmtDuration(sec) {
  if (sec == null || isNaN(sec)) return "";
  const s = Math.round(sec);
  return `${Math.floor(s / 60)}:${pad(s % 60)}`;
}
export function relTime(iso) {
  if (!iso) return "never";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} hr ago`;
  return `${Math.round(s / 86400)} day${Math.round(s / 86400) === 1 ? "" : "s"} ago`;
}
/** Slot instants from settings alone (used for past days / when /api/slots is unavailable). */
export function localSlots(fromYmd, days) {
  const sc = settings().schedule || { slots: [], days: [] };
  const times = [...(sc.slots || [])].sort();
  const out = [];
  for (let i = 0; i < days; i++) {
    const ymd = addDays(fromYmd, i);
    if (!(sc.days || []).includes(DAY_KEYS[dowOf(ymd)])) continue;
    for (const t of times) out.push({ at: toWire(zonedToUtc(ymd, t)), ymd, time: t, post_id: null });
  }
  return out;
}
export function cadenceSummary() {
  const sc = settings().schedule || { slots: [], days: [] };
  const slots = [...(sc.slots || [])].sort().map(fmt12);
  const days = sc.days || [];
  if (!slots.length || !days.length) return "No posting times set yet";
  const when = slots.length === 1 ? slots[0] : slots.slice(0, -1).join(", ") + " & " + slots.at(-1);
  const every = days.length === 7 ? "every day" : "on " + ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].filter((d) => days.includes(d)).map((d) => DAY_NAMES[d]).join(", ");
  return `${slots.length} per day at ${when}, ${every}`;
}

// ───────────────────────────────────────────────────────────── components ──
export function statusChip(status) {
  const [label, kind] = STATUS[status] || [status, "muted"];
  return h("span", { class: `chip chip-${kind}` }, label);
}
/** One status dot per destination. */
export function platformDots(p) {
  const wrap = h("span", { class: "pdots" });
  for (const pl of ORDER) {
    const d = p.deliveries?.[pl];
    const st = d ? d.status : "skipped";
    const label = st === "skipped" ? "not posting" : (STATUS[st]?.[0] || st);
    const dot = h("span", { class: `pdot st-${st}`, title: `${PLAT[pl].name}: ${label}` });
    dot.style.setProperty("--pc", PLAT[pl].color);
    wrap.append(dot);
  }
  return wrap;
}

// Toasts
export function toast(message, opts = {}) {
  const { kind = "info", details = [], ms } = opts;
  const root = $("#toasts");
  const t = h("div", { class: `toast toast-${kind}`, role: "status" },
    h("div", { class: "toast-msg" }, message),
    details.length ? h("ul", { class: "toast-details" }, details.slice(0, 8).map((d) => h("li", null, d)), details.length > 8 ? h("li", null, `…and ${details.length - 8} more`) : null) : null,
    h("button", { class: "toast-x", "aria-label": "Dismiss", onclick: () => t.remove() }, icon("x", 14)));
  root.append(t);
  const life = ms ?? (kind === "error" || details.length ? 9000 : 4200);
  setTimeout(() => t.remove(), life);
  return t;
}

// Modals on <dialog>. actions: [{label, kind:"primary|danger|ghost", onClick: async()=>false to stay open}]
export function openModal({ title, content, actions = [], wide = false, onClose }) {
  const dlg = h("dialog", { class: `modal${wide ? " wide" : ""}` });
  const body = h("div", { class: "modal-body" }, content);
  const foot = h("div", { class: "modal-foot" });
  const handle = { el: dlg, body, foot, result: undefined, close(v) { handle.result = v; if (dlg.open) dlg.close(); } };
  for (const a of actions) {
    const b = h("button", { class: `btn ${a.kind || ""}`, type: "button" }, a.label);
    b.addEventListener("click", async () => {
      if (!a.onClick) return handle.close(a.value);
      b.disabled = true;
      try { const r = await a.onClick(handle); if (r !== false) handle.close(a.value); } finally { b.disabled = false; }
    });
    a.el = b; foot.append(b);
  }
  dlg.append(
    h("div", { class: "modal-head" }, h("h2", null, title), h("button", { class: "icon-btn", "aria-label": "Close", onclick: () => handle.close() }, icon("x"))),
    body, actions.length ? foot : null);
  dlg.addEventListener("click", (e) => { if (e.target === dlg) handle.close(); });
  dlg.addEventListener("close", () => { dlg.remove(); onClose?.(handle.result); });
  document.body.append(dlg);
  dlg.showModal();
  return handle;
}
export function confirmModal({ title, message, content, confirmLabel = "Confirm", danger = false, cancelLabel = "Cancel" }) {
  return new Promise((resolve) => {
    openModal({
      title, content: content || h("p", { class: "modal-text" }, message),
      actions: [{ label: cancelLabel, kind: "ghost", value: false }, { label: confirmLabel, kind: danger ? "danger-solid" : "primary", value: true }],
      onClose: (v) => resolve(v === true),
    });
  });
}

// Popover anchored under a button; closes on outside click / Esc.
export function openPopover(anchor, content) {
  const pop = h("div", { class: "popover" }, content);
  document.body.append(pop);
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth, hgt = pop.offsetHeight;
  pop.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left)) + "px";
  const above = r.top - hgt - 8 > 8;
  pop.style.top = (above ? r.top - hgt - 8 : r.bottom + 8) + "px";
  const close = () => { pop.remove(); document.removeEventListener("mousedown", away, true); document.removeEventListener("keydown", esc, true); };
  const away = (e) => { if (!pop.contains(e.target) && !anchor.contains(e.target)) close(); };
  const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  setTimeout(() => { document.addEventListener("mousedown", away, true); document.addEventListener("keydown", esc, true); });
  return { el: pop, close };
}

export function thumbImg(p, cls = "") {
  const img = h("img", { class: cls, src: `/thumb/${p.id}.jpg`, loading: "lazy", alt: "", draggable: false });
  img.addEventListener("error", () => { img.style.visibility = "hidden"; });
  return img;
}

export function go(hash) { if (location.hash !== hash) location.hash = hash; }
export const openPost = (id) => go(`#/post/${id}`);

/** Standard bulk-result handling: toast + return the list of failed ids. */
export function reportBulk(res, okMsg, failVerb) {
  const results = res?.results || {};
  const failed = Object.entries(results).filter(([, v]) => v !== "ok");
  const okN = Object.keys(results).length - failed.length;
  const titleFor = (id) => { const p = postById(id); return p ? titleOf(p) : id; };
  if (failed.length) {
    toast(`${okN ? okN + " done, " : ""}${failed.length} couldn't be ${failVerb}`, {
      kind: "error", details: failed.map(([id, msg]) => `${titleFor(id)}: ${msg}`),
    });
  } else if (okMsg) toast(okMsg(okN), { kind: "success" });
  return failed.map(([id]) => id);
}
