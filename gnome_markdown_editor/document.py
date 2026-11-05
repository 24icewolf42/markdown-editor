"""One open document: source editor, live preview, file I/O."""

from __future__ import annotations

import os
import time
from gettext import gettext as _
from pathlib import Path

from gi.repository import Adw, Gio, GLib, GObject, Gtk, GtkSource, Spelling

from . import markdown_utils as mu
from .editor import MarkdownEditing
from .preview import Preview
from .search import SearchBar
from .settings import Settings

MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd", ".mkdn", ".mdwn", ".mdx", ".rmd", ".qmd", ".txt"}

_untitled_counter = 0
DEBUG = bool(os.environ.get("GME_DEBUG"))


def resolve_style_scheme(scheme_id: str) -> GtkSource.StyleScheme | None:
    manager = GtkSource.StyleSchemeManager.get_default()
    dark = Adw.StyleManager.get_default().get_dark()
    ids = manager.get_scheme_ids() or []
    base = scheme_id
    for suffix in ("-dark", "-light"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    if dark:
        candidates = [f"{base}-dark", scheme_id, base]
    else:
        candidates = [base if base in ids else f"{base}-light", f"{base}-light", scheme_id]
    for cid in candidates:
        if cid in ids:
            scheme = manager.get_scheme(cid)
            if scheme is not None:
                return scheme
    return manager.get_scheme("Adwaita-dark" if dark else "Adwaita")


class DocumentPage(Gtk.Box):
    __gtype_name__ = "GmeDocumentPage"
    __gsignals__ = {
        "outline-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "cursor-moved": (GObject.SignalFlags.RUN_LAST, None, ()),
        "open-link": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "toast": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        # The file is loaded and the cursor restored.
        "loaded": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    title = GObject.Property(type=str, default="")
    subtitle = GObject.Property(type=str, default="")
    modified = GObject.Property(type=bool, default=False)
    loading = GObject.Property(type=bool, default=False)

    def __init__(self, file: Gio.File | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        global _untitled_counter
        self.settings = Settings.get()
        self.headings: list[mu.Heading] = []
        self.word_count = 0
        self._render_source = 0
        self._outline_source = 0
        self._sync_source = 0
        self._monitor: Gio.FileMonitor | None = None
        self._monitor_source = 0
        self._disk_state: tuple[int, int] | None = None
        self._preview_dirty = True
        self._scroll_leader = "editor"
        self._suppress_editor_scroll_until = 0.0
        self._suppress_preview_scroll_until = 0.0
        self._handlers: list[tuple[GObject.Object, int]] = []
        self._view_mode = self.settings.props.view_mode
        if file is None:
            _untitled_counter += 1
            self._untitled_name = _("Untitled Document %d") % _untitled_counter if _untitled_counter > 1 else _("Untitled Document")
        else:
            self._untitled_name = ""

        # Source buffer -------------------------------------------------
        self.source_file = GtkSource.File()
        self.buffer = GtkSource.Buffer()
        lang = GtkSource.LanguageManager.get_default().get_language("markdown")
        self.buffer.set_language(lang)
        self.buffer.set_highlight_syntax(True)
        self.buffer.set_implicit_trailing_newline(True)
        self.buffer.connect("changed", self._on_changed)
        self.buffer.connect("modified-changed", self._on_modified_changed)
        self.buffer.connect("notify::cursor-position", lambda *_: self.emit("cursor-moved"))

        self.view = GtkSource.View(buffer=self.buffer)
        self.view.add_css_class("gme-editor")
        self.view.set_monospace(True)
        self.view.set_top_margin(12)
        self.view.set_bottom_margin(120)
        self.view.set_left_margin(10)
        self.view.set_right_margin(12)
        self.view.set_smart_home_end(GtkSource.SmartHomeEndType.BEFORE)
        self.view.set_smart_backspace(True)
        self.view.set_show_line_marks(False)
        self.view.set_input_hints(Gtk.InputHints.SPELLCHECK | Gtk.InputHints.WORD_COMPLETION)
        self.editing = MarkdownEditing(self.view, self.settings)

        # Spell checking (libspelling) ----------------------------------
        self.spell_adapter = Spelling.TextBufferAdapter.new(self.buffer, Spelling.Checker.get_default())
        self.view.set_extra_menu(self.spell_adapter.get_menu_model())
        self.view.insert_action_group("spelling", self.spell_adapter)

        self.scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.scroller.set_child(self.view)
        self.scroller.get_vadjustment().connect("value-changed", self._on_editor_scrolled)
        self.map = GtkSource.Map(view=self.view, visible=False)
        editor_box = Gtk.Box()
        editor_box.append(self.scroller)
        editor_box.append(self.map)

        self.search = SearchBar(self.view)
        self.editor_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.editor_area.append(self.search)
        self.editor_area.append(editor_box)

        # Preview (created lazily: a WebKit process per tab is not free) --
        self.preview: Preview | None = None
        self.preview_area = Gtk.Box(hexpand=True)
        self.preview_area.add_css_class("gme-preview-area")

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=False)
        self.paned.set_shrink_start_child(False)
        self.paned.set_shrink_end_child(False)
        self.paned.set_resize_start_child(True)
        self.paned.set_resize_end_child(True)
        self._paned_initialized = False
        self.paned.connect("notify::max-position", self._on_paned_allocated)

        # Banners -------------------------------------------------------
        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", self._on_banner_clicked)
        self._banner_action = None
        self.append(self.banner)
        self.append(self.paned)

        self._arrange_panes()
        self._connect_settings()
        self._apply_settings()

        if file is not None:
            self.load(file)
        else:
            self._update_title()

    # ------------------------------------------------------------------
    # Settings

    def _connect_settings(self) -> None:
        s = self.settings
        for key in (
            "show-line-numbers", "highlight-current-line", "show-right-margin", "right-margin-position",
            "wrap-text", "tab-width", "insert-spaces", "auto-indent", "spellcheck", "show-map",
            "style-scheme", "highlight-matching-brackets",
        ):
            self._handlers.append((s, s.connect(f"notify::{key}", lambda *_: self._apply_settings())))
        self._handlers.append((s, s.connect("notify::preview-position", lambda *_: self._arrange_panes())))
        sm = Adw.StyleManager.get_default()
        self._handlers.append((sm, sm.connect("notify::dark", lambda *_: self._apply_settings())))

    def _apply_settings(self) -> None:
        s = self.settings.props
        self.view.set_show_line_numbers(s.show_line_numbers)
        self.view.set_highlight_current_line(s.highlight_current_line)
        self.view.set_show_right_margin(s.show_right_margin)
        self.view.set_right_margin_position(max(1, s.right_margin_position))
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR if s.wrap_text else Gtk.WrapMode.NONE)
        self.view.set_tab_width(max(1, s.tab_width))
        self.view.set_indent_width(-1)
        self.view.set_insert_spaces_instead_of_tabs(s.insert_spaces)
        self.view.set_auto_indent(s.auto_indent)
        self.buffer.set_highlight_matching_brackets(s.highlight_matching_brackets)
        self.spell_adapter.set_enabled(s.spellcheck)
        self.map.set_visible(s.show_map)
        self.buffer.set_style_scheme(resolve_style_scheme(s.style_scheme))

    def disconnect_all(self) -> None:
        for obj, hid in self._handlers:
            obj.disconnect(hid)
        self._handlers.clear()
        for attr in ("_render_source", "_outline_source", "_sync_source", "_monitor_source"):
            src = getattr(self, attr)
            if src:
                GLib.source_remove(src)
                setattr(self, attr, 0)
        if self._monitor:
            self._monitor.cancel()
            self._monitor = None

    # ------------------------------------------------------------------
    # View modes

    @property
    def view_mode(self) -> str:
        return self._view_mode

    def set_view_mode(self, mode: str) -> None:
        if mode not in ("editor", "split", "preview"):
            mode = "split"
        self._view_mode = mode
        self._arrange_panes()

    def _ensure_preview(self) -> Preview:
        if self.preview is None:
            self.preview = Preview()
            self.preview.set_base_dir(self.directory)
            self.preview.connect("open-link", lambda _p, href, _raw: self.emit("open-link", href))
            self.preview.connect("scrolled", self._on_preview_scrolled)
            self.preview.connect("toggle-task", lambda _p, line: self.editing.toggle_task_at_line(line))
            self.preview.connect("reveal-line", self._on_reveal_line)
            self.preview.connect("rendered", self._on_preview_rendered)
            self.preview_area.append(self.preview)
            self._preview_dirty = True
        return self.preview

    def _arrange_panes(self) -> None:
        left_preview = self.settings.props.preview_position == "left"
        start, end = (self.preview_area, self.editor_area) if left_preview else (self.editor_area, self.preview_area)
        if self.paned.get_start_child() is not start:
            self.paned.set_start_child(None)
            self.paned.set_end_child(None)
            self.paned.set_start_child(start)
            self.paned.set_end_child(end)
        mode = self._view_mode
        self.editor_area.set_visible(mode != "preview")
        self.preview_area.set_visible(mode != "editor")
        if mode != "editor":
            self._ensure_preview()
            if self._preview_dirty:
                self._schedule_render(immediate=True)
        if mode == "split" and self.paned.props.max_position > 0:
            self.paned.set_position(self.paned.props.max_position // 2)

    def _on_paned_allocated(self, paned, _pspec) -> None:
        if not self._paned_initialized and paned.props.max_position > 0:
            self._paned_initialized = True
            paned.set_position(paned.props.max_position // 2)

    def focus_editor(self) -> None:
        if self._view_mode == "preview":
            return
        self.view.grab_focus()

    # ------------------------------------------------------------------
    # Titles

    @property
    def file(self) -> Gio.File | None:
        return self.source_file.get_location()

    @property
    def path(self) -> Path | None:
        f = self.file
        return Path(f.get_path()) if f is not None and f.get_path() else None

    @property
    def directory(self) -> Path | None:
        p = self.path
        return p.parent if p else None

    @property
    def is_untitled(self) -> bool:
        return self.file is None

    def is_empty_untitled(self) -> bool:
        return self.is_untitled and not self.buffer.get_modified() and self.buffer.get_char_count() == 0

    def _update_title(self) -> None:
        f = self.file
        if f is None:
            self.props.title = self._untitled_name
            self.props.subtitle = _("Draft")
        else:
            self.props.title = f.get_basename() or f.get_uri()
            parent = f.get_parent()
            path = parent.get_path() if parent else None
            if path:
                home = str(Path.home())
                path = "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path
            self.props.subtitle = path or (parent.get_uri() if parent else "")

    def _on_modified_changed(self, buf) -> None:
        self.props.modified = buf.get_modified()

    # ------------------------------------------------------------------
    # Loading and saving

    def load(self, file: Gio.File, keep_position: bool = False) -> None:
        cursor_line = self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_line() if keep_position else 0
        vvalue = self.scroller.get_vadjustment().get_value() if keep_position else 0.0
        self.source_file.set_location(file)
        self._update_title()
        if self.preview:
            self.preview.set_base_dir(self.directory)
        self.props.loading = True
        loader = GtkSource.FileLoader.new(self.buffer, self.source_file)

        def done(loader, result):
            self.props.loading = False
            try:
                loader.load_finish(result)
            except GLib.Error as exc:
                if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND):
                    # Opening a path that does not exist yet starts a new file there.
                    self.buffer.set_text("")
                    self.buffer.set_modified(False)
                else:
                    self.show_banner(_("Could not open “%s”: %s") % (file.get_basename(), exc.message))
                    self.emit("toast", _("Could not open “%s”") % file.get_basename())
            self.buffer.set_modified(False)
            self._record_disk_state()

            def restore_position():
                ok, it = self.buffer.get_iter_at_line(min(cursor_line, max(0, self.buffer.get_line_count() - 1)))
                self.buffer.place_cursor(it)
                self.scroller.get_vadjustment().set_value(vvalue)
                self.emit("cursor-moved")
                self.emit("loaded")
                return GLib.SOURCE_REMOVE

            # After the loader has finished touching the buffer.
            GLib.idle_add(restore_position)
            self._start_monitor()
            self._on_changed(self.buffer)
            self._update_outline()
            self.emit("cursor-moved")
            Gtk.RecentManager.get_default().add_item(file.get_uri())

        loader.load_async(GLib.PRIORITY_DEFAULT, None, None, None, done)

    def reload(self) -> None:
        if self.file is not None:
            self.hide_banner()
            self.load(self.file, keep_position=True)

    def save(self, file: Gio.File | None = None, callback=None, force: bool = False) -> None:
        """Save to ``file`` (or the current location). ``callback(ok)``."""
        target = file or self.file
        if target is None:
            raise ValueError("no location to save to")
        if file is not None and (self.file is None or not file.equal(self.file)):
            self.source_file.set_location(file)
            self._update_title()
            if self.preview:
                self.preview.set_base_dir(self.directory)
        saver = GtkSource.FileSaver.new(self.buffer, self.source_file)
        flags = GtkSource.FileSaverFlags.NONE
        if force:
            flags |= GtkSource.FileSaverFlags.IGNORE_MODIFICATION_TIME | GtkSource.FileSaverFlags.IGNORE_INVALID_CHARS
        saver.set_flags(flags)
        if self._monitor:
            self._monitor.cancel()
            self._monitor = None

        def done(saver, result):
            ok = True
            try:
                saver.save_finish(result)
            except GLib.Error as exc:
                ok = False
                if exc.matches(GtkSource.file_saver_error_quark(), GtkSource.FileSaverError.EXTERNALLY_MODIFIED):
                    self._confirm_overwrite(target, callback)
                    self._start_monitor()
                    return
                self.emit("toast", _("Could not save “%s”: %s") % (target.get_basename(), exc.message))
            if ok:
                self.buffer.set_modified(False)
                self._record_disk_state()
                self.hide_banner()
                Gtk.RecentManager.get_default().add_item(target.get_uri())
            self._start_monitor()
            if callback:
                callback(ok)

        saver.save_async(GLib.PRIORITY_DEFAULT, None, None, None, done)

    def _confirm_overwrite(self, target: Gio.File, callback) -> None:
        dialog = Adw.AlertDialog(
            heading=_("File Changed on Disk"),
            body=_("“%s” was changed by another program since it was opened. Overwrite it with your version?")
            % target.get_basename(),
        )
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("overwrite", _("_Overwrite"))
        dialog.set_response_appearance("overwrite", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def on_response(_d, response):
            if response == "overwrite":
                self.save(target, callback, force=True)
            elif callback:
                callback(False)

        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    # ------------------------------------------------------------------
    # External changes

    def _start_monitor(self) -> None:
        if self._monitor:
            self._monitor.cancel()
            self._monitor = None
        f = self.file
        if f is None or not f.is_native():
            return
        try:
            self._monitor = f.monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
        except GLib.Error:
            return
        self._monitor.set_rate_limit(250)
        self._monitor.connect("changed", self._on_file_changed)

    def _on_file_changed(self, _monitor, _file, _other, event) -> None:
        if DEBUG:
            print("file changed", event, flush=True)
        if event not in (
            Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.MOVED_OUT, Gio.FileMonitorEvent.RENAMED, Gio.FileMonitorEvent.MOVED_IN,
        ):
            return
        if self._monitor_source:
            GLib.source_remove(self._monitor_source)
        self._monitor_source = GLib.timeout_add(200, self._check_disk)

    def _stat(self) -> tuple[int, int] | None:
        p = self.path
        try:
            st = p.stat() if p else None
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size) if st else None

    def _record_disk_state(self) -> None:
        self._disk_state = self._stat()

    def _check_disk(self) -> bool:
        # GtkSourceFile's own check compares whole-second timestamps and so
        # misses quick successive writes (agents, formatters); compare
        # nanosecond mtime and size instead.
        self._monitor_source = 0
        current = self._stat()
        if DEBUG:
            print("check_disk", self._disk_state, current, flush=True)
        if current is None and self.path is not None:
            self.show_banner(_("The file was deleted or moved. Save to keep this document."), _("_Save"), "save")
            self.buffer.set_modified(True)
        elif current != self._disk_state:
            try:
                same = self.path.read_text(encoding="utf-8").rstrip("\n") == self.get_text().rstrip("\n")
            except (OSError, UnicodeDecodeError):
                same = False
            if same:
                self._disk_state = current
                return GLib.SOURCE_REMOVE
            # The saver must not refuse the next save on stale timestamps.
            self.source_file.check_file_on_disk()
            if not self.buffer.get_modified() and self.settings.props.auto_reload:
                self.reload()
                self.emit("toast", _("Reloaded “%s” — changed on disk") % self.props.title)
            else:
                self.show_banner(_("The file changed on disk."), _("_Reload"), "reload")
        return GLib.SOURCE_REMOVE

    def show_banner(self, text: str, button: str | None = None, action: str | None = None) -> None:
        self.banner.set_title(text)
        self.banner.set_button_label(button)
        self._banner_action = action
        self.banner.set_revealed(True)

    def hide_banner(self) -> None:
        self.banner.set_revealed(False)
        self._banner_action = None

    def _on_banner_clicked(self, _banner) -> None:
        action = self._banner_action
        self.hide_banner()
        if action == "reload":
            self.reload()
        elif action == "save":
            self.activate_action("win.save", None)

    # ------------------------------------------------------------------
    # Text changes → preview, outline

    def get_text(self) -> str:
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)

    def _on_changed(self, _buf) -> None:
        self._preview_dirty = True
        if self.preview_area.get_visible():
            self._schedule_render()
        if self._outline_source:
            GLib.source_remove(self._outline_source)
        self._outline_source = GLib.timeout_add(350, self._update_outline)

    def _schedule_render(self, immediate: bool = False) -> None:
        if self._render_source:
            GLib.source_remove(self._render_source)
        size = self.buffer.get_char_count()
        delay = 0 if immediate else min(900, 60 + size // 1500)
        self._render_source = GLib.timeout_add(delay, self._render_now)

    def _render_now(self) -> bool:
        self._render_source = 0
        if self.preview is not None:
            self._preview_dirty = False
            self.preview.render(self.get_text())
        return GLib.SOURCE_REMOVE

    def _update_outline(self) -> bool:
        self._outline_source = 0
        text = self.get_text()
        self.headings = mu.parse_headings(text)
        self.word_count = mu.word_count(text)
        self.emit("outline-changed")
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Navigation and scroll sync

    def cursor_position(self) -> tuple[int, int]:
        it = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        # Column counts tabs as tab-width wide, as the view displays them.
        return it.get_line() + 1, self.view.get_visual_column(it) + 1

    def goto_line(self, line: int, select: bool = False) -> None:
        line = max(0, min(line, self.buffer.get_line_count() - 1))
        ok, it = self.buffer.get_iter_at_line(line)
        self.buffer.place_cursor(it)
        if select:
            end = it.copy()
            end.forward_to_line_end()
            self.buffer.select_range(it, end)
        self._scroll_leader = "editor"
        mark = self.buffer.create_mark(None, it, True)
        self.view.scroll_to_mark(mark, 0.0, True, 0.0, 0.1)
        self.buffer.delete_mark(mark)
        if self.preview and self.preview_area.get_visible():
            self._suppress_preview_scroll_until = time.monotonic() + 0.3
            self.preview.scroll_to_line(line)
        self.focus_editor()

    def goto_anchor(self, anchor: str) -> bool:
        for h in self.headings or mu.parse_headings(self.get_text()):
            if h.anchor == anchor:
                self.goto_line(h.line)
                if self.preview:
                    self.preview.scroll_to_anchor(anchor)
                return True
        if self.preview:
            self.preview.scroll_to_anchor(anchor)
        return False

    def current_heading_index(self) -> int:
        line = self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_line()
        idx = -1
        for i, h in enumerate(self.headings):
            if h.line <= line:
                idx = i
            else:
                break
        return idx

    def _editor_top_line(self) -> float:
        vadj = self.scroller.get_vadjustment()
        y = vadj.get_value()
        it, line_top = self.view.get_line_at_y(int(y))
        _y, height = self.view.get_line_yrange(it)
        frac = (y - line_top) / height if height > 0 else 0.0
        return it.get_line() + max(0.0, min(1.0, frac))

    def _on_editor_scrolled(self, vadj) -> None:
        if time.monotonic() < self._suppress_editor_scroll_until:
            return
        self._scroll_leader = "editor"
        if not self.settings.props.sync_scroll or self.preview is None or self._view_mode != "split":
            return
        if not self._sync_source:
            self._sync_source = GLib.idle_add(self._sync_preview_to_editor)

    def _sync_preview_to_editor(self) -> bool:
        self._sync_source = 0
        if self.preview is None:
            return GLib.SOURCE_REMOVE
        vadj = self.scroller.get_vadjustment()
        self._suppress_preview_scroll_until = time.monotonic() + 0.3
        if vadj.get_value() >= vadj.get_upper() - vadj.get_page_size() - 1 and vadj.get_upper() > vadj.get_page_size():
            self.preview.scroll_to_line(self.buffer.get_line_count() + 1)
        else:
            self.preview.scroll_to_line(self._editor_top_line())
        return GLib.SOURCE_REMOVE

    def _on_preview_scrolled(self, _preview, line: float) -> None:
        if DEBUG:
            print("preview scrolled", line, time.monotonic() < self._suppress_preview_scroll_until, self._view_mode, flush=True)
        if time.monotonic() < self._suppress_preview_scroll_until:
            return
        self._scroll_leader = "preview"
        if not self.settings.props.sync_scroll or self._view_mode != "split":
            return
        ok, it = self.buffer.get_iter_at_line(int(line))
        y, height = self.view.get_line_yrange(it)
        target = y + (line - int(line)) * height
        self._suppress_editor_scroll_until = time.monotonic() + 0.2
        self.scroller.get_vadjustment().set_value(target)

    def _on_preview_rendered(self, _preview) -> None:
        if self._scroll_leader == "editor" and self.settings.props.sync_scroll and self._view_mode == "split":
            self._sync_preview_to_editor()

    def _on_reveal_line(self, _preview, line: int) -> None:
        if self._view_mode == "preview":
            return
        ok, it = self.buffer.get_iter_at_line(max(0, line))
        self.buffer.place_cursor(it)
        self.view.grab_focus()
        self.view.scroll_to_iter(it, 0.1, True, 0.0, 0.2)
