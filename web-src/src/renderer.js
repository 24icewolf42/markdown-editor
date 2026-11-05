// Preview renderer for gnome-markdown-editor.
//
// Runs inside the WebKitGTK preview. The host (Python) calls the functions on
// `window.GME` through `evaluate_javascript`, and this side talks back through
// the `gme` script message handler. Markdown is rendered with markdown-it plus
// the extensions used by GitHub, Hugo/goldmark and the
// Antigravity artifact format (carousels, alerts, mermaid).

import MarkdownIt from "markdown-it";
import footnote from "markdown-it-footnote";
import attrs from "markdown-it-attrs";
import mark from "markdown-it-mark";
import sub from "markdown-it-sub";
import sup from "markdown-it-sup";
import deflist from "markdown-it-deflist";
import abbr from "markdown-it-abbr";
import ins from "markdown-it-ins";
import dollarmath from "markdown-it-dollarmath";
import alerts from "markdown-it-github-alerts";
import GithubSlugger, { slug as githubSlug } from "github-slugger";
import * as yaml from "js-yaml";
import katex from "katex";
import hljs from "highlight.js";
import DOMPurify from "dompurify";
import { registerExtraLanguages } from "./languages.js";

registerExtraLanguages(hljs);

const state = {
  theme: "light",
  mermaidUrl: null,
  mermaidLoading: null,
  mermaidCache: new Map(), // key -> {svg} | {error}
  mermaidSeq: 0,
  renderSeq: 0,
  suppressScrollReport: 0,
  lastSource: null,
  options: { math: true, syncScroll: true, collapseChangelogs: true },
  entries: null, // cached scroll-sync geometry
  openChangelogs: new Set(), // changelog sections the reader expanded
  diagramQueue: [],
  diagramsRunning: false,
};

// --------------------------------------------------------------------------
// Host bridge

function post(msg) {
  try {
    window.webkit.messageHandlers.gme.postMessage(JSON.stringify(msg));
  } catch (e) {
    // Not running inside the app (e.g. tests in a plain browser).
  }
}

// --------------------------------------------------------------------------
// Helpers

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function hashString(s) {
  // FNV-1a, 32 bit, as hex. Good enough to key a cache.
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16);
}

function inlineText(tokens) {
  let out = "";
  for (const t of tokens || []) {
    if (t.type === "text" || t.type === "code_inline" || t.type === "math_inline") out += t.content;
    else if (t.children) out += inlineText(t.children);
  }
  return out;
}

// --------------------------------------------------------------------------
// Front matter (YAML `---` or TOML `+++`), stripped before parsing. The lines
// are blanked rather than removed so source line numbers stay aligned.

function splitFrontMatter(src) {
  const m = /^(---|\+\+\+)[ \t]*\r?\n([\s\S]*?)\r?\n\1[ \t]*(?:\r?\n|$)/.exec(src);
  if (!m) return { body: src, frontMatter: null };
  const block = m[0];
  const lines = block.split("\n").length - (block.endsWith("\n") ? 1 : 0);
  const body = "\n".repeat(lines) + src.slice(block.length);
  return { body, frontMatter: { kind: m[1] === "---" ? "yaml" : "toml", raw: m[2] } };
}

function renderFrontMatter(fm) {
  let rows = null;
  if (fm.kind === "yaml") {
    try {
      const data = yaml.load(fm.raw);
      if (data && typeof data === "object" && !Array.isArray(data)) {
        rows = Object.entries(data).map(([k, v]) => {
          let value;
          if (v === null || v === undefined) value = "";
          else if (Array.isArray(v) && v.every((x) => typeof x !== "object"))
            value = v.map((x) => `<span class="fm-chip">${escapeHtml(String(x))}</span>`).join(" ");
          else if (typeof v === "object")
            value = `<code>${escapeHtml(v instanceof Date ? v.toISOString().slice(0, 10) : JSON.stringify(v))}</code>`;
          else value = escapeHtml(String(v));
          return `<tr><th>${escapeHtml(k)}</th><td>${value}</td></tr>`;
        });
      }
    } catch (e) {
      rows = null;
    }
  }
  const body = rows
    ? `<table class="fm-table">${rows.join("")}</table>`
    : `<pre class="fm-raw"><code>${escapeHtml(fm.raw)}</code></pre>`;
  return `<details class="front-matter" data-line="0"><summary>Front matter <span class="fm-kind">${fm.kind.toUpperCase()}</span></summary>${body}</details>`;
}

// --------------------------------------------------------------------------
// markdown-it setup

function highlight(code, lang) {
  if (lang && hljs.getLanguage(lang)) {
    try {
      return hljs.highlight(code, { language: lang, ignoreIllegals: true }).value;
    } catch (e) {
      /* fall through */
    }
  }
  return escapeHtml(code);
}

const md = new MarkdownIt({
  html: true,
  linkify: true,
  typographer: false,
  breaks: false,
});

// Bare domains ("example.io") are not links on GitHub or in goldmark.
md.linkify.set({ fuzzyLink: false });
// markdown-it rejects file: and data: URLs; local documents link to each
// other with file:// URLs, and DOMPurify below is the safety net.
md.validateLink = (url) => !/^\s*(?:javascript|vbscript):/i.test(url) && !/^\s*data:(?!image\/(?:png|gif|jpe?g|webp|svg\+xml);)/i.test(url);

md.use(footnote)
  .use(deflist)
  .use(abbr)
  .use(mark)
  .use(ins)
  .use(sub)
  .use(sup)
  .use(alerts, { markers: "*" })
  .use(attrs, { leftDelimiter: "{", rightDelimiter: "}", allowedAttributes: ["id", "class", /^data-.*$/, "width", "height", "title"] });

md.use(dollarmath, {
  allow_space: false,
  allow_digits: false,
  double_inline: true,
  allow_labels: true,
  renderer(content, { displayMode }) {
    try {
      return katex.renderToString(content, { displayMode, throwOnError: false, strict: false, output: "htmlAndMathml" });
    } catch (e) {
      return `<span class="math-error" title="${escapeHtml(e.message)}">${escapeHtml(content)}</span>`;
    }
  },
});

