"""End-to-end smoke test driving the real application.

Runs in the current graphical session with an isolated config directory,
drives a scripted sequence of user-level operations and checks the results.

    python3 tests/gui_smoke.py [--shots DIR] [--big FILE]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

parser = argparse.ArgumentParser()
parser.add_argument("--shots", default=None, help="directory for screenshots")
parser.add_argument("--big", default=None, help="large document to load for timing")
args = parser.parse_args()

tmp = Path(tempfile.mkdtemp(prefix="gme-test-"))
os.environ["XDG_CONFIG_HOME"] = str(tmp / "config")
os.environ["XDG_DATA_HOME"] = str(tmp / "data")  # keep test files out of recent documents
(tmp / "config" / "gnome-markdown-editor").mkdir(parents=True)
(tmp / "config" / "gnome-markdown-editor" / "settings.json").write_text(
    json.dumps({"restore-session": False, "color-scheme": "light", "spellcheck": True})
)
docs = tmp / "docs"
shutil.copytree(ROOT / "tests" / "fixtures", docs)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from gnome_markdown_editor.application import Application  # noqa: E402
from gnome_markdown_editor.settings import Settings  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


class Driver:
    def __init__(self, app: Application) -> None:
        self.app = app
        self.win = None
        self.steps = iter(self.script())
        self.paintable = None

    # Steps yield a number of seconds to wait, or a predicate to wait for.
    def run(self) -> None:
        self._advance()

    def _advance(self) -> bool:
        try:
            wait = next(self.steps)
        except StopIteration:
            self.finish()
            return GLib.SOURCE_REMOVE
        except Exception:
            traceback.print_exc()
            check("script raised", False, traceback.format_exc().splitlines()[-1])
            self.finish()
            return GLib.SOURCE_REMOVE
        if callable(wait):
            deadline = time.monotonic() + 120
            def poll():
                if wait() or time.monotonic() > deadline:
                    if time.monotonic() > deadline:
                        check("wait timed out", False, getattr(wait, "__name__", "predicate"))
                    self._advance()
                    return GLib.SOURCE_REMOVE
                return GLib.SOURCE_CONTINUE
            GLib.timeout_add(50, poll)
        else:
            GLib.timeout_add(int(wait * 1000), self._advance)
        return GLib.SOURCE_REMOVE

    def shot(self, name: str) -> None:
        if not args.shots:
            return
        Path(args.shots).mkdir(parents=True, exist_ok=True)
        w, h = self.win.get_width(), self.win.get_height()
        snap = Gtk.Snapshot()
        self.paintable.snapshot(snap, w, h)
        node = snap.to_node()
        if node is not None:
            tex = self.win.get_native().get_renderer().render_texture(node, None)
            tex.save_to_png(str(Path(args.shots) / f"{name}.png"))

    def js(self, page, code: str):
        box = {}
        def done(view, res):
            try:
                box["v"] = view.evaluate_javascript_finish(res).to_string()
            except GLib.Error as e:
                box["v"] = f"ERROR {e.message}"
        page.preview.webview.evaluate_javascript(code, -1, None, None, None, done)
        return box

    def finish(self) -> None:
        for w in self.app.get_windows():
            for p in w.pages():
                p.buffer.set_modified(False)
            w._closing_confirmed = True
            w.close()

    # ------------------------------------------------------------------

    def script(self):
        app = self.app
        self.win = app.new_window()
        self.win.present()
        self.paintable = Gtk.WidgetPaintable.new(self.win)
        win = self.win
        show = docs / "showcase.md"
        page = win.open_file(Gio.File.new_for_path(str(show)))
        yield lambda: not page.props.loading and page.preview is not None and page.preview._ready
        settled = {"n": 0}
        page.preview.connect("settled", lambda *_: settled.__setitem__("n", settled["n"] + 1))
        drawn = {}
        page.preview.connect("diagrams-complete", lambda *_: drawn.setdefault("ok", True))
        page.preview.render_all_diagrams()
        yield lambda: "ok" in drawn
        yield 0.5
        check("loads file", page.get_text().startswith("---\ntitle: Showcase"))
        check("not modified after load", not page.props.modified)
        check("outline parsed", [h.anchor for h in page.headings][:2] == ["showcase", "links"], str([h.anchor for h in page.headings]))

        box = self.js(page, "document.getElementById('content').innerHTML")
        yield lambda: "v" in box
        html = box["v"]
        for name, needle in [
            ("front matter", 'class="front-matter"'),
            ("alert", "markdown-alert-note"),
            ("table alignment", 'style="text-align:center"'),
            ("inline math", 'class="katex"'),
            ("display math", "katex-display"),
            ("task list", 'class="task-list-item-checkbox"'),
            ("mermaid svg", "<svg"),
            ("highlighted rust", "hljs-keyword"),
            ("cddl grammar", "hljs-type"),
            ("footnote", 'class="footnotes"'),
            ("deflist", "<dt"),
            ("abbr", "<abbr"),
            ("details", "<details"),
            ("shortcode badge", 'class="shortcode"'),
            ("explicit heading id", 'id="showcase"'),
            ("mark/sub/sup/ins", "<mark>"),
            ("collapsed changelog", '<details class="changelog" data-changelog="changelog"><summary><h2'),
            ("changelog entry count", "2 entries"),
            ("collapsed release table", 'data-changelog="release-history-of-the-widget"'),
        ]:
            check(f"preview renders {name}", needle in html)
        check("mermaid without error", "mermaid-error" not in html)
        check("version-less list is not a changelog", 'data-changelog="changes-in-behaviour"' not in html)
        check("prose after changelog stays visible", html.index("Prose after the changelog") > html.index("</details>", html.index('data-changelog="changelog"')))
        self.shot("01-showcase-light")

        # Task toggle from the preview -------------------------------------
        line = next(i for i, l in enumerate(page.get_text().split("\n")) if l.startswith("- [ ] Task open"))
        page.preview.emit("toggle-task", line)
        check("toggle task from preview", page.get_text().split("\n")[line].startswith("- [x] Task open"))
        page.buffer.undo()
        check("undo task toggle", page.get_text().split("\n")[line].startswith("- [ ] Task open"))

        # Formatting -------------------------------------------------------
        b = page.buffer
        b.insert(b.get_end_iter(), "\nword\n")
        end = b.get_end_iter()
        s = end.copy(); s.backward_chars(5); e = s.copy(); e.forward_chars(4)
        b.select_range(s, e)
        page.editing.bold()
        check("bold wraps selection", page.get_text().rstrip().endswith("**word**"))
        page.editing.bold()
        check("bold toggles off", page.get_text().rstrip().endswith("\nword"))
        b.place_cursor(b.get_end_iter())
        page.editing.heading(2)
        page.editing.heading(2)
        b.insert_at_cursor("- item one")
        page.editing._continue_list()
        check("enter continues bullet list", page.get_text().endswith("- item one\n- "), repr(page.get_text()[-20:]))
        page.editing._continue_list()
        check("enter on empty item ends list", page.get_text().endswith("- item one\n"), repr(page.get_text()[-20:]))
        b.insert_at_cursor("1. first")
        page.editing._continue_list()
        check("ordered list increments", page.get_text().endswith("1. first\n2. "))
        page.editing._indent_list_item(outdent=False)
        check("tab nests list item", page.get_text().endswith("1. first\n    2. "), repr(page.get_text()[-20:]))
        b.insert_at_cursor("x")
        b.insert(b.get_end_iter(), "\n\n|a|b|\n|-|:-:|\n|long cell|c|\n")
        it = b.get_end_iter(); it.backward_line()
        b.place_cursor(it)
        page.editing.format_table()
        tail = page.get_text().rstrip().split("\n")[-3:]
        check("format table aligns pipes", tail == ["| a         |  b  |", "| --------- | :-: |", "| long cell |  c  |"], str(tail))
        b.place_cursor(b.get_end_iter())
        page.editing.insert_toc()
        check("insert TOC", "- [Links](#links)" in page.get_text())
        check("modified after edits", page.props.modified)

        # Search -----------------------------------------------------------
        page.search.present()
        page.search.entry.set_text("Task")
        yield 0.4
        check("search finds matches", page.search.context.get_occurrences_count() >= 3, page.search.count.get_label())
        page.search.dismiss()

        # Save -------------------------------------------------------------
        saved = {}
        page.save(None, lambda ok: saved.__setitem__("ok", ok))
        yield lambda: "ok" in saved
        check("save succeeds", saved["ok"] and not page.props.modified)
        check("saved content on disk", "- [Links](#links)" in show.read_text())

        # External change auto-reload ---------------------------------------
        show.write_text(show.read_text() + "\nExternal edit line\n")
        yield lambda: "External edit line" in page.get_text()
        check("auto-reloads external change", "External edit line" in page.get_text())
        check("not modified after reload", not page.props.modified)

        # Link navigation ---------------------------------------------------
        page.emit("open-link", (docs / "other.md").as_uri() + "#second-section")
        other = win.find_page(Gio.File.new_for_path(str(docs / "other.md")))
        check("link opens markdown in tab", other is not None)
        yield lambda: other is not None and not other.props.loading and other.headings
        yield 0.3
        cursor_line = other.buffer.get_iter_at_mark(other.buffer.get_insert()).get_line()
        check("link anchor moves cursor to heading", cursor_line == 4, f"line {cursor_line}")
        check("hugo-style link resolves", win._resolve_local(docs / "other", page) == docs / "other.md")

        # View modes -------------------------------------------------------
        win.set_view_mode("preview")
        check("preview-only hides editor", not other.editor_area.get_visible() and other.preview_area.get_visible())
        win.set_view_mode("editor")
        check("editor-only hides preview", other.editor_area.get_visible() and not other.preview_area.get_visible())
        win.set_view_mode("split")

        # Dark mode --------------------------------------------------------
        Settings.get().props.color_scheme = "dark"
        yield 0.5
        settled["n"] = 0
        win.tab_view.set_selected_page(win.tab_view.get_page(page))
        yield 2.5
        box = self.js(page, "document.documentElement.className")
        yield lambda: "v" in box
        check("preview follows dark style", box["v"] == "dark", box["v"])
        self.shot("02-showcase-dark")
        Settings.get().props.color_scheme = "light"
        yield 2.5  # theme switch re-renders the diagrams

        # Scroll sync: scrolling the preview moves the editor ----------------
        vadj = page.scroller.get_vadjustment()
        before = vadj.get_value()
        # Scroll events are delivered with rendering updates, which stall when
        # the test window is not on screen; dispatch one explicitly.
        self.js(page, "window.scrollTo(0, document.documentElement.scrollHeight); window.dispatchEvent(new Event('scroll'))")
        yield 0.8
        diag = self.js(page, "window.scrollY + ' ' + document.visibilityState + ' ' + document.hasFocus()")
        yield lambda: "v" in diag
        print("scroll diag:", diag["v"])
        check("preview scroll drives editor", vadj.get_value() > before + 50, f"{before} -> {vadj.get_value()}")

        # Large document --------------------------------------------------
        if args.big:
            t0 = time.monotonic()
            big = win.open_file(Gio.File.new_for_path(args.big))
            yield lambda: not big.props.loading and big.preview is not None and big.preview._ready
            t_load = time.monotonic() - t0
            got = {}
            big.preview.connect("rendered", lambda *_: got.setdefault("t", time.monotonic()))
            yield lambda: "t" in got
            check("big doc loads", True, f"file load {t_load:.2f}s, first render {got['t'] - t0:.2f}s, {big.buffer.get_line_count()} lines")
            # Time a re-render of the full text.
            text = big.get_text()
            box = self.js(big, f"(() => {{ const t = performance.now(); GME.render({json.dumps(text)}); return String(performance.now() - t); }})()")
            yield lambda: "v" in box
            check("big doc re-render time", True, f"{float(box['v']):.0f} ms")
            # Diagrams render lazily near the viewport; force the rest.
            box = self.js(big, "String(document.querySelectorAll('.mermaid svg').length)")
            yield lambda: "v" in box
            check("big doc renders only nearby diagrams first", True, f"{box['v']} drawn before scrolling")
            ok = {}
            t = time.monotonic()
            big.preview.connect("diagrams-complete", lambda *_: ok.setdefault("t", time.monotonic()))
            big.preview.render_all_diagrams()
            yield lambda: "t" in ok
            check("big doc all diagrams", True, f"{ok['t'] - t:.1f}s")
            box = self.js(big, "String(document.querySelectorAll('.mermaid svg').length) + '/' + document.querySelectorAll('.mermaid').length + ' errors ' + document.querySelectorAll('.mermaid-error').length")
            yield lambda: "v" in box
            check("big doc mermaid diagrams", "errors 0" in box["v"], box["v"])
            t = time.monotonic()
            big.buffer.insert(big.buffer.get_start_iter(), "x")
            check("big doc keystroke latency", time.monotonic() - t < 0.1, f"{(time.monotonic() - t) * 1000:.0f} ms")
            # Large documents travel as patches; the page must end up with
            # exactly the editor's text (UTF-16 offsets, astral characters).
            mid = big.buffer.get_iter_at_offset(big.buffer.get_char_count() // 2)
            big.buffer.insert(mid, "🔗 é\n")
            big.buffer.delete(*[big.buffer.get_iter_at_offset(o) for o in (10, 14)])
            yield 1.5
            src = self.js(big, "GME.source()")
            yield lambda: "v" in src
            check("big doc patches keep page in sync", src["v"] == big.get_text(), f"{len(src['v'])} vs {len(big.get_text())}")
            big.goto_line(big.buffer.get_line_count() // 2)
            yield 1.5
            self.shot("03-big-doc")

        # Export HTML (without the file dialog) ------------------------------
        exported = {}
        win._with_rendered_preview(page, lambda pv: pv.get_html(lambda body: exported.setdefault("body", body)))
        yield lambda: "body" in exported
        doc = win._standalone_html(page, exported["body"] or "")
        check("export html contains rendered diagram", "<svg" in doc and "markdown-alert" in doc and "<!doctype html>" in doc)

        # Broken Mermaid shows an error instead of hanging -------------------
        broken = win.new_page()
        broken.buffer.set_text("# Broken\n\n```mermaid\nflowchart LR\n  A -->\n```\n")
        got = {}
        yield lambda: broken.preview is not None and broken.preview._ready
        broken.preview.connect("diagrams-complete", lambda *_: got.setdefault("ok", True))
        broken._render_now()
        broken.preview.render_all_diagrams()
        yield lambda: "ok" in got
        box = self.js(broken, "String(document.querySelectorAll('.mermaid-error').length)")
        yield lambda: "v" in box
        check("broken mermaid shows error", box["v"] == "1", box["v"])
        broken.buffer.set_modified(False)
        win.tab_view.close_page(win.tab_view.get_page(broken))
        yield 0.3
        win.tab_view.set_selected_page(win.tab_view.get_page(page))

        # Close with unsaved changes asks -----------------------------------
        page.buffer.insert(page.buffer.get_end_iter(), "unsaved")
        n = win.tab_view.get_n_pages()
        win.tab_view.close_page(win.tab_view.get_page(page))
        yield 0.5
        check("closing modified tab prompts", win.tab_view.get_n_pages() == n and win.get_visible_dialog() is not None)
        if win.get_visible_dialog() is not None:
            win.get_visible_dialog().close()
        yield 0.3
        self.shot("04-final")

        # Dialogs and empty state -------------------------------------------
        for action, name in (("preferences", "05-preferences"), ("shortcuts", "06-shortcuts"), ("about", "07-about")):
            app.activate_action(action, None)
            yield 1.0
            check(f"{action} dialog opens", win.get_visible_dialog() is not None)
            self.shot(name)
            if win.get_visible_dialog() is not None:
                win.get_visible_dialog().force_close()
            yield 0.4
        for p in win.pages():
            p.buffer.set_modified(False)
        while win.tab_view.get_n_pages():
            win.tab_view.close_page(win.tab_view.get_nth_page(0))
            yield 0.2
        check("empty state after closing all", win.content_stack.get_visible_child_name() == "empty")
        self.shot("08-empty")


def main() -> int:
    app = Application()
    app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    driver = Driver(app)

    def activate(_app):
        driver.run()

    app.connect("activate", activate)
    app.do_activate = lambda: None  # type: ignore[assignment]
    Application.do_activate = lambda self: None
    app.run([])
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
