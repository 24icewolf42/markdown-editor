// Builds the preview assets into ../gnome_markdown_editor/web/vendor.
// The output is committed alongside the app, so running the editor needs no
// Node.js; only re-run this when updating the renderer or its dependencies.

import { build } from "esbuild";
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = join(here, "..", "gnome_markdown_editor", "web", "vendor");
const nm = join(here, "node_modules");

rmSync(out, { recursive: true, force: true });
mkdirSync(join(out, "katex"), { recursive: true });

await build({
  entryPoints: [join(here, "src", "renderer.js")],
  bundle: true,
  format: "iife",
  minify: true,
  target: ["safari16"],
  outfile: join(out, "renderer.js"),
  legalComments: "external",
  // markdown-it-dollarmath deep-imports a markdown-it 13 path; v14 renamed it.
  alias: { "markdown-it/lib/common/utils.js": join(nm, "markdown-it", "lib", "common", "utils.mjs") },
  logLevel: "info",
});

// Mermaid ships its own UMD bundle; it is loaded on demand.
cpSync(join(nm, "mermaid", "dist", "mermaid.min.js"), join(out, "mermaid.min.js"));

// KaTeX stylesheet and fonts (woff2 only: WebKitGTK supports it).
let katexCss = readFileSync(join(nm, "katex", "dist", "katex.min.css"), "utf8");
katexCss = katexCss.replace(/src:url\((fonts\/[^)]+\.woff2)\) format\("woff2"\)[^;}]*/g, 'src:url($1) format("woff2")');
writeFileSync(join(out, "katex", "katex.min.css"), katexCss);
mkdirSync(join(out, "katex", "fonts"), { recursive: true });
for (const f of readdirSync(join(nm, "katex", "dist", "fonts"))) {
  if (f.endsWith(".woff2")) cpSync(join(nm, "katex", "dist", "fonts", f), join(out, "katex", "fonts", f));
}

// highlight.js themes, scoped to the html.light / html.dark classes.
function scope(css, cls) {
  return css
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|})\s*([^{}@]+)\{/g, (m, brace, sel) =>
      `${brace}\n${sel
        .split(",")
        .map((s) => `html.${cls} ${s.trim()}`)
        .join(", ")} {`,
    );
}
const light = readFileSync(join(nm, "highlight.js", "styles", "github.css"), "utf8");
const dark = readFileSync(join(nm, "highlight.js", "styles", "github-dark.css"), "utf8");
writeFileSync(join(out, "highlight.css"), scope(light, "light") + "\n" + scope(dark, "dark") + "\n");

// Licence notices of everything bundled.
const pkgs = [
  "markdown-it", "markdown-it-footnote", "markdown-it-attrs", "markdown-it-mark", "markdown-it-sub",
  "markdown-it-sup", "markdown-it-deflist", "markdown-it-abbr", "markdown-it-ins", "markdown-it-dollarmath",
  "markdown-it-github-alerts", "github-slugger", "js-yaml", "katex", "highlight.js", "dompurify", "morphdom", "mermaid",
];
let notices = "Third-party components bundled in this directory\n\n";
for (const p of pkgs) {
  const pj = JSON.parse(readFileSync(join(nm, p, "package.json"), "utf8"));
  notices += `${p} ${pj.version} — ${typeof pj.license === "string" ? pj.license : JSON.stringify(pj.license)}\n`;
}
writeFileSync(join(out, "THIRD-PARTY-NOTICES.txt"), notices);
console.log("assets written to", out);