// Hugo shortcodes ({{< name args >}} / {{% name %}}) are shown as badges: the
// preview has no Hugo site to execute them against.
md.inline.ruler.before("text", "hugo_shortcode", (st, silent) => {
  if (st.src.charCodeAt(st.pos) !== 0x7b /* { */ || st.src.charCodeAt(st.pos + 1) !== 0x7b) return false;
  const m = /^\{\{([<%])\s*(\/?)([\w./-]+)([\s\S]*?)\s*([>%])\}\}/.exec(st.src.slice(st.pos));
  if (!m) return false;
  if (!silent) {
    const t = st.push("hugo_shortcode", "", 0);
    t.meta = { closing: m[2] === "/", name: m[3], args: m[4].trim() };
  }
  st.pos += m[0].length;
  return true;
});
// '{' is a terminator character of markdown-it's text rule, so the rule
// above gets a chance at every '{{'.
md.renderer.rules.hugo_shortcode = (tokens, idx) => {
  const { closing, name, args } = tokens[idx].meta;
  return `<span class="shortcode" title="Hugo shortcode — rendered by the site build, not by this preview">${closing ? "/" : ""}${escapeHtml(name)}${args ? ` <span class="shortcode-args">${escapeHtml(args)}</span>` : ""}</span>`;
};

// Task lists: `- [ ]` / `- [x]`. The checkbox carries its source line so a
// click can toggle the box in the editor.
md.core.ruler.after("inline", "task_lists", (st) => {
  const tokens = st.tokens;
  for (let i = 2; i < tokens.length; i++) {
    const t = tokens[i];
    if (t.type !== "inline") continue;
    if (tokens[i - 1].type !== "paragraph_open" || tokens[i - 2].type !== "list_item_open") continue;
    const m = /^\[([ xX])\][ \t]/.exec(t.content) || (/^\[([ xX])\]$/.exec(t.content));
    if (!m) continue;
    const first = t.children[0];
    if (!first || first.type !== "text" || !/^\[[ xX]\]/.test(first.content)) continue;
    const checked = m[1] !== " ";
    first.content = first.content.slice(3).replace(/^[ \t]/, "");
    const line = (tokens[i - 2].map ? tokens[i - 2].map[0] : 0) + (st.env.lineOffset || 0);
    const cb = new st.Token("html_inline", "", 0);
    cb.content = `<input type="checkbox" class="task-list-item-checkbox" data-task-line="${line}"${checked ? " checked" : ""}> `;
    t.children.unshift(cb);
    const li = tokens[i - 2];
    li.attrJoin("class", "task-list-item");
    // Mark the enclosing list.
    for (let j = i - 3; j >= 0; j--) {
      if ((tokens[j].type === "bullet_list_open" || tokens[j].type === "ordered_list_open") && tokens[j].level === li.level - 1) {
        if (!(tokens[j].attrGet("class") || "").includes("contains-task-list")) tokens[j].attrJoin("class", "contains-task-list");
        break;
      }
    }
  }
});

// Heading ids (GitHub / Hugo "github" style) unless set explicitly via {#id}.
// Chunks render independently, so the base slug goes into data-slug and
// dedupeHeadingIds() numbers duplicates across the whole document.
md.core.ruler.push("heading_ids", (st) => {
  const tokens = st.tokens;
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (t.type !== "heading_open") continue;
    let id = t.attrGet("id");
    if (!id) {
      id = githubSlug(inlineText(tokens[i + 1].children));
      t.attrSet("id", id);
      t.attrSet("data-slug", id);
    }
    t.meta = { id };
  }
});

// Source line attributes for scroll sync.
md.core.ruler.push("source_lines", (st) => {
  const off = st.env.lineOffset || 0;
  for (const t of st.tokens) {
    if (!t.map || t.nesting === -1) continue;
    if (t.level === 0 || (t.nesting === 1 && /^(list_item|tr|blockquote|alert|dt|dd)/.test(t.type))) {
      t.attrSet("data-line", String(t.map[0] + off));
    }
  }
});

