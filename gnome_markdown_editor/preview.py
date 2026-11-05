"""WebKitGTK preview of a Markdown document."""

from __future__ import annotations

import json
import os
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, WebKit

from .settings import Settings

WEB_DIR = Path(__file__).resolve().parent / "web"
VENDOR_DIR = WEB_DIR / "vendor"

_ALLOWED_CONTEXT_ACTIONS = {
    WebKit.ContextMenuAction.COPY,
    WebKit.ContextMenuAction.COPY_LINK_TO_CLIPBOARD,
    WebKit.ContextMenuAction.COPY_IMAGE_TO_CLIPBOARD,
    WebKit.ContextMenuAction.COPY_IMAGE_URL_TO_CLIPBOARD,
    WebKit.ContextMenuAction.SELECT_ALL,
    WebKit.ContextMenuAction.INSPECT_ELEMENT,
}


def _shell_html() -> str:
    vendor = VENDOR_DIR.as_uri()
    web = WEB_DIR.as_uri()
    # Script may only come from the application's own files; documents get no
    # network access from script (connect-src) and no plugins or frames.
    csp = (
        "default-src 'none'; "
        "script-src file:; "
        "style-src 'unsafe-inline' file:; "
        "font-src file: data:; "
        "img-src * file: data: blob:; "
        "media-src * file: data:; "
        "connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
    )
    return f"""<!doctype html>
<html class="light">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<meta name="color-scheme" content="light dark">
<link rel="stylesheet" href="{vendor}/katex/katex.min.css">
<link rel="stylesheet" href="{vendor}/highlight.css">
<link rel="stylesheet" href="{web}/preview.css">
</head>
<body class="empty">
<div id="content"></div>
<script src="{vendor}/renderer.js"></script>
</body>
</html>
"""


def _edit_range(old: str, new: str) -> tuple[int, int, str]:
    """(start, removed length, inserted text) turning ``old`` into ``new``."""
    n = min(len(old), len(new))
    block = 4096
    i = 0
    while i + block <= n and old[i : i + block] == new[i : i + block]:
        i += block
    while i < n and old[i] == new[i]:
        i += 1
    limit = n - i
    j = 0
    lo, ln = len(old), len(new)
    while j + block <= limit and old[lo - j - block : lo - j] == new[ln - j - block : ln - j]:
        j += block
    while j < limit and old[lo - 1 - j] == new[ln - 1 - j]:
        j += 1
    return i, lo - i - j, new[i : ln - j]


