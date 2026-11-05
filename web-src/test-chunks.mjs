// Regression tests for section-by-section rendering: the chunked preview must
// render exactly what a whole-document render does.  node test-chunks.mjs
import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";

const bundle = readFileSync(new URL("../gnome_markdown_editor/web/vendor/renderer.js", import.meta.url), "utf8");
const dom = new JSDOM(`<!doctype html><body><div id="content"></div>`, { runScripts: "outside-only" });
dom.window.eval(bundle);
const { GME, document } = dom.window;

function normalise(root) {
  for (const el of root.querySelectorAll("[data-line],[data-task-line],[data-ready]")) {
    for (const a of ["data-line", "data-task-line", "data-ready"]) el.removeAttribute(a);
  }
  for (const el of root.querySelectorAll(".carousel-controls")) el.remove();
  for (const sec of [...root.querySelectorAll("section.md-chunk")]) sec.replaceWith(...sec.childNodes);
  return root.innerHTML.replace(/>\s+</g, "><").trim();
}
function chunked(src) {
  GME.render(src);
  return normalise(document.getElementById("content").cloneNode(true));
}
function whole(src) {
  const d = document.createElement("div");
  d.innerHTML = GME.renderMarkdown(src).html;
  return normalise(d);
}
const ids = () => [...document.querySelectorAll("#content h1,#content h2")].map((h) => h.id).join(",");

let failed = 0;
function check(name, ok, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failed++;
}

const same = {
  "inline footnotes across sections": "# A\n\nx^[one]\n\n# B\n\ny^[two]\n",
  "indented footnote definition": "# A\n\n- x[^1]\n\n  [^1]: note\n\n# B\n\ntext\n",
  "reference definition in another section": "# A\n\nsee [x]\n\n# B\n\n[x]: http://a.example\n",
  "reference definition inside a fence is code": "# A\n\nsee [x]\n\n# B\n\n```\n[x]: http://evil.example\n```\n",
  "paragraph continuation is not a definition": "# A\n\nsee [x]\n\n# B\n\ntext\n[x]: http://a.example\n",
  "definition with destination on next line": "# A\n\nsee [x]\n\n# B\n\n[x]:\n  http://a.example\n",
  "indented closing fence does not close": "# A\n\n```\n    ```\n# comment\n```\n\n# B\n",
  "display math with a # line": "# A\n\n$$\na\n# b\n$$\n\n# C\n",
  "raw html block with a # line": "# A\n\n<table>\n# x\n</table>\n\n# B\n",
  "details spanning a heading": "# A\n\n<details>\n<summary>s</summary>\n\n# Inside\n\ntext\n\n</details>\n\n# B\n",
  "comment spanning a heading": "# A\n\n<!--\n# hidden\n-->\n\n# B\n",
  "list then heading": "# A\n\n- one\n- two\n# B\n\n1. x\n",
  "front matter": "---\ntitle: T\n---\n# A\n\ntext\n\n# A\n",
  "definition after a comment": "# A\n\nsee [x]\n\n# B\n\n<!-- c -->\n[x]: http://a.example\n",
  "definition after a setext heading": "# A\n\nsee [x]\n\n# B\n\nTitle\n=====\n[x]: http://a.example\n",
  "definition after a thematic break": "# A\n\nsee [x]\n\n# B\n\n---\n[x]: http://a.example\n",
  "definition after indented code": "# A\n\nsee [x]\n\n# B\n\n    code\n[x]: http://a.example\n",
  "definition in a blockquote": "# A\n\nsee [x]\n\n# B\n\n> [x]: http://a.example\n",
  "definition title on the next line": "# A\n\nsee [x]\n\n# B\n\n[x]: http://a.example\n  \"T\"\n",
  "list continuation is not indented code": "# A\n\nsee [x]\n\n# B\n\n- item\n\n    more text\n[x]: http://a.example\n",
  "nested item then lazy line": "# A\n\nsee [x]\n\n# B\n\n- a\n\n    - b\n[x]: http://a.example\n",
  "line-initial autolink": "# A\n\n<https://a.example>\n# B\n\ntext\n",
};
for (const [name, src] of Object.entries(same)) {
  const a = whole(src);
  const b = chunked(src);
  check(`same output: ${name}`, a === b, a === b ? "" : `\n  whole:   ${a}\n  chunked: ${b}`);
}

GME.render("# Intro\n\nA\n\n# Intro\n\nB\n");
check("duplicate ids numbered", ids() === "intro,intro-1", ids());
GME.render("# Intro\n\nB\n");
check("ids renumber after deleting a section", ids() === "intro", ids());
GME.render("# Intro\n\nA\n\n# Intro\n\nB\n");
GME.render("# Intro\n\nB\n\n# Intro\n\nA\n");
check("ids follow document order after reordering", ids() === "intro,intro-1", ids());

const sections = (src) => {
  GME.render(src);
  return document.getElementById("content").children.length;
};
check("'^[' in code keeps sections", sections("# A\n\n`^[a-z]`\n\n```\n^[0-9]+\n```\n\n# B\n\nx\n") === 2);
check("real footnote renders as one section", sections("# A\n\nx[^1]\n\n# B\n\n[^1]: n\n") === 1);
check("autolink line does not block splitting", sections("# A\n\n<https://a.example>\n# B\n") === 2);

const fold = (src) => {
  GME.render(src);
  return document.querySelectorAll("#content details.changelog").length;
};
check("changelog folds", fold("## Changelog\n\n- one\n- two\n") === 1);
check("revision history table folds", fold("## 17. Revision History\n\n| Version | Date |\n| - | - |\n| 0.1 | 2026-01-01 |\n") === 1);
check("dated 'Changes' list folds", fold("## Changes\n\n- **v1.2** fix\n- **v1.1** feature\n") === 1);
check("plain 'Changes' list does not fold", fold("## Changes\n\n- rename x\n- remove y\n") === 0);
check("'Supported Versions' table does not fold", fold("## Supported Versions\n\n| Version | Supported |\n| - | - |\n| 5.1.x | yes |\n| 5.0.x | no |\n") === 0);
check("prose after changelog stays outside", (GME.render("## Changelog\n\n- a\n\nProse.\n"), document.querySelector("details.changelog").textContent.includes("Prose")) === false);

console.log(failed ? `${failed} failed` : "all passed");
process.exit(failed ? 1 : 0);
