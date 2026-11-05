"""Find and replace bar for a GtkSourceView buffer."""

from __future__ import annotations

from gettext import gettext as _

from gi.repository import Gdk, GLib, Gtk, GtkSource


class SearchBar(Gtk.Revealer):
    __gtype_name__ = "GmeSearchBar"

    def __init__(self, view: GtkSource.View) -> None:
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.view = view
        self.buffer = view.get_buffer()
        self.search_settings = GtkSource.SearchSettings(wrap_around=True)
        self.context = GtkSource.SearchContext(buffer=self.buffer, settings=self.search_settings)
        self.context.set_highlight(False)
        self.context.connect("notify::occurrences-count", lambda *_: self._update_count())
        self.buffer.connect("mark-set", self._on_mark_set)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["toolbar", "gme-searchbar"])
        self.set_child(box)

        row = Gtk.Box(spacing=6)
        box.append(row)
        self.entry = Gtk.SearchEntry(hexpand=True, placeholder_text=_("Find"))
        self.entry.set_key_capture_widget(None)
        self.entry.connect("search-changed", self._on_search_changed)
        self.entry.connect("activate", lambda *_: self.next())
        self.entry.connect("stop-search", lambda *_: self.dismiss())
        self.entry.connect("next-match", lambda *_: self.next())
        self.entry.connect("previous-match", lambda *_: self.previous())
        overlay = Gtk.Overlay(hexpand=True)
        overlay.set_child(self.entry)
        self.count = Gtk.Label(css_classes=["dim-label", "numeric", "caption"], halign=Gtk.Align.END, margin_end=32, can_target=False)
        overlay.add_overlay(self.count)
        row.append(overlay)

        nav = Gtk.Box(css_classes=["linked"])
        prev_btn = Gtk.Button(icon_name="go-up-symbolic", tooltip_text=_("Previous Match (Shift+Ctrl+G)"))
        prev_btn.connect("clicked", lambda *_: self.previous())
        next_btn = Gtk.Button(icon_name="go-down-symbolic", tooltip_text=_("Next Match (Ctrl+G)"))
        next_btn.connect("clicked", lambda *_: self.next())
        nav.append(prev_btn)
        nav.append(next_btn)
        row.append(nav)

        self.case_btn = Gtk.ToggleButton(tooltip_text=_("Case Sensitive"), css_classes=["flat"])
        self.case_btn.set_child(Gtk.Label(label="Aa"))
        self.case_btn.bind_property("active", self.search_settings, "case-sensitive", 0)
        self.word_btn = Gtk.ToggleButton(tooltip_text=_("Match Whole Word"), css_classes=["flat"])
        self.word_btn.set_child(Gtk.Label(label="ab|"))
        self.word_btn.bind_property("active", self.search_settings, "at-word-boundaries", 0)
        self.regex_btn = Gtk.ToggleButton(tooltip_text=_("Regular Expression"), css_classes=["flat"])
        self.regex_btn.set_child(Gtk.Label(label=".*"))
        self.regex_btn.bind_property("active", self.search_settings, "regex-enabled", 0)
        for b in (self.case_btn, self.word_btn, self.regex_btn):
            b.connect("toggled", lambda *_: self._update_count())
            row.append(b)

        self.replace_toggle = Gtk.ToggleButton(icon_name="edit-find-replace-symbolic", tooltip_text=_("Replace (Ctrl+H)"), css_classes=["flat"])
        row.append(self.replace_toggle)
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text=_("Close"), css_classes=["flat", "circular"])
        close.connect("clicked", lambda *_: self.dismiss())
        row.append(close)

        self.replace_row = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.replace_toggle.bind_property("active", self.replace_row, "reveal-child", 0)
        rrow = Gtk.Box(spacing=6)
        self.replace_entry = Gtk.Entry(hexpand=True, placeholder_text=_("Replace"))
        self.replace_entry.connect("activate", lambda *_: self.replace_one())
        rrow.append(self.replace_entry)
        rbtn = Gtk.Button(label=_("_Replace"), use_underline=True)
        rbtn.connect("clicked", lambda *_: self.replace_one())
        rall = Gtk.Button(label=_("Replace _All"), use_underline=True)
        rall.connect("clicked", lambda *_: self.replace_all())
        rrow.append(rbtn)
        rrow.append(rall)
        self.replace_row.set_child(rrow)
        box.append(self.replace_row)

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        box.add_controller(key)
        self.error_label = Gtk.Label(css_classes=["error", "caption"], halign=Gtk.Align.START, visible=False)
        box.append(self.error_label)

    # ------------------------------------------------------------------

    def _on_key(self, _ctrl, keyval, _code, state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.dismiss()
            return True
        return False

    def present(self, replace: bool = False) -> None:
        b = self.buffer
        if b.get_has_selection():
            s, e = b.get_selection_bounds()
            text = b.get_text(s, e, True)
            if text and "\n" not in text:
                if self.regex_btn.get_active():
                    text = GLib.Regex.escape_string(text, -1)
                self.entry.set_text(text)
        self.replace_toggle.set_active(replace)
        self.set_reveal_child(True)
        self.context.set_highlight(True)
        self.entry.grab_focus()
        self.entry.select_region(0, -1)
        self._update_count()

    def dismiss(self) -> None:
        self.set_reveal_child(False)
        self.context.set_highlight(False)
        self.view.grab_focus()

    def _on_search_changed(self, entry) -> None:
        text = entry.get_text()
        self.search_settings.set_search_text(GtkSource.utils_unescape_search_text(text) if text and not self.regex_btn.get_active() else (text or None))
        if text:
            it = self.buffer.get_iter_at_mark(self.buffer.get_selection_bound())
            self._select_from(it, forward=True)
        self._update_count()

    def _select_from(self, it, forward: bool) -> None:
        if forward:
            found, s, e, _wrapped = self.context.forward(it)
        else:
            found, s, e, _wrapped = self.context.backward(it)
        if found:
            self.buffer.select_range(s, e)
            self.view.scroll_to_iter(s, 0.2, False, 0, 0)
        self._update_count()

    def next(self) -> None:
        if not self.search_settings.get_search_text():
            return
        if self.buffer.get_has_selection():
            it = self.buffer.get_selection_bounds()[1]
        else:
            it = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        self._select_from(it, True)

    def previous(self) -> None:
        if not self.search_settings.get_search_text():
            return
        it = self.buffer.get_selection_bounds()[0] if self.buffer.get_has_selection() else self.buffer.get_iter_at_mark(self.buffer.get_insert())
        self._select_from(it, False)

    def replace_one(self) -> None:
        if not self.buffer.get_has_selection():
            self.next()
            return
        s, e = self.buffer.get_selection_bounds()
        repl = self.replace_entry.get_text()
        try:
            self.context.replace(s, e, repl, -1)
        except GLib.Error:
            # Selection is not a match: find the next one first.
            self.next()
            return
        self.next()

    def replace_all(self) -> None:
        try:
            n = self.context.replace_all(self.replace_entry.get_text(), -1)
        except GLib.Error as exc:
            self._show_error(exc.message)
            return
        self.count.set_label(_("%d replaced") % n)

    def _on_mark_set(self, _buf, _it, mark) -> None:
        if mark == self.buffer.get_insert() and self.get_reveal_child():
            self._update_count()

    def _show_error(self, msg: str | None) -> None:
        self.error_label.set_visible(bool(msg))
        self.error_label.set_label(msg or "")

    def _update_count(self) -> None:
        err = self.context.get_regex_error()
        self._show_error(err.message if err else None)
        total = self.context.get_occurrences_count()
        if not self.search_settings.get_search_text():
            self.count.set_label("")
            return
        if total < 0:
            self.count.set_label("…")
            return
        pos = 0
        if self.buffer.get_has_selection():
            s, e = self.buffer.get_selection_bounds()
            pos = self.context.get_occurrence_position(s, e)
        self.count.set_label(_("%d of %d") % (pos, total) if pos > 0 else (_("%d matches") % total if total != 1 else _("1 match")))
