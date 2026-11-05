"""Measure preview performance on a (large) Markdown file in the real WebView.

    python3 tests/profile_preview.py FILE

Reports the time of a full render, of a re-render after a one-character edit
(including the layout it forces), and of the scroll-sync queries, plus the
time the Python side spends per edit (outline, word count).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
tmp = Path(tempfile.mkdtemp(prefix="gme-prof-"))
os.environ["XDG_CONFIG_HOME"] = str(tmp / "config")
os.environ["XDG_DATA_HOME"] = str(tmp / "data")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib  # noqa: E402

from gnome_markdown_editor import markdown_utils as mu  # noqa: E402
from gnome_markdown_editor.application import Application  # noqa: E402

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
middle = len(text) // 2
edited = text[:middle] + "x" + text[middle:]
nlines = text.count("\n")

PROBE = """
(async () => {
  const T = {};
  const css = %(css)s;
  if (css) { const st = document.createElement('style'); st.textContent = css; document.head.appendChild(st); document.documentElement.scrollHeight; }
  const layout = () => document.documentElement.scrollHeight;
  let t = performance.now(); GME.render(%(edited)s); layout(); T.editRender = performance.now() - t;
  t = performance.now(); GME.render(%(edited2)s); T.renderOnly = performance.now() - t;
  t = performance.now(); layout(); T.layoutAfter = performance.now() - t;
  t = performance.now();
  for (let i = 0; i < 20; i++) GME.scrollToLine(%(n)d * i / 20);
  T.scrollToLine = (performance.now() - t) / 20;
  t = performance.now();
  for (let i = 0; i < 20; i++) GME.lineForTop(i * 5000);
  T.lineForTop = (performance.now() - t) / 20;
  T.nodes = document.getElementsByTagName('*').length;
  T.diagrams = document.querySelectorAll('.mermaid svg').length + '/' + document.querySelectorAll('.mermaid').length;
  return JSON.stringify(T);
})()
"""


def main() -> int:
    app = Application()
    app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    Application.do_activate = lambda self: None
    out: dict = {}

    def start(_app):
        win = app.new_window()
        win.present()
        t0 = time.monotonic()
        page = win.open_file(Gio.File.new_for_path(str(path)))

        def wait_ready():
            if page.props.loading or page.preview is None or not page.preview._ready:
                return True
            page.preview.connect("rendered", first_render)
            return False

        def first_render(*_):
            if "first" in out:
                return
            out["first"] = time.monotonic() - t0
            GLib.timeout_add(3000, probe)

        def probe():
            code = PROBE % {
                "edited": json.dumps(edited),
                "edited2": json.dumps(edited[:middle] + "y" + edited[middle:]),
                "n": nlines,
                "css": json.dumps(os.environ.get("PROF_CSS", "")),
            }

            def done(view, res):
                value = view.call_async_javascript_function_finish(res)
                out.update(json.loads(value.to_string()))
                t = time.monotonic()
                mu.parse_headings(edited)
                out["py_outline_ms"] = (time.monotonic() - t) * 1000
                t = time.monotonic()
                mu.word_count(edited)
                out["py_words_ms"] = (time.monotonic() - t) * 1000
                t = time.monotonic()
                win.outline.set_headings(mu.parse_headings(text))
                win.outline.set_headings(mu.parse_headings(edited.replace("\n", "\n\n", 1)))
                out["py_outline_widget_ms"] = (time.monotonic() - t) * 1000
                for p in win.pages():
                    p.buffer.set_modified(False)
                win._closing_confirmed = True
                win.close()

            page.preview.webview.call_async_javascript_function(
                f"return await {code.strip()};", -1, None, None, None, None, done
            )
            return False

        GLib.timeout_add(50, wait_ready)

    app.connect("activate", start)
    app.run([])
    shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps({"file": path.name, "lines": nlines, **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in out.items()}}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
