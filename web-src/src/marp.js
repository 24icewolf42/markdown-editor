// Marp slide-deck rendering, loaded on demand by the preview renderer for
// documents whose front matter says `marp: true`.
//
// Exposes `window.GMEMarp.render(markdown, highlight)`, which returns the
// deck as `{ html, css }`: one inline <svg data-marpit-svg> per slide and the
// theme stylesheet scoped to the deck container. Each slide's <section> gets
// `data-line` (its first source line) for scroll sync.

import { Marp } from "@marp-team/marp-core";
import { polyfills } from "@marp-team/marpit-svg-polyfill";

let marp = null;
let highlighter = null;

function instance() {
  if (marp) return marp;
  marp = new Marp({
    html: true, // the preview sanitises the result with DOMPurify
    inlineSVG: true,
    script: false, // no runtime scripts: the deck is static markup
    math: { lib: "katex", katexFontPath: false }, // fonts come from the preview's KaTeX stylesheet
    emoji: { shortcode: true, unicode: false }, // no twemoji images from a CDN
    minifyCSS: false,
  });
  // Code blocks use the preview's highlight.js (with its extra languages).
  marp.highlighter = (code, lang) => (highlighter ? highlighter(code, lang) : "");
  marp.markdown.core.ruler.after("marpit_slide", "gme_slide_lines", (state) => {
    for (const t of state.tokens) {
      if (t.type === "marpit_slide_open" && t.map) t.attrSet("data-line", String(t.map[0]));
    }
  });
  return marp;
}

// Custom themes (`/* @theme name */` stylesheets) found next to the deck.
// A theme added earlier stays registered: the deck's `theme:` picks one.
const added = new Set();
function useThemes(list) {
  const m = instance();
  for (const css of list) {
    if (added.has(css)) continue;
    added.add(css);
    try {
      m.themeSet.add(css);
    } catch (e) {
      console.warn("Marp theme not added:", e.message);
    }
  }
}

// WebKit does not scale the HTML inside <foreignObject> with the SVG's
// viewBox; Marp's polyfill detects that and scales each slide's <section>.
let fixes = null;

window.GMEMarp = {
  /** Re-apply the WebKit scaling fix to the slides below `root`. */
  fit(root) {
    if (!fixes) fixes = Promise.resolve(polyfills()).catch(() => []);
    return fixes.then((list) => {
      for (const fix of list) fix({ target: root });
    });
  },

  render(markdown, highlight, themes) {
    highlighter = highlight;
    useThemes(themes || []);
    // MathJax is not bundled: `math: mathjax` decks render with KaTeX.
    // Only directives change: the front matter and HTML comments.
    const katex = (s) => s.replace(/^([ \t]*(?:<!--[ \t]*)?_?math[ \t]*:[ \t]*["']?)mathjax\b/gm, "$1katex");
    const source = markdown
      .replace(/^---[ \t]*\r?\n[\s\S]*?\r?\n---[ \t]*(?=\r?\n|$)/, katex)
      .replace(/<!--[\s\S]*?-->/g, katex);
    const { html, css } = instance().render(source);
    // `is="marp-pre"` etc. name Marp's auto-scaling custom elements, which
    // need its runtime script; the elements render fine as plain ones.
    return { html: html.replace(/\sis="marp-[a-z0-9-]+"/g, ""), css };
  },
};
