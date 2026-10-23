// activity.js — reverse-chronological event list.
import { h, platIcon, icon, S, postById, titleOf, openPost, relTime, fmtSlot } from "./core.js";

export function createActivity() {
  const root = h("div", { class: "view activity" });
  const list = h("div", { class: "events" });
  root.append(h("div", { class: "view-head" }, h("div", null, h("h1", null, "Activity"), h("div", { class: "sub" }, "The latest things the scheduler and you have done."))), list);
  let lastSig = "";
  function render() {
    const ev = [...(S()?.events || [])].sort((a, b) => Date.parse(b.at) - Date.parse(a.at) || b.id - a.id);
    const sig = JSON.stringify(ev.map((e) => e.id));
    if (sig === lastSig) return;
    lastSig = sig;
    if (!ev.length) {
      list.replaceChildren(h("div", { class: "empty" }, h("div", { class: "empty-ico" }, icon("activity", 28)), h("h3", null, "Nothing has happened yet"), h("p", null, "Imports, captions, approvals and posts will show up here.")));
      return;
    }
    list.replaceChildren(...ev.map((e) => {
      const p = e.post_id ? postById(e.post_id) : null;
      return h("div", { class: `event lvl-${e.level}` },
        h("span", { class: "ev-bar" }),
        h("div", { class: "ev-plat" }, e.platform ? platIcon(e.platform, 16) : icon("info", 16)),
        h("div", { class: "ev-main" }, h("div", { class: "ev-msg" }, e.message),
          h("div", { class: "ev-sub faint sm" }, h("span", { title: fmtSlot(e.at, " at ") }, relTime(e.at)), p ? [" · ", h("a", { class: "link", href: `#/post/${p.id}` }, titleOf(p))] : (e.post_id ? " · (removed short)" : ""))));
    }));
  }
  render();
  return { el: root, update: render, destroy() {} };
}