// Changelog sections fold into a closed <details>. A section counts as a
// changelog when its heading says so ("Changelog", "Revision History", ...)
// or when it says "changes"/"history"/"release" and its entries start with
// versions or dates. Only the lists and tables directly after the heading
// fold; prose after them (e.g. conformance boilerplate) stays visible.
const CHANGELOG_TITLE = /^(?:[\d.]+\s+)?(?:change\s*-?\s*log|revision\s+history|version\s+history|document\s+history|release\s+notes|release\s+history|history\s+of\s+changes)$/i;
// Weaker titles ("Changes", "Release 2.0", "History") fold only when most
// entries start with a version or a date.
const CHANGELOG_HINT = /change|history|revision|release/i;
const ENTRY_RE = /^\s*(?:\*\*|__|\[)?\s*(?:v(?:ersion)?\s*)?\d+(?:\.\d+)+|^\s*(?:\*\*|__)?\d{4}-\d{2}-\d{2}/i;

function blockEnd(tokens, i) {
  // Index of the closing token of the block opened at i.
  let depth = 0;
  for (let j = i; j < tokens.length; j++) {
    depth += tokens[j].nesting;
    if (depth === 0) return j;
  }
  return tokens.length - 1;
}

md.core.ruler.push("changelogs", (st) => {
  if (!state.options.collapseChangelogs) return;
  const tokens = st.tokens;
  for (let i = 0; i < tokens.length; i++) {
    const h = tokens[i];
    if (h.type !== "heading_open" || h.level !== 0) continue;
    const title = inlineText(tokens[i + 1].children).trim();
    const strong = CHANGELOG_TITLE.test(title);
    if (!strong && !CHANGELOG_HINT.test(title)) continue;
    let j = i + 3; // after heading_close
    const blocks = [];
    let entries = 0;
    let dated = 0;
    while (j < tokens.length && /^(bullet_list|ordered_list|table)_open$/.test(tokens[j].type)) {
      const end = blockEnd(tokens, j);
      for (let k = j; k < end; k++) {
        const t = tokens[k];
        const isEntry = (t.type === "list_item_open" && t.level === 1) || (t.type === "tr_open" && tokens[k - 1].type !== "thead_open");
        if (!isEntry) continue;
        entries++;
        const inline = tokens.slice(k, Math.min(end, k + 6)).find((x) => x.type === "inline");
        if (inline && ENTRY_RE.test(inline.content)) dated++;
      }
      blocks.push([j, end]);
      j = end + 1;
    }
    if (!blocks.length || !entries) continue;
    if (!strong && (entries < 2 || dated / entries < 0.6)) continue;
    const id = h.attrGet("id") || "";
    const open = new st.Token("html_block", "", 0);
    open.content = `<details class="changelog" data-changelog="${escapeHtml(id)}"><summary>`;
    const mid = new st.Token("html_block", "", 0);
    mid.content = `<span class="changelog-count">${entries} ${entries === 1 ? "entry" : "entries"}</span></summary>\n`;
    const close = new st.Token("html_block", "", 0);
    close.content = "</details>\n";
    const last = blocks[blocks.length - 1][1];
    tokens.splice(last + 1, 0, close);
    tokens.splice(i + 3, 0, mid);
    tokens.splice(i, 0, open);
    i = last + 3;
  }
});

const defaultHeadingClose = md.renderer.rules.heading_close || ((tokens, idx, opts, env, self) => self.renderToken(tokens, idx, opts));
md.renderer.rules.heading_close = (tokens, idx, opts, env, self) => {
  const open = tokens[idx - 2];
  const id = open && open.meta && open.meta.id;
  const anchor = id ? `<a class="heading-anchor" href="#${escapeHtml(id)}" aria-hidden="true">#</a>` : "";
  return anchor + defaultHeadingClose(tokens, idx, opts, env, self);
};

// Tables get a scroll wrapper so wide spec tables do not blow out the layout.
md.renderer.rules.table_open = (tokens, idx, opts, env, self) => `<div class="table-wrapper">${self.renderToken(tokens, idx, opts)}`;
md.renderer.rules.table_close = (tokens, idx, opts, env, self) => `${self.renderToken(tokens, idx, opts)}</div>`;

// Fenced code: mermaid, carousel, math, and syntax-highlighted code.
function fenceInfo(info) {
  const raw = (info || "").trim();
  // Also accept `{.mermaid ...}` (goldmark attribute fences).
  const attr = /^\{\s*\.([\w-]+)([^}]*)\}$/.exec(raw);
  if (attr) return { lang: attr[1].toLowerCase(), rest: attr[2].trim() };
  const m = /^([^\s{]+)?\s*(.*)$/.exec(raw);
  return { lang: (m[1] || "").toLowerCase(), rest: (m[2] || "").trim() };
}

md.renderer.rules.fence = (tokens, idx, opts, env) => {
  const t = tokens[idx];
  const { lang, rest } = fenceInfo(t.info);
  const line = t.attrGet("data-line");
  const lineAttr = line !== null ? ` data-line="${line}"` : "";
  const content = t.content;

  if (lang === "mermaid") {
    const key = hashString(content);
    env.mermaid = (env.mermaid || 0) + 1;
    return `<div class="mermaid-block"${lineAttr}><div class="mermaid" data-mermaid-key="${key}"></div><pre class="mermaid-source" hidden>${escapeHtml(content)}</pre></div>\n`;
  }

  if (lang === "carousel") {
    const slides = content.split(/^[ \t]*<!--\s*slide\s*-->[ \t]*$/m);
    const startLine = t.map ? t.map[0] + 1 + (env.lineOffset || 0) : 0;
    let offset = 0;
    const rendered = slides.map((s, i) => {
      const sub = md.render(s, { ...env, lineOffset: startLine + offset, nested: true });
      offset += s.split("\n").length - 1;
      return `<div class="carousel-slide" data-index="${i}">${sub}</div>`;
    });
    const dots = slides.map((_, i) => `<button class="carousel-dot" data-index="${i}" aria-label="Slide ${i + 1}"></button>`).join("");
    return `<div class="carousel"${lineAttr}><div class="carousel-track">${rendered.join("")}</div><div class="carousel-controls"><button class="carousel-prev" aria-label="Previous">‹</button><span class="carousel-dots">${dots}</span><span class="carousel-count">1 / ${slides.length}</span><button class="carousel-next" aria-label="Next">›</button></div></div>\n`;
  }

  if (lang === "math" || lang === "latex" && rest === "math" || lang === "katex") {
    let html;
    try {
      html = katex.renderToString(content, { displayMode: true, throwOnError: false, strict: false, output: "htmlAndMathml" });
    } catch (e) {
      html = `<pre>${escapeHtml(content)}</pre>`;
    }
    return `<div class="math-block"${lineAttr}>${html}</div>\n`;
  }

  const code = highlight(content, lang);
  const label = lang ? `<span class="code-lang">${escapeHtml(lang)}</span>` : "";
  return `<div class="code-block"${lineAttr}>${label}<button class="code-copy" title="Copy">Copy</button><pre><code class="hljs${lang ? ` language-${escapeHtml(lang)}` : ""}">${code}</code></pre></div>\n`;
};

md.renderer.rules.code_block = (tokens, idx) => {
  const t = tokens[idx];
  const line = t.attrGet("data-line");
  return `<div class="code-block"${line !== null ? ` data-line="${line}"` : ""}><pre><code class="hljs">${escapeHtml(t.content)}</code></pre></div>\n`;
};

// --------------------------------------------------------------------------
// Sanitising. Documents are local files, but a hostile Markdown file must not
// be able to run script in a WebView that can read file:// URLs.

const URI_RE = /^(?:(?:https?|file|mailto|tel|ftp|data):|[^a-z]|[a-z+.-]+(?:[^a-z+.\-:]|$))/i;

DOMPurify.addHook("uponSanitizeElement", (node, data) => {
  if (data.tagName === "iframe" || data.tagName === "video" || data.tagName === "audio") {
    const src = node.getAttribute && node.getAttribute("src");
    if (src && node.parentNode) {
      const a = node.ownerDocument.createElement("a");
      a.className = "embed-placeholder";
      a.setAttribute("href", src);
      a.textContent = `Embedded ${data.tagName}: ${src}`;
      node.parentNode.replaceChild(a, node);
    }
  }
});
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A" && node.getAttribute("href") && !node.getAttribute("href").startsWith("#")) {
    node.setAttribute("rel", "noopener");
  }
});

