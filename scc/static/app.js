// app.js — shell: router (location.hash), sidebar, banners, progress strip, polling.
//   #/library  #/calendar  #/activity  #/settings[/schedule]  #/post/<id> (drawer over the current view)
import {
  h, $, $$, icon, store, onState, startPolling, S, posts, postById, titleOf, ORDER, PLAT, ROUTE_LABEL, relTime, go, pref, confirmModal, plural,
} from "./core.js";
import { createLibrary } from "./library.js";
import { createCalendar } from "./calendar.js";
import { createActivity } from "./activity.js";
import { createSettings } from "./settings.js";
import { initEditor, openEditor, closeEditor, updateEditor, editorDirty, editorId } from "./editor.js";

const VIEWS = { library: createLibrary, calendar: createCalendar, activity: createActivity, settings: createSettings };
let lastView = "library", current = null, currentName = null, booted = false, accepted = "", suppress = false;

function parse(hash) {
  const [name, arg] = hash.replace(/^#\/?/, "").split("/");
  if (name === "post" && arg) return { name: "post", id: arg };
  return { name: VIEWS[name] ? name : "library", arg };
}

function mountView(name, arg) {
  if (current && currentName === name && name !== "settings") return;
  if (current && currentName === name && name === "settings" && !arg) return;
  current?.destroy?.();
  current = VIEWS[name](arg);
  currentName = name;
  const host = $("#view");
  host.replaceChildren(current.el);
  host.scrollTop = 0;
  paintNav();
}

function route() {
  const r = parse(location.hash);
  if (r.name === "post") {
    if (!current) mountView(lastView);
    if (!postById(r.id)) {
      if (S()) { go("#/" + lastView); }
      return;
    }
    openEditor(r.id);
  } else {
    closeEditor();
    lastView = r.name;
    mountView(r.name, r.arg);
  }
  paintNav();
}

window.addEventListener("hashchange", async () => {
  if (suppress) { suppress = false; return; }
  const next = parse(location.hash);
  if (editorDirty() && !(next.name === "post" && next.id === editorId())) {
    const ok = await confirmModal({ title: "Discard unsaved changes?", message: "You've edited this short but haven't saved.", confirmLabel: "Discard changes", danger: true, cancelLabel: "Keep editing" });
    if (!ok) { suppress = true; location.hash = accepted; return; }
  }
  accepted = location.hash;
  route();
});

// ── sidebar ──
function paintNav() {
  for (const a of $$("#nav a")) a.classList.toggle("on", a.dataset.route === lastView);
}
function paintSide() {
  const s = S(); if (!s) return;
  const att = s.counts?.attention ?? posts().filter((p) => p.bucket === "attention").length;
  const badge = $("#badgeLibrary");
  badge.textContent = att ? String(att) : ""; badge.hidden = !att;
  const rows = ORDER.map((pl) => {
    const p = s.platforms[pl];
    const cls = p.route === "dryrun" ? "grey" : p.ready ? "green" : "amber";
    const word = p.route === "dryrun" ? "Dry run" : p.ready ? ROUTE_LABEL[p.route] : "Needs setup";
    return h("div", { class: "ss-row", title: p.detail }, h("span", { class: `sdot ${cls}` }), h("span", { class: "ss-name" }, PLAT[pl].name), h("span", { class: "ss-state" }, word));
  });
  const sc = s.scheduler || {};
  const running = sc.in_process || sc.launchd_installed;
  $("#sideStatus").replaceChildren(...rows,
    h("div", { class: "ss-sched", title: sc.last_result || "" }, h("span", { class: `sdot ${running ? "green" : "grey"}` }),
      h("span", null, `Scheduler: ${running ? "running" : "stopped"} · last run ${relTime(sc.last_run)}`)));
}

// ── banners ──
function paintBanner() {
  const s = S(), el = $("#banner");
  const kids = [];
  if (store.offline) kids.push(h("div", { class: "banner bad" }, "Can't reach the dashboard server — retrying…"));
  if (s && ORDER.every((pl) => s.platforms[pl].route === "dryrun")) {
    kids.push(h("div", { class: "banner warn" }, icon("info", 16), h("span", null, h("strong", null, "Dry-run mode"), " — nothing will actually post. "), h("a", { href: "#/settings" }, "Connect platforms →")));
  }
  el.replaceChildren(...kids);
}

// ── caption progress strip ──
let batchTotal = 0;
function paintProgress() {
  const q = S()?.caption_queue, el = $("#progress");
  const left = (q?.running ? 1 : 0) + (q?.queued?.length || 0);
  if (!left) { batchTotal = 0; el.replaceChildren(); return; }
  batchTotal = Math.max(batchTotal, left);
  const done = batchTotal - left;
  const now = q.running ? postById(q.running) : null;
  el.replaceChildren(h("div", { class: "progress" }, icon("sparkle", 16),
    h("span", null, `Writing captions: ${Math.min(done + 1, batchTotal)} of ${batchTotal}`, now ? ` · now: ${titleOf(now)}` : ""),
    h("div", { class: "bar" }, h("i", { style: { width: `${(done / batchTotal) * 100}%` } }))));
}

function onUpdate() {
  paintSide(); paintBanner(); paintProgress();
  if (!store.state) return;
  if (!booted) { booted = true; accepted = location.hash; route(); return; }
  current?.update?.();
  updateEditor();
}

// ── boot ──
function boot() {
  for (const el of $$("[data-icon]")) el.replaceChildren(icon(el.dataset.icon, 20));
  initEditor({ closeNav: () => go("#/" + lastView) });
  const toggle = $("#navToggle");
  const setCollapsed = (c) => { document.body.classList.toggle("nav-collapsed", c); pref.set("navCollapsed", c ? "1" : "0"); };
  const saved = pref.get("navCollapsed");
  setCollapsed(saved == null ? window.innerWidth < 1000 : saved === "1");
  toggle.addEventListener("click", () => setCollapsed(!document.body.classList.contains("nav-collapsed")));
  onState(onUpdate);
  startPolling();
}
boot();
