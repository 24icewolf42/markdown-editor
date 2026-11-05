// Renders Markdown files through the built preview bundle inside jsdom and
// reports failures plus constructs worth eyeballing (math, shortcodes, ...).
// Usage: node test-corpus.mjs <file-or-dir>...
import { JSDOM } from "jsdom";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const bundle = readFileSync(join(here, "..", "gnome_markdown_editor", "web", "vendor", "renderer.js"), "utf8");
const dom = new JSDOM(`<!doctype html><html><body><div id="content"></div></body></html>`, { runScripts: "outside-only" });
dom.window.eval(bundle);
const GME = dom.window.GME;

const skip = new Set(["node_modules", ".git", "public", "out", "themes", "graphify-out", "resources"]);
function walk(p, acc) {
  const st = statSync(p);
  if (st.isDirectory()) {
    for (const e of readdirSync(p)) if (!skip.has(e)) walk(join(p, e), acc);
  } else if (p.endsWith(".md")) acc.push(p);
  return acc;
}
const files = process.argv.slice(2).flatMap((p) => walk(p, []));
let failures = 0, mathInline = [], mathBlocks = 0, alerts = 0, mermaid = 0, shortcodes = 0, footnotes = 0, tasks = 0, carousels = 0, fm = 0, mathErrors = [];
const t0 = Date.now();
for (const f of files) {
  const src = readFileSync(f, "utf8");
  try {
    const { html } = GME.renderMarkdown(src);
    const d = dom.window.document.createElement("div");
    d.innerHTML = html;
    for (const m of d.querySelectorAll("span.math.inline, .math.inline")) mathInline.push(`${f}: ${m.textContent.slice(0, 80)}`);
    mathBlocks += d.querySelectorAll(".math.block, .math-block").length;
    for (const e of d.querySelectorAll(".katex-error, .math-error")) mathErrors.push(`${f}: ${e.textContent.slice(0, 80)}`);
    alerts += d.querySelectorAll(".markdown-alert").length;
    mermaid += d.querySelectorAll(".mermaid").length;
    shortcodes += d.querySelectorAll(".shortcode").length;
    footnotes += d.querySelectorAll(".footnotes").length;
    tasks += d.querySelectorAll(".task-list-item-checkbox").length;
    carousels += d.querySelectorAll(".carousel").length;
    fm += d.querySelectorAll(".front-matter").length;
    if (/<script/i.test(html)) { console.log("SCRIPT SURVIVED SANITIZE:", f); failures++; }
  } catch (e) {
    failures++;
    console.log("FAIL", f, e.stack.split("\n").slice(0, 3).join(" | "));
  }
}
console.log(JSON.stringify({ files: files.length, ms: Date.now() - t0, failures, mathInline: mathInline.length, mathBlocks, mathErrors: mathErrors.length, alerts, mermaid, shortcodes, footnotes, tasks, carousels, frontMatter: fm }));
for (const m of mathInline.slice(0, 40)) console.log("  math:", m);
for (const m of mathErrors.slice(0, 10)) console.log("  math-error:", m);
