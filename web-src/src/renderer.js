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
import GithubSlugger from "github-slugger";
import * as yaml from "js-yaml";
import katex from "katex";
import hljs from "highlight.js";
import DOMPurify from "dompurify";
import morphdom from "morphdom";
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
  options: { math: true, syncScroll: true },
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
md.core.ruler.push("heading_ids", (st) => {
  const slugger = st.env.slugger || (st.env.slugger = new GithubSlugger());
  const tokens = st.tokens;
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (t.type !== "heading_open") continue;
    const text = inlineText(tokens[i + 1].children);
    let id = t.attrGet("id");
    if (id) slugger.slug(id); // reserve
    else {
      id = slugger.slug(text);
      t.attrSet("id", id);
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
    ADD_ATTR: ["data-line", "data-task-line", "data-mermaid-key", "data-index", "open", "align", "aria-hidden", "encoding"],
    FORBID_TAGS: ["style", "form", "button-x"],
    FORBID_ATTR: ["srcdoc", "formaction"],
    ALLOW_UNKNOWN_PROTOCOLS: false,
  });
}

// --------------------------------------------------------------------------
// Mermaid (loaded on demand: the bundle is large)

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

function fillMermaidFromCache(root) {
  const pending = [];
  for (const el of root.querySelectorAll(".mermaid[data-mermaid-key]")) {
    const ck = mermaidCacheKey(el.dataset.mermaidKey);
    if (el.dataset.renderedKey === ck) continue;
    const hit = state.mermaidCache.get(ck);
    if (hit) applyMermaid(el, hit, ck);
    else pending.push(el);
  }
  return pending;
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
}

