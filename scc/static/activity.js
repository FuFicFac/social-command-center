// activity.js — reverse-chronological event list.
import { h, platIcon, icon, S, postById, titleOf, openPost, relTime, fmtSlot, PLAT } from "./core.js";

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
      const plats = (e.platform || "").split(",").filter((x) => PLAT[x]);
      // Which account this happened on, with the short's brand.
      const owner = plats.length === 1 ? (S()?.accounts || []).find((a) => a.platform === plats[0] && a.label === e.account) : null;
      const brand = owner?.brand_name || (owner ? "" : p?.brand_name) || "";
      const where = e.account ? [plats.length === 1 ? PLAT[plats[0]].name + " " : "", e.account, brand ? ` · ${brand}` : ""].join("") : "";
      return h("div", { class: `event lvl-${e.level}` },
        h("span", { class: "ev-bar" }),
        h("div", { class: "ev-plat" }, plats.length ? plats.map((x) => platIcon(x, 16)) : icon("info", 16)),
        h("div", { class: "ev-main" }, h("div", { class: "ev-msg" }, e.message),
          h("div", { class: "ev-sub faint sm" }, where ? [h("span", { class: "ev-acct" }, where), " · "] : null, h("span", { title: fmtSlot(e.at, " at ") }, relTime(e.at)), p ? [" · ", h("a", { class: "link", href: `#/post/${p.id}` }, titleOf(p))] : (e.post_id ? " · (removed short)" : ""))));
    }));
  }
  render();
  return { el: root, update: render, destroy() {} };
}
