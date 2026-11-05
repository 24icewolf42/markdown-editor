"""Application object: windows, global actions, editor font."""

from __future__ import annotations

import json
import os
import sys
from gettext import gettext as _
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GtkSource", "5")
gi.require_version("WebKit", "6.0")
gi.require_version("Spelling", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, GtkSource, Pango  # noqa: E402

from . import APP_ID, VERSION  # noqa: E402
from .settings import Settings  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"

APP_CSS = """
.gme-statusbar { min-height: 22px; }
.gme-searchbar { padding: 6px 8px; border-bottom: 1px solid alpha(currentColor, 0.12); background: var(--view-bg-color, @view_bg_color); }
.gme-preview-area { border-left: 1px solid alpha(currentColor, 0.10); }
paned > separator { background: alpha(currentColor, 0.10); }
"""

SHORTCUTS = [
    (_("Documents"), [
        (_("New document"), "<Control>t"), (_("New window"), "<Control>n"), (_("Open"), "<Control>o"),
        (_("Save"), "<Control>s"), (_("Save as"), "<Control><Shift>s"), (_("Save all"), "<Control><Alt>s"),
        (_("Reload"), "<Control><Shift>r"), (_("Print"), "<Control>p"), (_("Close document"), "<Control>w"),
        (_("Quit"), "<Control>q"),
    ]),
    (_("View"), [
        (_("Editor only"), "<Control><Alt>1"), (_("Editor and preview"), "<Control><Alt>2"),
        (_("Preview only"), "<Control><Alt>3"), (_("Toggle preview"), "<Control><Shift>p"),
        (_("Outline"), "F9"), (_("Zoom in"), "<Control>plus"), (_("Zoom out"), "<Control>minus"),
        (_("Reset zoom"), "<Control>0"), (_("Fullscreen"), "F11"), (_("Switch to document 1–9"), "<Alt>1...9"),
    ]),
    (_("Find"), [
        (_("Find"), "<Control>f"), (_("Find and replace"), "<Control>h"), (_("Next match"), "<Control>g"),
        (_("Previous match"), "<Control><Shift>g"), (_("Go to line"), "<Control>l"),
    ]),
    (_("Formatting"), [
        (_("Bold"), "<Control>b"), (_("Italic"), "<Control>i"), (_("Strikethrough"), "<Control><Shift>x"),
        (_("Highlight"), "<Control><Shift>h"), (_("Inline code"), "<Control>grave"), (_("Inline math"), "<Control><Shift>m"),
        (_("Link"), "<Control>k"), (_("Image"), "<Control><Shift>i"), (_("Footnote"), "<Control><Shift>f"),
        (_("Heading 1–6"), "<Control>1...6"), (_("Plain paragraph"), "<Control>7"),
        (_("Bulleted list"), "<Control><Shift>8"), (_("Numbered list"), "<Control><Shift>7"),
        (_("Task list"), "<Control><Shift>9"), (_("Block quote"), "<Control><Shift>period"),
        (_("Code block"), "<Control><Shift>k"), (_("Table"), "<Control><Shift>t"), (_("Format table"), "<Control><Alt>t"),
    ]),
    (_("Editing"), [
        (_("Undo"), "<Control>z"), (_("Redo"), "<Control><Shift>z"), (_("Indent list item"), "Tab"),
        (_("Outdent list item"), "<Shift>Tab"), (_("Move line up"), "<Alt>Up"), (_("Move line down"), "<Alt>Down"),
        (_("Newline without continuing a list"), "<Shift>Return"),
    ]),
]


class Application(Adw.Application):
    def __init__(self) -> None:
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if os.environ.get("GME_NON_UNIQUE"):
            # Tests: never hand files to an instance the user is running.
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        GLib.set_application_name(_("Markdown Editor"))
        self.settings = Settings.get()
        self._font_provider = Gtk.CssProvider()
        self.add_main_option("new-window", ord("n"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE, _("Open a new window"), None)
        self._force_new_window = False

    # ------------------------------------------------------------------

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GtkSource.init()
        display = Gdk.Display.get_default()
        icons = Gtk.IconTheme.get_for_display(display)
        icons.add_search_path(str(DATA_DIR / "icons"))
        Gtk.Window.set_default_icon_name(APP_ID)
        css = Gtk.CssProvider()
        css.load_from_string(APP_CSS)
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.StyleContext.add_provider_for_display(display, self._font_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

        self._iface = None
        source = Gio.SettingsSchemaSource.get_default()
        if source is not None and source.lookup("org.gnome.desktop.interface", True) is not None:
            self._iface = Gio.Settings.new("org.gnome.desktop.interface")
            self._iface.connect("changed::monospace-font-name", lambda *_: self._update_font())
        for key in ("use-system-font", "custom-font", "zoom"):
            self.settings.connect(f"notify::{key}", lambda *_: self._update_font())
        self.settings.connect("notify::color-scheme", lambda *_: self._update_color_scheme())
        self._update_font()
        self._update_color_scheme()

        for name, cb in (
            ("new-window", lambda *_: self.new_window().present()),
            ("preferences", lambda *_: self._show_preferences()),
            ("shortcuts", lambda *_: self._show_shortcuts()),
            ("about", lambda *_: self._show_about()),
            ("quit", lambda *_: self._quit()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
        self.set_accels_for_action("app.new-window", ["<Control>n"])
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.preferences", ["<Control>comma"])
        self.set_accels_for_action("app.shortcuts", ["<Control>question"])

    def do_handle_local_options(self, options) -> int:
        if options.contains("new-window"):
            self._force_new_window = True
        return -1

    def do_activate(self) -> None:
        win = self.get_active_window()
        if win is None or self._force_new_window:
            win = self.new_window()
            if not self._restore_session(win):
                win.new_page()
        self._force_new_window = False
        win.present()
        self.schedule_debug_screenshot()

    def do_open(self, files, n_files, hint) -> None:
        win = self.get_active_window()
        if win is None or self._force_new_window:
            win = self.new_window()
        self._force_new_window = False
        for f in files:
            win.open_file(f)
        win.present()
        self.schedule_debug_screenshot()

    def schedule_debug_screenshot(self) -> None:
        """GME_SCREENSHOT=out.png[:seconds] saves the window and quits (for tests)."""
        spec = os.environ.get("GME_SCREENSHOT")
        if not spec:
            return
        path, _sep, delay = spec.partition(":")
        wins = self.get_windows()
        # The paintable records the widget's render node on the next frames.
        self._debug_paintable = Gtk.WidgetPaintable.new(wins[0]) if wins else None
        GLib.timeout_add(int(float(delay or 4) * 1000), self._debug_screenshot, path)

    def _debug_screenshot(self, path: str) -> bool:
        wins = self.get_windows()
        win = wins[0] if wins else None
        if win is None:
            print("screenshot: no window", flush=True)
        else:
            paintable = self._debug_paintable or Gtk.WidgetPaintable.new(win)
            w, h = win.get_width(), win.get_height()
            snap = Gtk.Snapshot()
            paintable.snapshot(snap, w, h)
            node = snap.to_node()
            if node is None:
                print("screenshot: empty render", flush=True)
            else:
                texture = win.get_native().get_renderer().render_texture(node, None)
                texture.save_to_png(path)
                print("screenshot:", path, flush=True)
        if not os.environ.get("GME_SCREENSHOT_KEEP"):
            for w in self.get_windows():
                for p in w.pages():
                    p.buffer.set_modified(False)
                w._closing_confirmed = True
                w.close()
        return GLib.SOURCE_REMOVE

    def do_shutdown(self) -> None:
        self.settings.flush()
        Adw.Application.do_shutdown(self)

    # ------------------------------------------------------------------

    def new_window(self):
        from .window import Window

        return Window(self)

    def _restore_session(self, win) -> bool:
        if not self.settings.props.restore_session:
            return False
        try:
            paths = json.loads(self.settings.props.session_files)
        except ValueError:
            return False
        opened = 0
        for p in paths:
            if isinstance(p, str) and Path(p).is_file():
                win.open_file(Gio.File.new_for_path(p))
                opened += 1
        if opened:
            idx = max(0, min(self.settings.props.session_active, win.tab_view.get_n_pages() - 1))
            win.tab_view.set_selected_page(win.tab_view.get_nth_page(idx))
        return opened > 0

    def _quit(self) -> None:
        for win in list(self.get_windows()):
            win.close()

    def _update_color_scheme(self) -> None:
        scheme = {
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }.get(self.settings.props.color_scheme, Adw.ColorScheme.DEFAULT)
        Adw.StyleManager.get_default().set_color_scheme(scheme)

    def _update_font(self) -> None:
        s = self.settings.props
        name = s.custom_font
        if s.use_system_font:
            name = self._iface.get_string("monospace-font-name") if self._iface else "Monospace 11"
        desc = Pango.FontDescription.from_string(name or "Monospace 11")
        size = desc.get_size() / Pango.SCALE if desc.get_size() else 11
        size = max(4, size + s.zoom)
        family = desc.get_family() or "Monospace"
        weight = int(desc.get_weight())
        style = {Pango.Style.ITALIC: "italic", Pango.Style.OBLIQUE: "oblique"}.get(desc.get_style(), "normal")
        css = (
            f'textview.gme-editor {{ font-family: "{family}"; font-size: {size}pt; '
            f"font-weight: {weight}; font-style: {style}; }}"
        )
        self._font_provider.load_from_string(css)

    # ------------------------------------------------------------------

    def _show_preferences(self) -> None:
        from .preferences import PreferencesDialog

        PreferencesDialog().present(self.get_active_window())

    def _show_shortcuts(self) -> None:
        dialog = Adw.ShortcutsDialog()
        for title, items in SHORTCUTS:
            section = Adw.ShortcutsSection(title=title)
            for label, accel in items:
                section.add(Adw.ShortcutsItem(title=label, accelerator=accel))
            dialog.add(section)
        dialog.present(self.get_active_window())

    def _show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name=_("Markdown Editor"),
            application_icon=APP_ID,
            developer_name="Andreas",
            version=VERSION,
            comments=_(
                "Edit and preview Markdown with GitHub, Hugo and Antigravity extensions: alerts, Mermaid diagrams, "
                "math, footnotes, task lists, front matter and image carousels."
            ),
            license_type=Gtk.License.GPL_3_0,
        )
        try:
            notices = (Path(__file__).resolve().parent / "web" / "vendor" / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8")
            about.add_legal_section(_("Preview components"), None, Gtk.License.CUSTOM, notices.split("\n", 2)[-1])
        except OSError:
            pass
        about.present(self.get_active_window())


def main(argv: list[str] | None = None) -> int:
    app = Application()
    return app.run(argv if argv is not None else sys.argv)