// Resolves true once every diagram of render `seq` is in place, false if a
// newer render superseded it.
async function renderMermaid(seq) {
  const pending = fillMermaidFromCache(document.getElementById("content"));
  if (!pending.length) return true;
  let mermaid;
  try {
    mermaid = await loadMermaid();
  } catch (e) {
    for (const el of pending) applyMermaid(el, { error: e.message }, "");
    return true;
  }
  for (const el of pending) {
    if (seq !== state.renderSeq) return false; // superseded
    if (!el.isConnected) continue;
    let src = el.parentElement.querySelector(".mermaid-source").textContent;
    // Light-only themes are unreadable on the dark preview; swap them.
    if (state.theme === "dark") src = src.replace(/(["']theme["']\s*:\s*["'])(neutral|default|forest)(["'])/, "$1dark$3");
    const ck = mermaidCacheKey(el.dataset.mermaidKey);
    let result = state.mermaidCache.get(ck);
    if (!result) {
      const id = `mermaid-svg-${++state.mermaidSeq}`;
      try {
        const out = await mermaid.render(id, src);
        result = { svg: out.svg };
      } catch (e) {
        result = { error: String((e && (e.message || e.str)) || e) };
        document.getElementById(id)?.remove();
        document.getElementById("d" + id)?.remove();
      }
      state.mermaidCache.set(ck, result);
      if (state.mermaidCache.size > 400) state.mermaidCache.delete(state.mermaidCache.keys().next().value);
    }
    // Re-query: the element may have been replaced by a later morph.
    for (const target of document.querySelectorAll(`.mermaid[data-mermaid-key="${el.dataset.mermaidKey}"]`)) {
      if (target.dataset.renderedKey !== ck) applyMermaid(target, result, ck);
    }
  }
  return seq === state.renderSeq;
}

// --------------------------------------------------------------------------
// Rendering

function renderMarkdown(src) {
  const { body, frontMatter } = splitFrontMatter(src);
  const env = { slugger: new GithubSlugger(), lineOffset: 0 };
  let html = md.render(body, env);
  if (frontMatter) html = renderFrontMatter(frontMatter) + html;
  return { html: sanitize(html), env };
}

function render(src) {
  const seq = ++state.renderSeq;
  state.lastSource = src;
  let html;
  try {
    html = renderMarkdown(src).html;
  } catch (e) {
    html = `<div class="render-error"><strong>Rendering failed:</strong> ${escapeHtml(e.message)}</div><pre>${escapeHtml(src)}</pre>`;
  }
  const content = document.getElementById("content");
  const next = document.createElement("div");
  next.id = "content";
  next.innerHTML = html;
  fillMermaidFromCache(next);
  morphdom(content, next, {
    onBeforeElUpdated(fromEl, toEl) {
      if (fromEl.isEqualNode(toEl)) return false;
      if (fromEl.classList && fromEl.classList.contains("mermaid") && fromEl.dataset.renderedKey && fromEl.dataset.renderedKey === toEl.dataset.renderedKey) return false;
      if (fromEl.tagName === "DETAILS" && fromEl.hasAttribute("open") !== toEl.hasAttribute("open")) {
        if (fromEl.hasAttribute("open")) toEl.setAttribute("open", "");
        else toEl.removeAttribute("open");
      }
      return true;
    },
  });
  setupCarousels();
  renderMermaid(seq).then((done) => {
    if (done) post({ type: "rendered-async" });
  });
  document.body.classList.toggle("empty", !src.trim());
  post({ type: "rendered" });
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

function lineEntries() {
  const out = [];
  const y0 = window.scrollY;
  for (const el of document.querySelectorAll("#content [data-line]")) {
    if (el.closest(".carousel-slide")) continue;
    const line = Number(el.dataset.line);
    if (Number.isNaN(line)) continue;
    const r = el.getBoundingClientRect();
    if (r.height === 0 && r.width === 0) continue;
    out.push({ line, top: r.top + y0, bottom: r.bottom + y0 });
  }
  out.sort((a, b) => a.line - b.line || a.top - b.top);
  // Keep tops monotonic.
  const mono = [];
  for (const e of out) if (!mono.length || (e.line > mono[mono.length - 1].line && e.top >= mono[mono.length - 1].top)) mono.push(e);
  return mono;
}

function topForLine(line) {
  const es = lineEntries();
  if (!es.length) return 0;
  let prev = null;
  let next = null;
  for (const e of es) {
    if (e.line <= line) prev = e;
    else {
      next = e;
      break;
    }
  }
  if (!prev) return 0;
  if (!next) {
    // Inside the last block: interpolate within its own height.
    return prev.top;
  }
  const frac = (line - prev.line) / Math.max(1, next.line - prev.line);
  return prev.top + frac * (next.top - prev.top);
}

function lineForTop(y) {
  const es = lineEntries();
  if (!es.length) return 0;
  let prev = null;
  let next = null;
  for (const e of es) {
    if (e.top <= y) prev = e;
    else {
      next = e;
      break;
    }
  }
  if (!prev) return 0;
  if (!next) return prev.line;
  const frac = (y - prev.top) / Math.max(1, next.top - prev.top);
  return prev.line + frac * (next.line - prev.line);
}

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
    post({ type: "toggle-task", line: Number(cb.dataset.taskLine) });
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
  if (el && !ev.target.closest(".carousel-slide")) post({ type: "reveal", line: Number(el.dataset.line) });
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
  document.documentElement.className = theme;
  if (window.mermaid) initMermaid();
  if (state.lastSource !== null) render(state.lastSource);
}

function configure(opts) {
  Object.assign(state.options, opts || {});
  if (opts && opts.mermaidUrl) state.mermaidUrl = opts.mermaidUrl;
  if (opts && opts.theme) setTheme(opts.theme);
  if (opts && opts.fontSize) document.documentElement.style.setProperty("--font-size", `${opts.fontSize}px`);
  if (opts && "maxWidth" in opts) document.documentElement.classList.toggle("full-width", !opts.maxWidth);
}

function exportHtml() {
  // Self-contained body markup for HTML export (mermaid already rendered).
  return document.getElementById("content").innerHTML;
}

window.GME = { render, renderMarkdown, configure, setTheme, scrollToLine, scrollToAnchor, exportHtml, lineForTop };
post({ type: "ready" });