function sanitize(html) {
  return DOMPurify.sanitize(html, {
    ALLOWED_URI_REGEXP: URI_RE,
    ADD_TAGS: ["details", "summary", "semantics", "annotation", "mrow"],
    ADD_ATTR: ["data-line", "data-task-line", "data-mermaid-key", "data-index", "data-slug", "data-changelog", "open", "align", "aria-hidden", "encoding"],
    RETURN_DOM_FRAGMENT: true,
    FORBID_TAGS: ["style", "form", "button-x"],
    FORBID_ATTR: ["srcdoc", "formaction"],
    ALLOW_UNKNOWN_PROTOCOLS: false,
  });
}

// --------------------------------------------------------------------------
// Mermaid (loaded on demand: the bundle is large). Diagrams render lazily as
// they approach the viewport, one at a time with yields in between, so a
// document with dozens of diagrams stays scrollable while they appear.

function loadMermaid() {
  if (window.mermaid) return Promise.resolve(window.mermaid);
  if (state.mermaidLoading) return state.mermaidLoading;
  state.mermaidLoading = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = state.mermaidUrl;
    s.onload = () => {
      initMermaid();
      resolve(window.mermaid);
    };
    s.onerror = () => reject(new Error("Could not load the Mermaid library"));
    document.head.appendChild(s);
  });
  return state.mermaidLoading;
}

function initMermaid() {
  window.mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    theme: state.theme === "dark" ? "dark" : "default",
    fontFamily: "inherit",
    flowchart: { htmlLabels: true },
    suppressErrorRendering: true,
  });
}

function mermaidCacheKey(key) {
  return `${state.theme}:${key}`;
}

function applyMermaid(el, result, ck) {
  el.dataset.renderedKey = ck;
  if (result.svg) {
    el.classList.remove("mermaid-error");
    el.innerHTML = result.svg;
  } else {
    el.classList.add("mermaid-error");
    el.innerHTML = `<div class="mermaid-error-title">Mermaid diagram could not be rendered</div><pre>${escapeHtml(result.error)}</pre>`;
  }
  invalidateGeometry();
}

// Without IntersectionObserver (tests under jsdom) every diagram queues at once.
const IO = window.IntersectionObserver || class {
  constructor(cb) {
    this.cb = cb;
  }
  observe(el) {
    this.cb([{ isIntersecting: true, target: el }]);
  }
  unobserve() {}
};
const diagramObserver = new IO(
  (records) => {
    for (const r of records) {
      if (!r.isIntersecting) continue;
      diagramObserver.unobserve(r.target);
      enqueueDiagram(r.target);
    }
  },
  { rootMargin: "1500px 0px" },
);

// Fill diagrams under `root` from the cache; watch the rest.
function refreshDiagrams(root) {
  for (const el of root.querySelectorAll(".mermaid[data-mermaid-key]")) {
    const ck = mermaidCacheKey(el.dataset.mermaidKey);
    if (el.dataset.renderedKey === ck) continue;
    const hit = state.mermaidCache.get(ck);
    if (hit) applyMermaid(el, hit, ck);
    else diagramObserver.observe(el);
  }
}

function yieldToEventLoop() {
  return new Promise((resolve) => {
    const ch = new MessageChannel();
    ch.port1.onmessage = () => resolve();
    ch.port2.postMessage(0);
  });
}

function enqueueDiagram(el) {
  if (!state.diagramQueue.includes(el)) state.diagramQueue.push(el);
  runDiagramQueue();
}

