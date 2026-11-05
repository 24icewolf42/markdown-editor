"""Preferences dialog."""

from __future__ import annotations

from gettext import gettext as _

from gi.repository import Adw, GObject, Gtk, GtkSource, Pango

from .settings import Settings


def _switch(settings, key: str, title: str, subtitle: str | None = None) -> Adw.SwitchRow:
    row = Adw.SwitchRow(title=title, subtitle=subtitle or "")
    settings.bind_property(key, row, "active", GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
    return row


def _spin(settings, key: str, title: str, lo: int, hi: int, subtitle: str | None = None) -> Adw.SpinRow:
    row = Adw.SpinRow.new_with_range(lo, hi, 1)
    row.set_title(title)
    if subtitle:
        row.set_subtitle(subtitle)
    settings.bind_property(key, row, "value", GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
    return row


def _combo(settings, key: str, title: str, choices: list[tuple[str, str]], subtitle: str | None = None) -> Adw.ComboRow:
    row = Adw.ComboRow(title=title, subtitle=subtitle or "")
    row.set_model(Gtk.StringList.new([label for _v, label in choices]))
    values = [v for v, _l in choices]
    current = settings.get_property(key)
    row.set_selected(values.index(current) if current in values else 0)
    row.connect("notify::selected", lambda r, _p: settings.set_property(key, values[r.get_selected()]))
    return row


class PreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = "GmePreferencesDialog"

    def __init__(self) -> None:
        super().__init__(search_enabled=True)
        s = Settings.get()

        # Editor -----------------------------------------------------------
        page = Adw.PreferencesPage(title=_("Editor"), icon_name="document-edit-symbolic")
        self.add(page)

        appearance = Adw.PreferencesGroup(title=_("Appearance"))
        page.add(appearance)
        appearance.add(_combo(s, "color-scheme", _("Style"), [("default", _("Follow System")), ("light", _("Light")), ("dark", _("Dark"))]))
        schemes = GtkSource.StyleSchemeManager.get_default()
        ids = sorted({i[:-5] if i.endswith("-dark") else (i[:-6] if i.endswith("-light") else i) for i in schemes.get_scheme_ids()})
        names = []
        for i in ids:
            scheme = schemes.get_scheme(i) or schemes.get_scheme(i + "-dark") or schemes.get_scheme(i + "-light")
            names.append((i, scheme.get_name() if scheme else i))
        appearance.add(_combo(s, "style-scheme", _("Color Scheme"), names, _("Light and dark variants follow the style")))
        system_font = _switch(s, "use-system-font", _("Use System Font"))
        appearance.add(system_font)
        font_row = Adw.ActionRow(title=_("Editor Font"))
        font_button = Gtk.FontDialogButton(dialog=Gtk.FontDialog(title=_("Editor Font")), valign=Gtk.Align.CENTER, use_font=True, use_size=True)
        font_button.set_font_desc(Pango.FontDescription.from_string(s.props.custom_font))
        font_button.connect("notify::font-desc", lambda b, _p: setattr(s.props, "custom_font", b.get_font_desc().to_string()))
        font_row.add_suffix(font_button)
        font_row.set_activatable_widget(font_button)
        system_font.bind_property("active", font_row, "sensitive", GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.INVERT_BOOLEAN)
        appearance.add(font_row)

        display = Adw.PreferencesGroup(title=_("Display"))
        page.add(display)
        display.add(_switch(s, "show-line-numbers", _("Line Numbers")))
        display.add(_switch(s, "highlight-current-line", _("Highlight Current Line")))
        display.add(_switch(s, "highlight-matching-brackets", _("Highlight Matching Brackets")))
        display.add(_switch(s, "wrap-text", _("Wrap Text")))
        display.add(_switch(s, "show-map", _("Overview Map")))
        margin = _switch(s, "show-right-margin", _("Right Margin"))
        display.add(margin)
        margin_pos = _spin(s, "right-margin-position", _("Right Margin Position"), 20, 300)
        margin.bind_property("active", margin_pos, "sensitive", GObject.BindingFlags.SYNC_CREATE)
        display.add(margin_pos)

        behaviour = Adw.PreferencesGroup(title=_("Behavior"))
        page.add(behaviour)
        behaviour.add(_spin(s, "tab-width", _("Tab Width"), 1, 16))
        behaviour.add(_switch(s, "insert-spaces", _("Indent with Spaces")))
        behaviour.add(_switch(s, "auto-indent", _("Automatic Indentation")))
        behaviour.add(_switch(s, "smart-lists", _("Continue Lists"), _("Enter starts the next list item or quote line; Tab nests items")))
        behaviour.add(_switch(s, "spellcheck", _("Check Spelling"), _("Code blocks and URLs are not checked")))

        # Preview ------------------------------------------------------------
        page = Adw.PreferencesPage(title=_("Preview"), icon_name="view-reveal-symbolic")
        self.add(page)
        group = Adw.PreferencesGroup(title=_("Preview"))
        page.add(group)
        group.add(_combo(s, "view-mode", _("Default View"), [("split", _("Editor and Preview")), ("editor", _("Editor Only")), ("preview", _("Preview Only"))]))
        group.add(_combo(s, "preview-position", _("Preview Position"), [("right", _("Right of the Editor")), ("left", _("Left of the Editor"))]))
        group.add(_switch(s, "sync-scroll", _("Synchronize Scrolling"), _("Keep the preview at the part of the document being edited")))
        group.add(_spin(s, "preview-font-size", _("Text Size"), 9, 32, _("In pixels; zoom applies on top")))
        group.add(_switch(s, "preview-full-width", _("Full Width"), _("Use the whole pane instead of a readable line length")))

        # Files --------------------------------------------------------------
        page = Adw.PreferencesPage(title=_("Files"), icon_name="document-open-symbolic")
        self.add(page)
        group = Adw.PreferencesGroup(title=_("Files"))
        page.add(group)
        group.add(_switch(s, "restore-session", _("Restore Open Documents"), _("Reopen the documents of the last session on start")))
        group.add(_switch(s, "auto-reload", _("Reload Changed Files"), _("Reload files changed by other programs when they have no unsaved changes")))