class Preview(Gtk.Box):
    """Rendered view of the document. Signals carry host-relevant events."""

    __gtype_name__ = "GmePreview"
    __gsignals__ = {
        "open-link": (GObject.SignalFlags.RUN_LAST, None, (str, str)),
        "scrolled": (GObject.SignalFlags.RUN_LAST, None, (float,)),
        "toggle-task": (GObject.SignalFlags.RUN_LAST, None, (int,)),
        "reveal-line": (GObject.SignalFlags.RUN_LAST, None, (int,)),
        "rendered": (GObject.SignalFlags.RUN_LAST, None, ()),
        # Emitted once asynchronous work (Mermaid diagrams) has finished.
        "settled": (GObject.SignalFlags.RUN_LAST, None, ()),
        # Every diagram is drawn, after render_all_diagrams().
        "diagrams-complete": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.settings = Settings.get()
        self._ready = False
        self._pending_text: str | None = None
        self._pending_anchor: str | None = None
        self._pending_line: float | None = None
        self._base_uri = Path.home().as_uri() + "/"
        self._last_text: str | None = None
        self._sent_text: str | None = None  # the source the page holds
        self._render_all_pending = False
        self.last_render_posted = False

        manager = WebKit.UserContentManager()
        manager.register_script_message_handler("gme", None)
        manager.connect("script-message-received::gme", self._on_message)

        self.webview = WebKit.WebView(user_content_manager=manager)
        self.webview.set_vexpand(True)
        self.webview.set_hexpand(True)
        ws = self.webview.get_settings()
        ws.set_enable_javascript(True)
        ws.set_javascript_can_open_windows_automatically(False)
        ws.set_allow_file_access_from_file_urls(False)
        ws.set_allow_universal_access_from_file_urls(False)
        ws.set_enable_back_forward_navigation_gestures(False)
        ws.set_enable_developer_extras(bool(os.environ.get("GME_DEBUG")))
        ws.set_enable_write_console_messages_to_stdout(bool(os.environ.get("GME_DEBUG")))
        ws.set_enable_html5_local_storage(False)
        ws.set_enable_html5_database(False)
        ws.set_enable_page_cache(False)
        ws.set_enable_smooth_scrolling(True)
        self.webview.connect("decide-policy", self._on_decide_policy)
        self.webview.connect("context-menu", self._on_context_menu)
        self.webview.connect("web-process-terminated", self._on_crashed)
        self.append(self.webview)

        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", lambda *_: self._apply_theme())
        style.connect("notify::accent-color", lambda *_: self._apply_theme())
        for key in ("preview-font-size", "preview-full-width", "collapse-changelogs"):
            self.settings.connect(f"notify::{key}", lambda *_: self._apply_config())
        self.settings.connect("notify::zoom", lambda *_: self._apply_zoom())
        self._apply_zoom()
        self._apply_background()
        self._load_shell()

    # ------------------------------------------------------------------
    # Loading

    def set_base_dir(self, directory: Path | None) -> None:
        uri = (directory or Path.home()).resolve().as_uri().rstrip("/") + "/"
        if uri == self._base_uri:
            return
        self._base_uri = uri
        self._pending_text = self._last_text if self._pending_text is None else self._pending_text
        self._load_shell()

    def _load_shell(self) -> None:
        self._ready = False
        self._sent_text = None
        self.webview.load_html(_shell_html(), self._base_uri)

    def _on_crashed(self, _view, reason) -> None:
        self._ready = False
        self._pending_text = self._last_text
        GLib.timeout_add(500, lambda: (self._load_shell(), False)[1])

    # ------------------------------------------------------------------
    # JS calls

    def _js(self, code: str, callback=None) -> None:
        def done(view, res):
            try:
                value = view.evaluate_javascript_finish(res)
            except GLib.Error as exc:
                if os.environ.get("GME_DEBUG"):
                    print("preview JS error:", exc.message)
                value = None
            if callback:
                callback(value)

        self.webview.evaluate_javascript(code, -1, None, None, None, done)

    def render(self, text: str) -> None:
        self.last_render_posted = True
        self._last_text = text
        if not self._ready:
            self._pending_text = text
            return
        sent, self._sent_text = self._sent_text, text
        if sent is None or len(text) < 20000:
            self._js(f"GME.render({json.dumps(text)})")
            return
        if sent == text:
            self.last_render_posted = False  # nothing changed: no "rendered" follows
            return
        # Large documents: send only the changed range. Offsets are in UTF-16
        # code units, as JavaScript strings count them.
        start, removed, inserted = _edit_range(sent, text)
        u16 = lambda s: len(s.encode("utf-16-le")) // 2  # noqa: E731
        self._js(
            f"GME.renderPatch({u16(sent[:start])}, {u16(sent[start:start + removed])}, "
            f"{json.dumps(inserted)}, {u16(text)})"
        )

    def render_all_diagrams(self) -> None:
        """Render every Mermaid diagram now; ``diagrams-complete`` follows."""
        if not self._ready:
            self._render_all_pending = True
            return
        self._js("GME.renderAllDiagrams()")

    def scroll_to_line(self, line: float) -> None:
        if not self._ready:
            self._pending_line = line
            return
        self._js(f"GME.scrollToLine({line:.3f})")

    def scroll_to_anchor(self, anchor: str) -> None:
        self._pending_anchor = anchor
        if self._ready and self._last_text is not None:
            self._flush_anchor()

    def _flush_anchor(self) -> None:
        if self._pending_anchor:
            anchor, self._pending_anchor = self._pending_anchor, None
            self._js(f"GME.scrollToAnchor({json.dumps(anchor)})")

    def get_html(self, callback) -> None:
        """Rendered body markup (Mermaid diagrams included) for export."""
        if not self._ready:
            callback(None)
            return
        self._js("GME.exportHtml()", lambda v: callback(v.to_string() if v is not None else None))

    def print_document(self, parent: Gtk.Window) -> None:
        op = WebKit.PrintOperation.new(self.webview)
        op.run_dialog(parent)

    def _apply_zoom(self) -> None:
        # The editor zoom (points) scales the preview as well.
        self.webview.set_zoom_level(max(0.3, 1.0 + self.settings.props.zoom * 0.08))

    # ------------------------------------------------------------------
    # Theme

    def _theme(self) -> str:
        return "dark" if Adw.StyleManager.get_default().get_dark() else "light"

    def _apply_background(self) -> None:
        rgba = Gdk.RGBA()
        rgba.parse("#1d1d20" if self._theme() == "dark" else "#ffffff")
        self.webview.set_background_color(rgba)

    def _config(self) -> dict:
        style = Adw.StyleManager.get_default()
        accent = style.get_accent_color_rgba() if hasattr(style, "get_accent_color_rgba") else None
        cfg = {
            "theme": self._theme(),
            "fontSize": self.settings.props.preview_font_size,
            "maxWidth": not self.settings.props.preview_full_width,
            "collapseChangelogs": self.settings.props.collapse_changelogs,
            "mermaidUrl": (VENDOR_DIR / "mermaid.min.js").as_uri(),
        }
        if accent is not None:
            cfg["accent"] = "#%02x%02x%02x" % (int(accent.red * 255), int(accent.green * 255), int(accent.blue * 255))
        return cfg

    def _apply_config(self) -> None:
        if self._ready:
            cfg = self._config()
            accent = cfg.pop("accent", None)
            code = f"GME.configure({json.dumps(cfg)});"
            if accent:
                code += f"document.documentElement.style.setProperty('--accent', {json.dumps(accent)});"
            self._js(code)

    def _apply_theme(self) -> None:
        self._apply_background()
        self._apply_config()

    # ------------------------------------------------------------------
    # Messages from the page

    def _on_message(self, _manager, value) -> None:
        try:
            msg = json.loads(value.to_string())
        except (ValueError, TypeError):
            return
        kind = msg.get("type")
        if kind == "ready":
            self._ready = True
            self._apply_config()
            text = self._pending_text if self._pending_text is not None else self._last_text
            self._pending_text = None
            if text is not None:
                self.render(text)
        elif kind == "rendered":
            if self._pending_line is not None:
                line, self._pending_line = self._pending_line, None
                self.scroll_to_line(line)
            self._flush_anchor()
            self.emit("rendered")
            if self._render_all_pending:
                self._render_all_pending = False
                self.render_all_diagrams()
        elif kind == "all-diagrams":
            self.emit("diagrams-complete")
        elif kind == "resync":
            self._sent_text = None
            if self._last_text is not None:
                self.render(self._last_text)
        elif kind == "rendered-async":
            # Drawn diagrams change the layout: scroll sync re-aligns.
            if msg.get("drawn"):
                self.emit("rendered")
            self.emit("settled")
        elif kind == "scroll":
            self.emit("scrolled", float(msg.get("line", 0)))
        elif kind == "open-link":
            self.emit("open-link", msg.get("href", ""), msg.get("raw", ""))
        elif kind == "toggle-task":
            self.emit("toggle-task", int(msg.get("line", -1)))
        elif kind == "reveal":
            self.emit("reveal-line", int(msg.get("line", 0)))
        elif kind == "copy":
            Gdk.Display.get_default().get_clipboard().set(msg.get("text", ""))

    # ------------------------------------------------------------------
    # Navigation policy: the page never navigates; links go to the host.

    def _on_decide_policy(self, _view, decision, decision_type) -> bool:
        if decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            action = decision.get_navigation_action()
            uri = action.get_request().get_uri()
            if action.get_navigation_type() == WebKit.NavigationType.OTHER and uri == self._base_uri:
                decision.use()
                return True
            decision.ignore()
            if action.is_user_gesture() and uri:
                self.emit("open-link", uri, uri)
            return True
        if decision_type == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            action = decision.get_navigation_action()
            decision.ignore()
            uri = action.get_request().get_uri()
            if uri:
                self.emit("open-link", uri, uri)
            return True
        return False

    def _on_context_menu(self, _view, menu, _hit) -> bool:
        for item in list(menu.get_items()):
            if item.get_stock_action() not in _ALLOWED_CONTEXT_ACTIONS:
                menu.remove(item)
        return False
