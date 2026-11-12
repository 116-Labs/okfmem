// Runs webui/inbox_review.html's own script against a live review server, on a
// minimal DOM, clicks Done and prints what the page then shows as JSON.
// Usage: node webui_page.cjs <inbox_review.html> <base url> <token>
"use strict";
const fs = require("fs");
const vm = require("vm");

const [htmlPath, base, token] = process.argv.slice(2);
if (typeof fetch !== "function") {
  process.stderr.write("this node has no fetch\n");
  process.exit(3);
}
const html = fs.readFileSync(htmlPath, "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
const markup = html.slice(html.indexOf("<body>"), html.indexOf("<script>"));

class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.attrs = {};
    this.kids = [];
    this.className = "";
    this.id = "";
    this.disabled = false;
    this.checked = false;
  }
  setAttribute(k, v) {
    if (k === "class") this.className = String(v);
    else if (k === "id") this.id = String(v);
    else this.attrs[k] = String(v);
  }
  append(...xs) { this.kids.push(...xs); }
  replaceChildren(...xs) { this.kids = xs; }
  get textContent() { return this.kids.map(k => typeof k === "string" ? k : k.textContent).join(""); }
  set textContent(v) { this.kids = [String(v)]; }
  *walk() {
    yield this;
    for (const k of this.kids) if (k instanceof El) yield* k.walk();
  }
}

const root = new El("body");
for (const m of markup.matchAll(/<(\w+)([^>]*)>/g)) {
  const attrs = Object.fromEntries([...m[2].matchAll(/(\w+)="([^"]*)"/g)].map(a => [a[1], a[2]]));
  if (!attrs.id) continue;
  const e = new El(m[1]);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  root.append(e);
}

function matches(e, sel) {
  const [tag, cls] = sel.trim().split(".");
  return e.tagName === tag.toUpperCase() && (!cls || e.className.split(" ").includes(cls));
}
const document = {
  createElement: tag => new El(tag),
  getElementById: id => [...root.walk()].find(e => e.id === id) || null,
  querySelectorAll: sel => [...root.walk()].filter(e => sel.split(",").some(s => matches(e, s))),
};
const ctx = vm.createContext({
  document, window: {scrollTo() {}}, location: {search: "?t=" + encodeURIComponent(token)},
  fetch: (path, opts) => fetch(base + path, opts), URLSearchParams,
  setTimeout, clearTimeout, setInterval, clearInterval, console,
});
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  vm.runInContext(script, ctx);
  for (let i = 0; i < 250 && !vm.runInContext("S !== null", ctx); i++) await sleep(20);
  if (!vm.runInContext("S !== null", ctx)) throw new Error("the page never loaded /api/state");
  const doneButtons = document.querySelectorAll("button.done").length;
  await document.getElementById("done").onclick();
  const banner = document.getElementById("banner");
  const sections = [];
  for (let i = 1; i + 1 < banner.kids.length; i += 2) {
    sections.push([banner.kids[i].textContent, banner.kids[i + 1].kids.map(li => li.textContent)]);
  }
  const controls = document.querySelectorAll("button, input, textarea, select");
  process.stdout.write(JSON.stringify({
    banner: {kind: banner.className, text: banner.kids[0], sections},
    done_buttons: doneButtons,
    controls: controls.length,
    enabled: controls.filter(e => !e.disabled).map(e => e.id || e.textContent),
  }));
  process.exit(0);
})().catch(e => {
  process.stderr.write(String(e && e.stack || e) + "\n");
  process.exit(1);
});
