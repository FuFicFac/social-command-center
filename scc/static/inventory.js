import {h, apiGet, apiPost, toast, errText} from "./core.js";

export function createInventory() {
  const el = h("section", {class: "page"});
  const query = h("input", {class: "input", type: "search", placeholder: "Find a title or filename", "aria-label": "Find a Short"});
  const list = h("div", {class: "inventory-list"});
  const count = h("p", {class: "hint"});
  let items = [];
  function paint() {
    const matches = items.filter(i => [i.title || "", ...i.paths].join(" ").toLowerCase().includes(query.value.toLowerCase()));
    count.textContent = matches.length + " of " + items.length + " distinct files";
    list.replaceChildren(...matches.map(i => {
      const filename = i.paths[0].split("/").at(-1);
      let status = i.post_id ? "In Command Center" : "YouTube history unverified";
      if (i.youtube === "published") status = "Previously public on YouTube";
      if (i.youtube === "private") status = "Uploaded to YouTube · Private";
      if (i.note) status = i.note;
      const button = h("button", {type: "button", class: "btn", disabled: !!i.phase}, i.phase ? "Preparation requested" : "Prepare for Instagram, Facebook and TikTok");
      button.onclick = async () => {
        button.disabled = true;
        try {
          await apiPost("/api/shorts-inventory/" + i.hash + "/prepare", {});
          button.textContent = "Preparation requested";
        } catch (e) { button.disabled = false; toast(errText(e), {kind:"error"}); }
      };
      return h("article", {class:"inventory-item"},
        h("strong", null, i.title || filename),
        h("span", {class:"hint"}, filename + " · " + Math.round(i.seconds) + "s" + (i.paths.length > 1 ? " · " + i.paths.length + " copies" : "")),
        i.url ? h("a",{class:"link",href:i.url,target:"_blank",rel:"noopener noreferrer"},status) : h("span", {class:"hint"}, status),
        i.post_id ? h("a", {class:"link", href:"#/post/"+i.post_id}, "Review platform drafts") : button);
    }));
  }
  query.oninput = paint;
  el.append(h("h1", null, "Shorts inventory"), query, count, list);
  apiGet("/api/shorts-inventory").then(r => {items=r.items;paint();}).catch(e => {count.textContent=errText(e);});
  return {el};
}