async function runDiagramQueue() {
  if (state.diagramsRunning) return;
  state.diagramsRunning = true;
  let mermaid = null;
  let drawn = 0;
  try {
    while (state.diagramQueue.length) {
      const el = state.diagramQueue.shift();
      const ck = mermaidCacheKey(el.dataset.mermaidKey);
      if (!el.isConnected || el.dataset.renderedKey === ck) continue;
      let result = state.mermaidCache.get(ck);
      if (!result) {
        if (!mermaid) {
          try {
            mermaid = await loadMermaid();
          } catch (e) {
            applyMermaid(el, { error: e.message }, ck);
            continue;
          }
        }
        let src = el.parentElement.querySelector(".mermaid-source").textContent;
        // Light-only themes are unreadable on the dark preview; swap them.
        if (state.theme === "dark") src = src.replace(/(["']theme["']\s*:\s*["'])(neutral|default|forest)(["'])/, "$1dark$3");
        const id = `mermaid-svg-${++state.mermaidSeq}`;
        try {
          result = { svg: (await mermaid.render(id, src)).svg };
        } catch (e) {
          result = { error: String((e && (e.message || e.str)) || e) };
          document.getElementById(id)?.remove();
          document.getElementById("d" + id)?.remove();
        }
        state.mermaidCache.set(ck, result);
        if (state.mermaidCache.size > 600) state.mermaidCache.delete(state.mermaidCache.keys().next().value);
      }
      for (const target of document.querySelectorAll(`.mermaid[data-mermaid-key="${el.dataset.mermaidKey}"]`)) {
        if (target.dataset.renderedKey !== ck) {
          applyMermaid(target, result, ck);
          drawn++;
        }
      }
      // Let input and scrolling through between diagrams. A MessageChannel
      // round trip yields to the event loop without timer clamping, which
      // throttles setTimeout to 1 s in pages that are not on screen.
      await yieldToEventLoop();
    }
  } finally {
    state.diagramsRunning = false;
  }
  post({ type: "rendered-async", drawn });
  if (state.allDiagramsRequested) {
    state.allDiagramsRequested = false;
    post({ type: "all-diagrams" });
  }
}

// Render every diagram now (printing, export). Posts all-diagrams when done.
function renderAllDiagrams() {
  const content = document.getElementById("content");
  refreshDiagrams(content);
  const pending = [...content.querySelectorAll(".mermaid[data-mermaid-key]")].filter(
    (el) => el.dataset.renderedKey !== mermaidCacheKey(el.dataset.mermaidKey),
  );
  for (const el of pending) {
    diagramObserver.unobserve(el);
    if (!state.diagramQueue.includes(el)) state.diagramQueue.push(el);
  }
  if (state.diagramQueue.length || state.diagramsRunning) {
    state.allDiagramsRequested = true;
    runDiagramQueue();
  } else post({ type: "all-diagrams" });
}

// --------------------------------------------------------------------------
// Rendering
//
// The source is split into chunks at top-level ATX headings. Each chunk is
// rendered, sanitised and turned into DOM on its own, and a chunk whose text
// did not change keeps its existing DOM (with rendered diagrams, open
// <details>, carousel position). Line numbers inside a chunk are relative to
// its first line, held in data-base, so an edit above a chunk does not change
// the chunk itself. Editing a 2 MB document re-renders one section.

const RAW_HTML_OPEN = /^\s{0,3}<(script|pre|style|textarea)(?:\s|>|$)/i;
const HTML_BLOCK_START = /^ {0,3}<\/?[a-zA-Z][a-zA-Z0-9-]*(?:\s|\/?>|$)/;
// Optional blockquote markers, then a definition label.
const REF_DEF_LINE = /^((?: {0,3}>[ \t]?)*) {0,3}\[(?!\^)[^\]]+\]:/;
const BLOCK_ENDERS = /^ {0,3}(?:(?:=+|-+)[ \t]*$|([-*_])(?:[ \t]*\1){2,}[ \t]*$|<!--)/;
const FOOTNOTE_SYNTAX = /\[\^[^\]\s]+\]:|\^\[/;

// Splits the body into chunks at top-level ATX headings, never inside a
// fence, display math, comment, raw HTML block or open <details>/<div>.
// Also collects link reference definitions (outside code, at the start of
// a block), which apply to the whole document.
function splitChunks(body) {
  const lines = body.split("\n");
  const chunks = [];
  const defs = [];
  let start = 0;
  let fence = null; // { mark, indent }
  let math = false;
  let comment = false;
  let rawTag = null;
  let htmlBlock = false; // ends at a blank line
  let depth = 0; // open <details>/<div> elements spanning blank lines
  let paragraph = false; // previous line continues a paragraph
  let footnotes = false; // footnote syntax outside code
  let inList = false; // inside a list item (indented lines continue it)
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const blank = !line.trim();
    if (fence) {
      const m = /^(\s*)(`{3,}|~{3,})\s*$/.exec(line);
      if (m && m[2][0] === fence.mark[0] && m[2].length >= fence.mark.length && m[1].length <= fence.indent + 3) fence = null;
      continue;
    }
    if (math) {
      if ((line.match(/\$\$/g) || []).length % 2 === 1) math = false;
      continue;
    }
    if (comment) {
      if (line.includes("-->")) comment = false;
      continue;
    }
    if (rawTag) {
      if (line.toLowerCase().includes(`</${rawTag}`)) rawTag = null;
      continue;
    }
    if (htmlBlock) {
      if (blank) htmlBlock = false;
      else {
        depth = Math.max(0, depth + htmlDepth(line));
        continue;
      }
    }
    const heading = /^#{1,6}(?:[ \t]|$)/.test(line);
    if (i > start && depth === 0 && heading) {
      chunks.push({ start, text: lines.slice(start, i).join("\n") });
      start = i;
    }
    const f = /^(\s*)(`{3,}|~{3,})(.*)$/.exec(line);
    if (f && !(f[2][0] === "`" && f[3].includes("`"))) {
      fence = { mark: f[2], indent: f[1].length };
      paragraph = false;
      continue;
    }
    if (/^\s*\$\$/.test(line) && (line.match(/\$\$/g) || []).length % 2 === 1) {
      math = true;
      paragraph = false;
      continue;
    }
    if (!footnotes && line.includes("^") && FOOTNOTE_SYNTAX.test(line.replace(/(`+)[^`]*?\1/g, ""))) footnotes = true;
    const d = paragraph ? null : REF_DEF_LINE.exec(line);
    if (d) {
      // Strip blockquote markers: definitions apply document-wide.
      const strip = (l) => l.replace(/^(?: {0,3}>[ \t]?)*/, "");
      defs.push(strip(line));
      // Destination and/or title on the following lines.
      let k = i + 1;
      if (/\]:\s*$/.test(line) && k < lines.length) defs.push(strip(lines[k++]));
      if (k < lines.length && /^\s+["'(]/.test(strip(lines[k]))) defs.push(strip(lines[k]));
      defs.push("");
      continue;
    }
    const c = line.lastIndexOf("<!--");
    if (c >= 0 && line.indexOf("-->", c + 4) < 0) comment = true;
    const r = RAW_HTML_OPEN.exec(line);
    if (r && !line.toLowerCase().includes(`</${r[1].toLowerCase()}`)) rawTag = r[1].toLowerCase();
    else if (!paragraph && HTML_BLOCK_START.test(line)) htmlBlock = true;
    if (line.includes("<")) depth = Math.max(0, depth + htmlDepth(line));
    // Setext underlines, thematic breaks, comments and indented code end a
    // block, so a definition may follow them.
    // (Indented lines inside a list are item content, not code.)
    const indented = /^(?: {4}|\t)/.test(line);
    if (/^\s{0,3}(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)/.test(line)) inList = true;
    else if (!blank && !indented) inList = false;
    const indentedCode = !paragraph && !inList && indented;
    paragraph = !blank && !heading && !htmlBlock && !comment && !indentedCode && !BLOCK_ENDERS.test(line);
  }
  chunks.push({ start, text: lines.slice(start).join("\n") });
  return { chunks, defs: defs.join("\n"), footnotes };
}

function htmlDepth(line) {
  return (line.match(/<(details|div)[\s>]/gi) || []).length - (line.match(/<\/(details|div)>/gi) || []).length;
}

// Link reference definitions are document-wide; collected by splitChunks
// and parsed once so a chunk can use a definition from another chunk.
let refsCache = { sig: null, references: {} };

function documentReferences(sig) {
  if (sig !== refsCache.sig) {
    const env = {};
    if (sig) md.parse(sig, env);
    refsCache = { sig, references: env.references || {} };
  }
  return refsCache;
}

function renderChunkElement(text, env) {
  const el = document.createElement("section");
  el.className = "md-chunk";
  let frag;
  try {
    frag = sanitize(md.render(text, env));
  } catch (e) {
    frag = sanitize(`<div class="render-error"><strong>Rendering failed:</strong> ${escapeHtml(e.message)}</div>`);
  }
  el.appendChild(frag);
  for (const d of el.querySelectorAll("details.changelog")) {
    if (state.openChangelogs.has(d.dataset.changelog)) d.open = true;
  }
  return el;
}

function dedupeHeadingIds(content) {
  const slugger = new GithubSlugger();
  for (const h of content.querySelectorAll("h1, h2, h3, h4, h5, h6")) {
    const base = h.dataset.slug;
    if (base === undefined) {
      if (h.id) slugger.slug(h.id);
      continue;
    }
    const id = slugger.slug(base);
    if (h.id !== id) {
      h.id = id;
      const a = h.querySelector(".heading-anchor");
      if (a) a.setAttribute("href", `#${id}`);
      const d = h.closest("details.changelog");
      if (d) d.dataset.changelog = id;
    }
  }
}

// Plain HTML of a whole document (tests, tooling); not used by the preview.
function renderMarkdown(src) {
  const { body, frontMatter } = splitFrontMatter(src);
  const env = { lineOffset: 0 };
  let html = md.render(body, env);
  if (frontMatter) html = renderFrontMatter(frontMatter) + html;
  const div = document.createElement("div");
  div.appendChild(sanitize(html));
  dedupeHeadingIds(div);
  return { html: div.innerHTML, env };
}

function render(src) {
  state.renderSeq++;
  state.lastSource = src;
  const content = document.getElementById("content");
  const { body, frontMatter } = splitFrontMatter(src);
  // Footnotes and abbreviations are numbered/resolved document-wide by their
  // plugins: such documents render as one chunk.
  // (footnote syntax outside code, or abbreviation definitions).
  let split = splitChunks(body);
  if (split.footnotes || /^\s*\*\[[^\]]+\]:/m.test(body)) split = { chunks: [{ start: 0, text: body }], defs: "" };
  const chunks = split.chunks;
  const refs = documentReferences(split.defs);
  const prefix = `${state.options.collapseChangelogs ? 1 : 0}\u0000${refs.sig}\u0000`;
  if (frontMatter) {
    // The body's first chunk starts with the blanked front-matter lines;
    // for scroll sync it begins where the body text does.
    chunks[0].geomLine = /^\n*/.exec(body)[0].length;
    chunks.unshift({ start: 0, text: null, fm: frontMatter });
  }

  const old = new Map();
  for (const el of content.children) {
    const list = old.get(el._gmeKey);
    if (list) list.push(el);
    else old.set(el._gmeKey, [el]);
  }
  let cursor = content.firstElementChild;
  let created = 0;
  for (const chunk of chunks) {
    const key = chunk.fm ? `fm\u0000${chunk.fm.kind}\u0000${chunk.fm.raw}` : prefix + chunk.text;
    const reuse = old.get(key);
    let el = reuse && reuse.length ? reuse.shift() : null;
    if (!el) {
      if (chunk.fm) {
        el = document.createElement("section");
        el.className = "md-chunk";
        el.appendChild(sanitize(renderFrontMatter(chunk.fm)));
      } else {
        el = renderChunkElement(chunk.text, { references: { ...refs.references }, lineOffset: 0 });
      }
      el._gmeKey = key;
      created++;
    }
    const base = String(chunk.start);
    if (el.dataset.base !== base) el.dataset.base = base;
    const geom = chunk.geomLine === undefined ? undefined : String(chunk.geomLine);
    if (el.dataset.geomLine !== geom) {
      if (geom === undefined) delete el.dataset.geomLine;
      else el.dataset.geomLine = geom;
    }
    if (el === cursor) cursor = cursor.nextElementSibling;
    else content.insertBefore(el, cursor);
  }
  // Placed chunks all sit before `cursor`; what remains is stale.
  while (cursor) {
    const next = cursor.nextElementSibling;
    cursor.remove();
    cursor = next;
  }
  // Ids depend on the order of all headings: deleting or moving a section
  // renumbers duplicates even when no section was re-rendered.
  dedupeHeadingIds(content);
  if (created) setupCarousels();
  primeContentVisibility(content, created, chunks.length);
  refreshDiagrams(content);
  invalidateGeometry();
  document.body.classList.toggle("empty", !src.trim());
  post({ type: "rendered", created, chunks: chunks.length });
  if (!state.diagramQueue.length && !state.diagramsRunning) post({ type: "rendered-async" });
}

// Long documents let the engine skip layout of off-screen sections
// (content-visibility: auto). Sections are laid out once with it off, so
// each one records its real height (contain-intrinsic-size: auto) and the
// scrollbar and scroll sync stay accurate; then skipping is switched on.
let primeTimer = 0;
function primeContentVisibility(content, created, total) {
  if (total < 30) {
    content.classList.remove("cv");
    return;
  }
  // Re-measure when many sections are new (replace-all, big paste): new
  // off-screen sections would otherwise sit at a placeholder height.
  if (created < 20 && created < total / 2 && content.classList.contains("cv")) return;
  content.classList.remove("cv");
  void content.offsetHeight; // full layout once
  clearTimeout(primeTimer);
  const enable = () => {
    clearTimeout(primeTimer);
    content.classList.add("cv");
    invalidateGeometry();
  };
  // Sizes are recorded at the next rendering update; rAF may never fire for
  // a page that is not on screen, hence the timer fallback.
  const raf = window.requestAnimationFrame || ((f) => setTimeout(f, 16));
  raf(() => raf(enable));
  primeTimer = setTimeout(enable, 1000);
}

// Apply an edit to the last rendered source: the host sends only the changed
// range instead of the whole document.
function renderPatch(start, removed, inserted, newLength) {
  const src = state.lastSource;
  if (src === null || start + removed > src.length || src.length - removed + inserted.length !== newLength) {
    post({ type: "resync" });
    return;
  }
  render(src.slice(0, start) + inserted + src.slice(start + removed));
}

// --------------------------------------------------------------------------
// Carousels

function setupCarousels() {
  for (const c of document.querySelectorAll(".carousel")) {
    if (c.dataset.ready) {
      updateCarousel(c);
      continue;
    }
    c.dataset.ready = "1";
    const track = c.querySelector(".carousel-track");
    c.addEventListener("click", (ev) => {
      const b = ev.target.closest("button");
      if (!b || !c.contains(b)) return;
      ev.preventDefault();
      const n = track.children.length;
      let i = carouselIndex(c);
      if (b.classList.contains("carousel-prev")) i = (i - 1 + n) % n;
      else if (b.classList.contains("carousel-next")) i = (i + 1) % n;
      else if (b.classList.contains("carousel-dot")) i = Number(b.dataset.index);
      else return;
      track.scrollTo({ left: track.children[i].offsetLeft - track.offsetLeft, behavior: "smooth" });
    });
    track.addEventListener("scroll", () => updateCarousel(c), { passive: true });
    updateCarousel(c);
  }
}

function carouselIndex(c) {
  const track = c.querySelector(".carousel-track");
  return Math.round(track.scrollLeft / Math.max(1, track.clientWidth));
}

function updateCarousel(c) {
  const track = c.querySelector(".carousel-track");
  const i = carouselIndex(c);
  c.querySelectorAll(".carousel-dot").forEach((d, j) => d.classList.toggle("active", i === j));
  const count = c.querySelector(".carousel-count");
  if (count) count.textContent = `${i + 1} / ${track.children.length}`;
}

// --------------------------------------------------------------------------
// Scroll sync

function absoluteLine(el) {
  const chunk = el.closest(".md-chunk");
  return Number(el.dataset.line) + (chunk ? Number(chunk.dataset.base) || 0 : 0);
}

function invalidateGeometry() {
  state.geometryVersion = (state.geometryVersion || 0) + 1;
  state.entries = null;
}

// Scroll-sync geometry, in two levels so off-screen sections (which
// content-visibility leaves unlaid-out) are never forced into layout: the
// chunk list gives each section's top and first line; the elements inside a
// section are measured only for the section being scrolled to.
function chunkEntries() {
  if (state.entries) return state.entries;
  const out = [];
  const y0 = window.scrollY;
  for (const el of document.getElementById("content").children) {
    const r = el.getBoundingClientRect();
    if (r.height === 0 && r.width === 0) continue;
    const line = Number(el.dataset.geomLine ?? el.dataset.base) || 0;
    out.push({ el, line, base: Number(el.dataset.base) || 0, top: r.top + y0, bottom: r.bottom + y0 });
  }
  state.entries = out;
  return out;
}

function innerEntries(chunk) {
  if (chunk.el._geom && chunk.el._geomVersion === state.geometryVersion) return chunk.el._geom;
  const out = [{ line: chunk.line, top: chunk.top }];
  const y0 = window.scrollY;
  for (const el of chunk.el.querySelectorAll("[data-line]")) {
    if (el.closest(".carousel-slide")) continue;
    const line = Number(el.dataset.line) + chunk.base;
    if (Number.isNaN(line)) continue;
    const r = el.getBoundingClientRect();
    if (r.height === 0 && r.width === 0) continue;
    const last = out[out.length - 1];
    if (line > last.line && r.top + y0 >= last.top) out.push({ line, top: r.top + y0 });
  }
  chunk.el._geom = out;
  chunk.el._geomVersion = state.geometryVersion;
  return out;
}

// Index of the last entry whose `key` is <= value (binary search), or -1.
function lastAtOrBefore(es, key, value) {
  let lo = 0;
  let hi = es.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (es[mid][key] <= value) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

function interpolate(es, i, fromKey, toKey, value, end) {
  const prev = es[i];
  const next = es[i + 1] || end;
  if (!next || next[fromKey] === prev[fromKey]) return prev[toKey];
  const frac = Math.min(1, Math.max(0, (value - prev[fromKey]) / (next[fromKey] - prev[fromKey])));
  return prev[toKey] + frac * (next[toKey] - prev[toKey]);
}

function topForLine(line) {
  const chunks = chunkEntries();
  const c = lastAtOrBefore(chunks, "line", line);
  if (c < 0) return 0;
  const chunk = chunks[c];
  const next = chunks[c + 1];
  const es = innerEntries(chunk);
  const i = lastAtOrBefore(es, "line", line);
  return interpolate(es, Math.max(0, i), "line", "top", line, next ? { line: next.line, top: next.top } : { line: line + 1, top: chunk.bottom });
}

function lineForTop(y) {
  const chunks = chunkEntries();
  const c = lastAtOrBefore(chunks, "top", y);
  if (c < 0) return 0;
  const chunk = chunks[c];
  const next = chunks[c + 1];
  const es = innerEntries(chunk);
  const i = lastAtOrBefore(es, "top", y);
  return interpolate(es, Math.max(0, i), "top", "line", y, next ? { line: next.line, top: next.top } : null);
}

if (window.ResizeObserver) new ResizeObserver(invalidateGeometry).observe(document.getElementById("content"));
window.addEventListener("resize", invalidateGeometry);
document.addEventListener("load", invalidateGeometry, true); // images
document.addEventListener(
  "toggle",
  (ev) => {
    const d = ev.target;
    if (d.classList && d.classList.contains("changelog")) {
      if (d.open) state.openChangelogs.add(d.dataset.changelog);
      else state.openChangelogs.delete(d.dataset.changelog);
    }
    invalidateGeometry();
  },
  true,
);

// Printing shows changelogs expanded; the reader's state is restored after.
let printOpened = [];
window.addEventListener("beforeprint", () => {
  printOpened = [...document.querySelectorAll("details.changelog:not([open])")];
  for (const d of printOpened) d.open = true;
});
window.addEventListener("afterprint", () => {
  for (const d of printOpened) d.open = false;
  printOpened = [];
});

function scrollToLine(line) {
  const y = Math.max(0, topForLine(line) - 12);
  if (Math.abs(window.scrollY - y) < 2) return;
  state.suppressScrollReport = performance.now() + 250;
  window.scrollTo({ top: y, behavior: "instant" });
}

let scrollQueued = false;
window.addEventListener(
  "scroll",
  () => {
    if (scrollQueued) return;
    scrollQueued = true;
    // setTimeout rather than requestAnimationFrame: rAF stalls while the
    // view is not being painted.
    setTimeout(() => {
      scrollQueued = false;
      if (performance.now() < state.suppressScrollReport) return;
      const atEnd = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2;
      post({ type: "scroll", line: lineForTop(window.scrollY + 12), atEnd });
    }, 16);
  },
  { passive: true },
);

function scrollToAnchor(id) {
  const el = document.getElementById(id) || document.getElementsByName(id)[0];
  if (!el) return false;
  // Open folded sections around the target (its own summary counts too).
  for (let d = el.closest("details"); d; d = d.parentElement && d.parentElement.closest("details")) {
    if (!d.open && !d.querySelector(":scope > summary")?.contains(el)) d.open = true;
  }
  el.scrollIntoView({ block: "start" });
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1200);
  return true;
}

// --------------------------------------------------------------------------
// Interaction

document.addEventListener("click", (ev) => {
  const cb = ev.target.closest("input.task-list-item-checkbox");
  if (cb) {
    const chunk = cb.closest(".md-chunk");
    post({ type: "toggle-task", line: Number(cb.dataset.taskLine) + (chunk ? Number(chunk.dataset.base) || 0 : 0) });
    return;
  }
  const copy = ev.target.closest(".code-copy");
  if (copy) {
    ev.preventDefault();
    const code = copy.parentElement.querySelector("code");
    post({ type: "copy", text: code ? code.textContent : "" });
    copy.textContent = "Copied";
    setTimeout(() => (copy.textContent = "Copy"), 1200);
    return;
  }
  const a = ev.target.closest("a[href]");
  if (a) {
    ev.preventDefault();
    const href = a.getAttribute("href");
    if (href.startsWith("#")) {
      scrollToAnchor(decodeURIComponent(href.slice(1)));
      post({ type: "anchor", id: decodeURIComponent(href.slice(1)) });
    } else {
      post({ type: "open-link", href: a.href, raw: href });
    }
    return;
  }
  const zoomable = ev.target.closest("#content img, #content .mermaid svg");
  if (zoomable && !ev.target.closest(".lightbox")) {
    openLightbox(zoomable);
  }
});

document.addEventListener("dblclick", (ev) => {
  const el = ev.target.closest("[data-line]");
  if (el && !ev.target.closest(".carousel-slide")) post({ type: "reveal", line: absoluteLine(el) });
});

function openLightbox(el) {
  const box = document.createElement("div");
  box.className = "lightbox";
  const inner = el.tagName === "IMG" ? el.cloneNode() : el.closest(".mermaid").cloneNode(true);
  inner.removeAttribute("width");
  inner.removeAttribute("height");
  box.appendChild(inner);
  let scale = 1;
  box.addEventListener("wheel", (ev) => {
    if (!ev.ctrlKey) return;
    ev.preventDefault();
    scale = Math.min(8, Math.max(0.2, scale * (ev.deltaY < 0 ? 1.1 : 0.9)));
    inner.style.transform = `scale(${scale})`;
  });
  box.addEventListener("click", () => box.remove());
  document.addEventListener(
    "keydown",
    function esc(ev) {
      if (ev.key === "Escape") {
        box.remove();
        document.removeEventListener("keydown", esc);
      }
    },
  );
  document.body.appendChild(box);
}

// --------------------------------------------------------------------------
// Public API

function setTheme(theme) {
  if (theme === state.theme) return;
  state.theme = theme;
  document.documentElement.classList.remove("light", "dark");
  document.documentElement.classList.add(theme);
  if (window.mermaid) initMermaid();
  // Chunks are theme-independent; only diagrams need redrawing.
  refreshDiagrams(document.getElementById("content"));
  if (!state.diagramQueue.length && !state.diagramsRunning) post({ type: "rendered-async" });
}

function configure(opts) {
  opts = opts || {};
  const before = state.options.collapseChangelogs;
  Object.assign(state.options, opts);
  if (opts.mermaidUrl) state.mermaidUrl = opts.mermaidUrl;
  if (opts.theme) setTheme(opts.theme);
  if (opts.fontSize) document.documentElement.style.setProperty("--font-size", `${opts.fontSize}px`);
  if ("maxWidth" in opts) document.documentElement.classList.toggle("full-width", !opts.maxWidth);
  if (state.options.collapseChangelogs !== before && state.lastSource !== null) render(state.lastSource);
  invalidateGeometry();
}

function exportHtml() {
  // Self-contained body markup for HTML export (mermaid already rendered).
  return document.getElementById("content").innerHTML;
}

window.GME = {
  render,
  renderPatch,
  renderMarkdown,
  renderAllDiagrams,
  configure,
  setTheme,
  scrollToLine,
  scrollToAnchor,
  exportHtml,
  lineForTop,
  source: () => state.lastSource,
};
post({ type: "ready" });
