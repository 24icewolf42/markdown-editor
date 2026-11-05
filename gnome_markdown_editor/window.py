"""Main window: tabs, header bar, outline sidebar, actions."""

from __future__ import annotations

import html
import json
import os
import subprocess
from gettext import gettext as _
from pathlib import Path
from urllib.parse import unquote, urlsplit

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from .document import MARKDOWN_SUFFIXES, DocumentPage
from .preview import VENDOR_DIR, WEB_DIR
from .settings import Settings

FORMAT_ITEMS = [
    # (section, [(label, target, accel)])
    (None, [
        (_("Bold"), "bold", "<Control>b"),
        (_("Italic"), "italic", "<Control>i"),
        (_("Strikethrough"), "strikethrough", "<Control><Shift>x"),
        (_("Highlight"), "highlight", "<Control><Shift>h"),
        (_("Inline Code"), "code", "<Control>grave"),
        (_("Inline Math"), "inline-math", "<Control><Shift>m"),
    ]),
    (None, [
        (_("Link"), "link", "<Control>k"),
        (_("Image"), "image", "<Control><Shift>i"),
        (_("Insert Image File…"), "image-file", None),
        (_("Footnote"), "footnote", "<Control><Shift>f"),
    ]),
    (_("Headings"), [
        (_("Heading 1"), "heading-1", "<Control>1"),
        (_("Heading 2"), "heading-2", "<Control>2"),
        (_("Heading 3"), "heading-3", "<Control>3"),
        (_("Heading 4"), "heading-4", "<Control>4"),
        (_("Heading 5"), "heading-5", "<Control>5"),
        (_("Heading 6"), "heading-6", "<Control>6"),
        (_("Plain Paragraph"), "heading-0", "<Control>7"),
    ]),
    (None, [
        (_("Bulleted List"), "bullet", "<Control><Shift>8"),
        (_("Numbered List"), "numbered", "<Control><Shift>7"),
        (_("Task List"), "task", "<Control><Shift>9"),
        (_("Block Quote"), "quote", "<Control><Shift>period"),
    ]),
    (_("Insert"), [
        (_("Code Block"), "code-block", "<Control><Shift>k"),
        (_("Table"), "table", "<Control><Shift>t"),
        (_("Mermaid Flowchart"), "mermaid", None),
        (_("Mermaid Sequence Diagram"), "sequence", None),
        (_("Math Block"), "math", None),
        (_("Collapsible Section"), "details", None),
        (_("Image Carousel"), "carousel", None),
        (_("Horizontal Rule"), "hr", None),
        (_("Front Matter"), "front-matter", None),
        (_("Table of Contents"), "toc", None),
    ]),
    (_("Alert"), [
        (_("Note"), "alert-note", None),
        (_("Tip"), "alert-tip", None),
        (_("Important"), "alert-important", None),
        (_("Warning"), "alert-warning", None),
        (_("Caution"), "alert-caution", None),
    ]),
]

WIN_ACCELS = {
    "win.new-tab": ["<Control>t"],
    "win.open": ["<Control>o"],
    "win.save": ["<Control>s"],
    "win.save-as": ["<Control><Shift>s"],
    "win.save-all": ["<Control><Alt>s"],
    "win.close-tab": ["<Control>w"],
    "win.reload": ["<Control><Shift>r"],
    "win.print": ["<Control>p"],
    "win.find": ["<Control>f"],
    "win.find-replace": ["<Control>h"],
    "win.find-next": ["<Control>g"],
    "win.find-previous": ["<Control><Shift>g"],
    "win.goto-line": ["<Control>l"],
    "win.format-table": ["<Control><Alt>t"],
    "win.view-mode::editor": ["<Control><Alt>1"],
    "win.view-mode::split": ["<Control><Alt>2"],
    "win.view-mode::preview": ["<Control><Alt>3"],
    "win.toggle-preview": ["<Control><Shift>p"],
    "win.show-outline": ["F9"],
    "win.zoom-in": ["<Control>plus", "<Control>equal", "<Control>KP_Add"],
    "win.zoom-out": ["<Control>minus", "<Control>KP_Subtract"],
    "win.zoom-reset": ["<Control>0", "<Control>KP_0"],
    "win.fullscreen": ["F11"],
}


def _find_repo_root(start: Path) -> Path | None:
    for p in [start, *start.parents]:
        if (p / ".git").exists():
            return p
    return None


class OutlinePanel(Gtk.Box):
    """Heading outline of the current document."""

    __gtype_name__ = "GmeOutlinePanel"
    __gsignals__ = {"heading-activated": (GObject.SignalFlags.RUN_LAST, None, (int,))}

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._headings = []
        self._rows: dict[int, Gtk.ListBoxRow] = {}
        self._filter = ""
        self.search = Gtk.SearchEntry(placeholder_text=_("Filter headings"), margin_start=8, margin_end=8, margin_bottom=6)
        self.search.connect("search-changed", self._on_filter)
        self.append(self.search)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["navigation-sidebar"])
        self.list.connect("row-activated", lambda _l, row: self.emit("heading-activated", row.heading_index))
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.list)
        self.stack = Gtk.Stack()
        self.stack.add_named(scroller, "list")
        self.stack.add_named(
            Adw.StatusPage(icon_name="view-list-bullet-symbolic", title=_("No Headings"), css_classes=["compact"]),
            "empty",
        )
        self.append(self.stack)

    def _on_filter(self, entry) -> None:
        self._filter = entry.get_text().lower()
        self.set_headings(self._headings, force=True)

    def set_headings(self, headings, force: bool = False) -> None:
        # Rows only show level and title; line numbers are looked up through
        # the index, so typing that only shifts lines rebuilds nothing.
        same = len(headings) == len(self._headings) and all(
            a.level == b.level and a.title == b.title for a, b in zip(headings, self._headings)
        )
        self._headings = list(headings)
        if same and not force:
            return
        self.list.remove_all()
        self._rows = {}
        min_level = min((h.level for h in headings), default=1)
        shown = 0
        for i, h in enumerate(headings):
            if self._filter and self._filter not in h.title.lower():
                continue
            label = Gtk.Label(
                label=h.title or _("(untitled)"),
                xalign=0,
                ellipsize=3,  # Pango.EllipsizeMode.END
                margin_start=6 + 14 * (h.level - min_level),
                tooltip_text=h.title,
            )
            if h.level == min_level:
                label.add_css_class("heading")
            row = Gtk.ListBoxRow(child=label)
            row.heading_index = i
            self._rows[i] = row
            self.list.append(row)
            shown += 1
        self.stack.set_visible_child_name("list" if shown else "empty")

    def select_index(self, index: int) -> None:
        row = self._rows.get(index)
        if row is None:
            self.list.unselect_all()
        elif self.list.get_selected_row() is not row:
            self.list.select_row(row)


class Window(Adw.ApplicationWindow):
    __gtype_name__ = "GmeWindow"

    def __init__(self, application) -> None:
        super().__init__(application=application)
        self.settings = Settings.get()
        self.set_default_size(self.settings.props.window_width, self.settings.props.window_height)
        if self.settings.props.window_maximized:
            self.maximize()
        self.set_title(_("Markdown Editor"))
        self._page_handlers: list[tuple[GObject.Object, int]] = []
        self._closing_confirmed = False

        # Tabs ------------------------------------------------------------
        self.tab_view = Adw.TabView()
        self.tab_view.connect("close-page", self._on_close_page)
        self.tab_view.connect("notify::selected-page", self._on_selected_page)
        self.tab_view.connect("page-detached", lambda *_: self._update_empty_state())
        self.tab_view.connect("page-attached", lambda *_: self._update_empty_state())
        self.tab_view.connect("create-window", self._on_create_window)
        self.tab_bar = Adw.TabBar(view=self.tab_view, autohide=True)

        # Header bar ------------------------------------------------------
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title=_("Markdown Editor"))
        header.set_title_widget(self.window_title)

        self.open_button = Adw.SplitButton(label=_("_Open"), use_underline=True, action_name="win.open", tooltip_text=_("Open a File (Ctrl+O)"))
        self.recent_popover = Gtk.Popover()
        self.recent_popover.connect("show", self._fill_recent)
        self.open_button.set_popover(self.recent_popover)
        header.pack_start(self.open_button)
        header.pack_start(Gtk.Button(icon_name="tab-new-symbolic", action_name="win.new-tab", tooltip_text=_("New Document (Ctrl+T)")))

        self.sidebar_button = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text=_("Outline (F9)"))
        header.pack_start(self.sidebar_button)

        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=self._build_main_menu(), primary=True, tooltip_text=_("Main Menu"))
        header.pack_end(menu_button)

        self.view_toggle = Adw.ToggleGroup(css_classes=["round"], tooltip_text=_("View Mode"))
        for name, icon, tip in (
            ("editor", "document-edit-symbolic", _("Editor Only (Ctrl+Alt+1)")),
            ("split", "view-dual-symbolic", _("Editor and Preview (Ctrl+Alt+2)")),
            ("preview", "view-reveal-symbolic", _("Preview Only (Ctrl+Alt+3)")),
        ):
            self.view_toggle.add(Adw.Toggle(name=name, icon_name=icon, tooltip=tip))
        self.view_toggle.set_active_name(self.settings.props.view_mode)
        self.view_toggle.connect("notify::active-name", self._on_view_toggle)
        header.pack_end(self.view_toggle)

        format_button = Gtk.MenuButton(icon_name="format-text-bold-symbolic", menu_model=self._build_format_menu(), tooltip_text=_("Format"))
        header.pack_end(format_button)
        tab_button = Adw.TabButton(view=self.tab_view, action_name="overview.open", tooltip_text=_("View Open Documents"))
        header.pack_end(tab_button)

        # Status bar ------------------------------------------------------
        self.position_label = Gtk.Label(css_classes=["dim-label", "caption", "numeric"])
        self.words_label = Gtk.Label(css_classes=["dim-label", "caption", "numeric"])
        self.mode_label = Gtk.Label(css_classes=["dim-label", "caption"])
        status = Gtk.Box(spacing=18, margin_start=12, margin_end=12, margin_top=3, margin_bottom=3, css_classes=["gme-statusbar"])
        status.append(self.words_label)
        status.append(Gtk.Box(hexpand=True))
        status.append(self.mode_label)
        status.append(self.position_label)

        # Empty state -----------------------------------------------------
        empty = Adw.StatusPage(
            icon_name="io.github.gnome_markdown_editor",
            title=_("No Open Documents"),
            description=_("Create a new document or open a Markdown file. You can also drop files here."),
        )
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        buttons.append(Gtk.Button(label=_("_New Document"), use_underline=True, action_name="win.new-tab", css_classes=["pill"]))
        buttons.append(Gtk.Button(label=_("_Open…"), use_underline=True, action_name="win.open", css_classes=["pill", "suggested-action"]))
        empty.set_child(buttons)

        self.content_stack = Gtk.Stack()
        self.content_stack.add_named(self.tab_view, "tabs")
        self.content_stack.add_named(empty, "empty")

        toolbar_view = Adw.ToolbarView(top_bar_style=Adw.ToolbarStyle.RAISED_BORDER)
        toolbar_view.add_top_bar(header)
        toolbar_view.add_top_bar(self.tab_bar)
        toolbar_view.set_content(self.content_stack)
        self.status_bar = status
        toolbar_view.add_bottom_bar(status)

        # Outline sidebar -------------------------------------------------
        self.outline = OutlinePanel()
        self.outline.connect("heading-activated", self._on_heading_activated)
        sidebar_view = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        sidebar_header.set_title_widget(Adw.WindowTitle(title=_("Outline")))
        sidebar_view.add_top_bar(sidebar_header)
        sidebar_view.set_content(self.outline)

        self.split_view = Adw.OverlaySplitView(sidebar=sidebar_view, content=toolbar_view, min_sidebar_width=200, max_sidebar_width=340)
        self.split_view.set_show_sidebar(self.settings.props.show_outline)
        self.split_view.bind_property("show-sidebar", self.sidebar_button, "active", GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        self.split_view.connect("notify::show-sidebar", lambda sv, _p: setattr(self.settings.props, "show_outline", sv.get_show_sidebar()) if not sv.get_collapsed() else None)

        self.overview = Adw.TabOverview(view=self.tab_view, child=self.split_view, enable_new_tab=True)
        self.overview.connect("create-tab", lambda *_: self.new_page())
        self.toast_overlay = Adw.ToastOverlay(child=self.overview)
        self.set_content(self.toast_overlay)

        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        breakpoint.add_setter(self.split_view, "collapsed", True)
        self.add_breakpoint(breakpoint)

        # Drag and drop ---------------------------------------------------
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        self._install_actions()
        self.connect("close-request", self._on_close_request)
        self._update_empty_state()

    # ------------------------------------------------------------------
    # Menus

    def _build_main_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        s = Gio.Menu()
        s.append(_("New _Window"), "app.new-window")
        menu.append_section(None, s)
        s = Gio.Menu()
        s.append(_("_Save As…"), "win.save-as")
        s.append(_("Save A_ll"), "win.save-all")
        s.append(_("_Reload"), "win.reload")
        menu.append_section(None, s)
        s = Gio.Menu()
        s.append(_("_Find and Replace…"), "win.find-replace")
        s.append(_("_Go to Line…"), "win.goto-line")
        s.append(_("Format _Table"), "win.format-table")
        menu.append_section(None, s)
        s = Gio.Menu()
        s.append(_("_Print…"), "win.print")
        s.append(_("_Export as HTML…"), "win.export-html")
        s.append(_("Open Containing _Folder"), "win.open-folder")
        menu.append_section(None, s)
        view = Gio.Menu()
        view.append(_("Show _Outline"), "win.show-outline")
        view.append(_("_Synchronize Scrolling"), "win.sync-scroll")
        view.append(_("_Wrap Text"), "win.wrap-text")
        view.append(_("Show _Line Numbers"), "win.show-line-numbers")
        view.append(_("Show Overview _Map"), "win.show-map")
        view.append(_("_Full-Width Preview"), "win.preview-full-width")
        zoom = Gio.Menu()
        zoom.append(_("Zoom _In"), "win.zoom-in")
        zoom.append(_("Zoom _Out"), "win.zoom-out")
        zoom.append(_("_Reset Zoom"), "win.zoom-reset")
        view.append_section(None, zoom)
        s = Gio.Menu()
        s.append_submenu(_("_View"), view)
        menu.append_section(None, s)
        s = Gio.Menu()
        s.append(_("_Preferences"), "app.preferences")
        s.append(_("_Keyboard Shortcuts"), "app.shortcuts")
        s.append(_("_About Markdown Editor"), "app.about")
        menu.append_section(None, s)
        return menu

    def _build_format_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        for section, items in FORMAT_ITEMS:
            sub = Gio.Menu()
            for label, target, _accel in items:
                sub.append(label, f"win.format::{target}")
            if section:
                holder = Gio.Menu()
                holder.append_submenu(section, sub)
                menu.append_section(None, holder)
            else:
                menu.append_section(None, sub)
        return menu

    def _fill_recent(self, popover) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list-separate", "navigation-sidebar"])
        items = [
            i for i in Gtk.RecentManager.get_default().get_items()
            if i.get_uri().startswith("file://") and Path(unquote(urlsplit(i.get_uri()).path)).suffix.lower() in MARKDOWN_SUFFIXES and i.exists()
        ]
        items.sort(key=lambda i: i.get_modified().to_unix() if i.get_modified() else 0, reverse=True)
        for item in items[:15]:
            path = Path(unquote(urlsplit(item.get_uri()).path))
            row = Adw.ActionRow(title=GLib.markup_escape_text(path.name), subtitle=GLib.markup_escape_text(str(path.parent).replace(str(Path.home()), "~", 1)), activatable=True)
            row.uri = item.get_uri()
            listbox.append(row)
        listbox.connect("row-activated", lambda _l, row: (popover.popdown(), self.open_file(Gio.File.new_for_uri(row.uri))))
        if items:
            scroller = Gtk.ScrolledWindow(propagate_natural_height=True, max_content_height=480, min_content_width=340, hscrollbar_policy=Gtk.PolicyType.NEVER)
            scroller.set_child(listbox)
            box.append(Gtk.Label(label=_("Recent Documents"), xalign=0, css_classes=["heading"], margin_start=6))
            box.append(scroller)
        else:
            box.append(Gtk.Label(label=_("No recent documents"), css_classes=["dim-label"], margin_top=12, margin_bottom=12, margin_start=24, margin_end=24))
        popover.set_child(box)

    # ------------------------------------------------------------------
    # Actions

    def _install_actions(self) -> None:
        def add(name, cb, param=None):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new(param) if param else None)
            action.connect("activate", cb)
            self.add_action(action)
            return action

        add("new-tab", lambda *_: self.new_page())
        add("open", lambda *_: self.open_dialog())
        add("save", lambda *_: self.save_current())
        add("save-as", lambda *_: self.save_current(save_as=True))
        add("save-all", lambda *_: self.save_all())
        add("close-tab", lambda *_: self._close_current())
        add("reload", lambda *_: self._reload_current())
        add("print", lambda *_: self.print_current())
        add("export-html", lambda *_: self.export_html())
        add("open-folder", lambda *_: self._open_folder())
        add("find", lambda *_: self._with_page(lambda p: (self._ensure_editor_visible(p), p.search.present(False))))
        add("find-replace", lambda *_: self._with_page(lambda p: (self._ensure_editor_visible(p), p.search.present(True))))
        add("find-next", lambda *_: self._with_page(lambda p: p.search.next()))
        add("find-previous", lambda *_: self._with_page(lambda p: p.search.previous()))
        add("goto-line", lambda *_: self.goto_line_dialog())
        add("format-table", lambda *_: self._with_page(lambda p: p.editing.format_table() or self.toast(_("The cursor is not in a table"))))
        add("format", self._on_format, "s")
        add("toggle-preview", lambda *_: self._toggle_preview())
        add("zoom-in", lambda *_: self._zoom(1))
        add("zoom-out", lambda *_: self._zoom(-1))
        add("zoom-reset", lambda *_: self._zoom(0))
        add("fullscreen", lambda *_: self.unfullscreen() if self.is_fullscreen() else self.fullscreen())

        view_mode = Gio.SimpleAction.new_stateful("view-mode", GLib.VariantType.new("s"), GLib.Variant("s", self.settings.props.view_mode))
        view_mode.connect("activate", lambda a, v: self.set_view_mode(v.get_string()))
        self.add_action(view_mode)
        self._view_mode_action = view_mode

        outline = Gio.PropertyAction.new("show-outline", self.split_view, "show-sidebar")
        self.add_action(outline)
        for key in ("sync-scroll", "wrap-text", "show-line-numbers", "show-map", "preview-full-width"):
            self.add_action(Gio.PropertyAction.new(key, self.settings, key.replace("-", "_")))

        app = self.get_application()
        for action, accels in WIN_ACCELS.items():
            app.set_accels_for_action(action, accels)
        for _section, items in FORMAT_ITEMS:
            for _label, target, accel in items:
                if accel:
                    app.set_accels_for_action(f"win.format::{target}", [accel])

    def _with_page(self, fn):
        page = self.current_page
        if page is not None:
            fn(page)

    def _ensure_editor_visible(self, page: DocumentPage) -> None:
        if page.view_mode == "preview":
            self.set_view_mode("split")

    def _on_format(self, _action, param) -> None:
        page = self.current_page
        if page is None:
            return
        self._ensure_editor_visible(page)
        ed = page.editing
        t = param.get_string()
        simple = {
            "bold": ed.bold, "italic": ed.italic, "strikethrough": ed.strikethrough, "highlight": ed.highlight,
            "code": ed.inline_code, "inline-math": ed.inline_math, "link": ed.link,
            "image": lambda: ed.link(image=True), "code-block": ed.code_block, "toc": ed.insert_toc,
            "image-file": lambda: self._insert_image_file(page),
        }
        if t in simple:
            simple[t]()
        elif t.startswith("heading-"):
            ed.heading(int(t.split("-")[1]))
        elif t in ("bullet", "numbered", "task", "quote"):
            ed.toggle_line_prefix(t)
        else:
            ed.insert_template(t)
        page.view.grab_focus()

    def _insert_image_file(self, page: DocumentPage) -> None:
        dialog = Gtk.FileDialog(title=_("Insert Image"))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        f = Gtk.FileFilter(name=_("Images"))
        f.add_mime_type("image/*")
        filters.append(f)
        dialog.set_filters(filters)
        if page.directory:
            dialog.set_initial_folder(Gio.File.new_for_path(str(page.directory)))

        def done(d, res):
            try:
                file = d.open_finish(res)
            except GLib.Error:
                return
            path = Path(file.get_path())
            ref = os.path.relpath(path, page.directory) if page.directory else str(path)
            page.editing.insert_image_path(ref, path.stem.replace("-", " ").replace("_", " "))

        dialog.open(self, None, done)

    # ------------------------------------------------------------------
    # Pages

    @property
    def current_page(self) -> DocumentPage | None:
        tp = self.tab_view.get_selected_page()
        return tp.get_child() if tp else None

    def pages(self) -> list[DocumentPage]:
        return [self.tab_view.get_nth_page(i).get_child() for i in range(self.tab_view.get_n_pages())]

    def _add_page(self, page: DocumentPage, select: bool = True) -> Adw.TabPage:
        tp = self.tab_view.append(page)
        page.set_view_mode(self.settings.props.view_mode if self.current_page is None else self.current_page.view_mode)

        def update_title(*_):
            t = page.props.title
            tp.set_title(("• " + t) if page.props.modified else t)
            tp.set_tooltip(GLib.markup_escape_text(str(page.path) if page.path else page.props.title))
            if self.current_page is page:
                self._update_title()

        page.connect("notify::title", update_title)
        page.connect("notify::modified", update_title)
        page.bind_property("loading", tp, "loading", GObject.BindingFlags.SYNC_CREATE)
        page.connect("open-link", self._on_open_link)
        page.connect("toast", lambda _p, msg: self.toast(msg))
        update_title()
        if select:
            self.tab_view.set_selected_page(tp)
        return tp

    def new_page(self) -> DocumentPage:
        page = DocumentPage()
        self._add_page(page)
        GLib.idle_add(lambda: (page.focus_editor(), False)[1])
        return page

    def find_page(self, file: Gio.File) -> DocumentPage | None:
        for p in self.pages():
            if p.file is not None and p.file.equal(file):
                return p
        return None

    def open_file(self, file: Gio.File, anchor: str | None = None, line: int | None = None) -> DocumentPage:
        page = self.find_page(file)
        if page is None:
            current = self.current_page
            if current is not None and current.is_empty_untitled():
                page = current
                page.load(file)
            else:
                page = DocumentPage(file)
                self._add_page(page)
        self.tab_view.set_selected_page(self.tab_view.get_page(page))
        if anchor or line is not None:
            def go():
                if anchor:
                    page.goto_anchor(anchor)
                else:
                    page.goto_line(line)

            if page.props.loading:
                handler: list[int] = []

                def once(*_):
                    page.disconnect(handler[0])
                    go()

                handler.append(page.connect("loaded", once))
            else:
                go()
        return page

    def open_dialog(self) -> None:
        dialog = Gtk.FileDialog(title=_("Open Files"))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        md = Gtk.FileFilter(name=_("Markdown Documents"))
        md.add_mime_type("text/markdown")
        md.add_mime_type("text/x-markdown")
        for suffix in sorted(MARKDOWN_SUFFIXES):
            md.add_suffix(suffix.lstrip("."))
        text = Gtk.FileFilter(name=_("All Text Files"))
        text.add_mime_type("text/*")
        anything = Gtk.FileFilter(name=_("All Files"))
        anything.add_pattern("*")
        for f in (md, text, anything):
            filters.append(f)
        dialog.set_filters(filters)
        dialog.set_default_filter(md)
        page = self.current_page
        if page is not None and page.directory:
            dialog.set_initial_folder(Gio.File.new_for_path(str(page.directory)))

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            for i in range(files.get_n_items()):
                self.open_file(files.get_item(i))

        dialog.open_multiple(self, None, done)

    def save_page(self, page: DocumentPage, callback=None, save_as: bool = False) -> None:
        if page.is_untitled or save_as:
            dialog = Gtk.FileDialog(title=_("Save As"))
            name = page.file.get_basename() if page.file else self._suggest_name(page)
            dialog.set_initial_name(name)
            if page.directory:
                dialog.set_initial_folder(Gio.File.new_for_path(str(page.directory)))
            elif self.current_page and self.current_page.directory:
                dialog.set_initial_folder(Gio.File.new_for_path(str(self.current_page.directory)))

            def done(d, res):
                try:
                    file = d.save_finish(res)
                except GLib.Error:
                    if callback:
                        callback(False)
                    return
                page.save(file, callback)

            dialog.save(self, None, done)
        else:
            page.save(None, callback)

    @staticmethod
    def _suggest_name(page: DocumentPage) -> str:
        for h in page.headings:
            if h.title:
                stem = "".join(c if c.isalnum() or c in "-_ " else "" for c in h.title).strip().replace(" ", "-")
                if stem:
                    return stem[:60] + ".md"
        return _("Untitled") + ".md"

    def save_current(self, save_as: bool = False) -> None:
        page = self.current_page
        if page is not None:
            self.save_page(page, save_as=save_as)

    def save_all(self, callback=None) -> None:
        pending = [p for p in self.pages() if p.props.modified or (p.is_untitled and not p.is_empty_untitled())]

        def step(ok=True):
            if not ok:
                if callback:
                    callback(False)
                return
            if not pending:
                if callback:
                    callback(True)
                return
            page = pending.pop(0)
            self.tab_view.set_selected_page(self.tab_view.get_page(page))
            self.save_page(page, step)

        step()

    def _reload_current(self) -> None:
        page = self.current_page
        if page is None or page.is_untitled:
            return
        if page.props.modified:
            dialog = Adw.AlertDialog(heading=_("Discard Changes?"), body=_("Reloading “%s” discards your unsaved changes.") % page.props.title)
            dialog.add_response("cancel", _("_Cancel"))
            dialog.add_response("reload", _("_Reload"))
            dialog.set_response_appearance("reload", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect("response", lambda _d, r: page.reload() if r == "reload" else None)
            dialog.present(self)
        else:
            page.reload()

    def _close_current(self) -> None:
        tp = self.tab_view.get_selected_page()
        if tp is not None:
            self.tab_view.close_page(tp)

    def _on_close_page(self, view, tp) -> bool:
        page: DocumentPage = tp.get_child()
        if not page.props.modified or (page.is_untitled and page.buffer.get_char_count() == 0):
            page.disconnect_all()
            view.close_page_finish(tp, True)
            return Gdk.EVENT_STOP
        dialog = Adw.AlertDialog(
            heading=_("Save Changes?"),
            body=_("“%s” contains unsaved changes. Changes that are not saved are lost permanently.") % page.props.title,
        )
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("discard", _("_Discard"))
        dialog.add_response("save", _("_Save"))
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def on_response(_d, response):
            if response == "discard":
                page.disconnect_all()
                view.close_page_finish(tp, True)
            elif response == "save":
                def saved(ok):
                    if ok:
                        page.disconnect_all()
                    view.close_page_finish(tp, ok)

                self.save_page(page, saved)
            else:
                view.close_page_finish(tp, False)

        dialog.connect("response", on_response)
        dialog.present(self)
        return Gdk.EVENT_STOP

    def _on_create_window(self, _view):
        win = Window(self.get_application())
        win.present()
        return win.tab_view

    def _update_empty_state(self) -> None:
        has = self.tab_view.get_n_pages() > 0
        self.content_stack.set_visible_child_name("tabs" if has else "empty")
        self.status_bar.set_visible(has)
        for name in ("save", "save-as", "close-tab", "reload", "print", "export-html", "find", "find-replace",
                     "find-next", "find-previous", "goto-line", "format-table", "format", "open-folder", "toggle-preview"):
            action = self.lookup_action(name)
            if action is not None:
                action.set_enabled(has)
        self.view_toggle.set_sensitive(has)
        if not has:
            self._update_title()
            self.outline.set_headings([])

    # ------------------------------------------------------------------
    # Current page tracking

    def _on_selected_page(self, *_):
        for obj, hid in self._page_handlers:
            obj.disconnect(hid)
        self._page_handlers.clear()
        page = self.current_page
        self._update_title()
        if page is None:
            return
        self._page_handlers.append((page, page.connect("outline-changed", lambda *_: self._update_outline())))
        self._page_handlers.append((page, page.connect("cursor-moved", lambda *_: self._update_position())))
        self._update_outline()
        self._update_position()
        self.view_toggle.set_active_name(page.view_mode)
        self._view_mode_action.set_state(GLib.Variant("s", page.view_mode))
        GLib.idle_add(lambda: (page.focus_editor(), False)[1])

    def _update_title(self) -> None:
        page = self.current_page
        if page is None:
            self.window_title.set_title(_("Markdown Editor"))
            self.window_title.set_subtitle("")
            self.set_title(_("Markdown Editor"))
            return
        title = ("• " if page.props.modified else "") + page.props.title
        self.window_title.set_title(title)
        self.window_title.set_subtitle(page.props.subtitle)
        self.set_title(f"{page.props.title} — {_('Markdown Editor')}")

    def _update_outline(self) -> None:
        page = self.current_page
        if page is None:
            return
        self.outline.set_headings(page.headings)
        self.outline.select_index(page.current_heading_index())
        n = page.word_count
        self.words_label.set_label(_("%d words") % n if n != 1 else _("1 word"))

    def _update_position(self) -> None:
        page = self.current_page
        if page is None:
            return
        line, col = page.cursor_position()
        self.position_label.set_label(_("Ln %d, Col %d") % (line, col))
        if page.buffer.get_has_selection():
            s, e = page.buffer.get_selection_bounds()
            self.mode_label.set_label(_("%d selected") % (e.get_offset() - s.get_offset()))
        else:
            self.mode_label.set_label("")
        self.outline.select_index(page.current_heading_index())

    def _on_heading_activated(self, _panel, index: int) -> None:
        page = self.current_page
        if page is None or index >= len(page.headings):
            return
        h = page.headings[index]
        page.goto_line(h.line)
        if page.preview is not None:
            page.preview.scroll_to_anchor(h.anchor)
        if self.split_view.get_collapsed():
            self.split_view.set_show_sidebar(False)

    # ------------------------------------------------------------------
    # View mode

    def set_view_mode(self, mode: str) -> None:
        page = self.current_page
        if page is not None:
            page.set_view_mode(mode)
        self.settings.props.view_mode = mode
        self._view_mode_action.set_state(GLib.Variant("s", mode))
        if self.view_toggle.get_active_name() != mode:
            self.view_toggle.set_active_name(mode)

    def _on_view_toggle(self, group, _pspec) -> None:
        mode = group.get_active_name()
        if mode and (self.current_page is None or self.current_page.view_mode != mode):
            self.set_view_mode(mode)

    def _toggle_preview(self) -> None:
        page = self.current_page
        if page is not None:
            self.set_view_mode("editor" if page.view_mode == "split" else "split")

    def _zoom(self, delta: int) -> None:
        s = self.settings.props
        s.zoom = 0 if delta == 0 else max(-8, min(24, s.zoom + delta))

    # ------------------------------------------------------------------
    # Links from the preview

    def _on_open_link(self, page: DocumentPage, href: str) -> None:
        parts = urlsplit(href)
        scheme = parts.scheme.lower()
        if scheme in ("http", "https", "mailto", "ftp", "tel", "xmpp", "irc"):
            Gtk.UriLauncher.new(href).launch(self, None, None)
            return
        if scheme != "file":
            self.toast(_("Unsupported link: %s") % href)
            return
        path = Path(unquote(parts.path))
        anchor = unquote(parts.fragment) if parts.fragment else None
        if page.path is not None and path == page.path:
            if anchor:
                page.goto_anchor(anchor)
            return
        target = self._resolve_local(path, page)
        if target is None:
            self.toast(_("Not found: %s") % str(path).replace(str(Path.home()), "~", 1))
            return
        if target.is_dir():
            Gtk.FileLauncher.new(Gio.File.new_for_path(str(target))).launch(self, None, None)
        elif target.suffix.lower() in MARKDOWN_SUFFIXES:
            self.open_file(Gio.File.new_for_path(str(target)), anchor=anchor)
        else:
            Gtk.FileLauncher.new(Gio.File.new_for_path(str(target))).launch(self, None, None)

    @staticmethod
    def _resolve_local(path: Path, page: DocumentPage) -> Path | None:
        """Find the file a link means, including Hugo-style pretty URLs."""
        def variants(p: Path):
            yield p
            yield p.with_name(p.name + ".md") if p.name else p
            yield p / "_index.md"
            yield p / "index.md"
            yield p / "README.md"

        candidates = list(variants(path))
        # Site-absolute links (/services/Foo/) resolve against the repository.
        if page.directory is not None:
            root = _find_repo_root(page.directory)
            if root is not None and not str(path).startswith(str(root)):
                rel = str(path).lstrip("/")
                for base in (root, root / "content"):
                    candidates.extend(variants(base / rel))
        for c in candidates:
            if c.is_file():
                return c
        if path.is_dir():
            return path
        return None

    # ------------------------------------------------------------------
    # Dialogs

    def goto_line_dialog(self) -> None:
        page = self.current_page
        if page is None:
            return
        self._ensure_editor_visible(page)
        line, _col = page.cursor_position()
        dialog = Adw.AlertDialog(heading=_("Go to Line"))
        entry = Gtk.SpinButton.new_with_range(1, max(1, page.buffer.get_line_count()), 1)
        entry.set_value(line)
        entry.set_activates_default(True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("go", _("_Go"))
        dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("go")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: page.goto_line(int(entry.get_value()) - 1) if r == "go" else None)
        dialog.present(self)
        GLib.idle_add(lambda: (entry.grab_focus(), False)[1])

    def _with_rendered_preview(self, page: DocumentPage, fn) -> None:
        """Run ``fn(preview)`` once the preview shows the current text and all diagrams.

        Runs exactly once: after the diagrams are drawn, or after a timeout if
        the page never reports back (reload, web process crash).
        """
        preview = page._ensure_preview()
        handlers: list[int] = []
        state = {"done": False, "timer": 0}

        def finish(*_):
            if state["done"]:
                return
            state["done"] = True
            for hid in handlers:
                preview.disconnect(hid)
            if state["timer"]:
                GLib.source_remove(state["timer"])
            fn(preview)

        def on_timeout():
            state["timer"] = 0
            finish()
            return GLib.SOURCE_REMOVE

        def draw_all(*_):
            # Only after the current text is on the page (a patch may have
            # needed a full resync first).
            preview.disconnect(handlers.pop(0))
            preview.render_all_diagrams()

        handlers.append(preview.connect("diagrams-complete", finish))
        state["timer"] = GLib.timeout_add_seconds(60, on_timeout)
        if page._preview_dirty or not preview._ready:
            handlers.insert(0, preview.connect("rendered", draw_all))
            page._render_now()
            if preview._ready and not preview.last_render_posted:
                # The page already had this text (e.g. an edit was undone).
                preview.disconnect(handlers.pop(0))
                preview.render_all_diagrams()
        else:
            preview.render_all_diagrams()

    def print_current(self) -> None:
        page = self.current_page
        if page is not None:
            self._with_rendered_preview(page, lambda pv: pv.print_document(self))

    def export_html(self) -> None:
        page = self.current_page
        if page is None:
            return

        def got_html(body):
            if body is None:
                self.toast(_("The preview is not ready yet"))
                return
            dialog = Gtk.FileDialog(title=_("Export as HTML"))
            stem = page.path.stem if page.path else self._suggest_name(page)[:-3]
            dialog.set_initial_name(stem + ".html")
            if page.directory:
                dialog.set_initial_folder(Gio.File.new_for_path(str(page.directory)))

            def done(d, res):
                try:
                    file = d.save_finish(res)
                except GLib.Error:
                    return
                try:
                    Path(file.get_path()).write_text(self._standalone_html(page, body), encoding="utf-8")
                except OSError as exc:
                    self.toast(_("Export failed: %s") % exc.strerror)
                    return
                toast = Adw.Toast(title=_("Exported “%s”") % file.get_basename(), button_label=_("_Open"))
                toast.connect("button-clicked", lambda *_: Gtk.FileLauncher.new(file).launch(self, None, None))
                self.toast_overlay.add_toast(toast)

            dialog.save(self, None, done)

        self._with_rendered_preview(page, lambda pv: pv.get_html(got_html))

    @staticmethod
    def _standalone_html(page: DocumentPage, body: str) -> str:
        css = (WEB_DIR / "preview.css").read_text(encoding="utf-8")
        hl = (VENDOR_DIR / "highlight.css").read_text(encoding="utf-8")
        katex_version = "0.19.1"
        try:
            notices = (VENDOR_DIR / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8")
            for line in notices.splitlines():
                if line.startswith("katex "):
                    katex_version = line.split()[1]
        except OSError:
            pass
        theme = "dark" if Adw.StyleManager.get_default().get_dark() else "light"
        title = html.escape(page.props.title)
        return f"""<!doctype html>
<html class="{theme}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@{katex_version}/dist/katex.min.css">
<style>
{hl}
{css}
.code-copy {{ display: none; }}
</style>
</head>
<body>
<div id="content">
{body}
</div>
</body>
</html>
"""

    def _open_folder(self) -> None:
        page = self.current_page
        if page is None or page.file is None:
            return
        Gtk.FileLauncher.new(page.file).open_containing_folder(self, None, None)

    def toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=GLib.markup_escape_text(message), timeout=3))

    # ------------------------------------------------------------------
    # Drag and drop, closing

    def _on_drop(self, _target, value, _x, _y) -> bool:
        for f in value.get_files():
            self.open_file(f)
        return True

    def session_paths(self) -> list[str]:
        return [str(p.path) for p in self.pages() if p.path is not None]

    def _on_close_request(self, _win) -> bool:
        if self._closing_confirmed:
            self._store_state()
            return False
        dirty = [p for p in self.pages() if p.props.modified and not (p.is_untitled and p.buffer.get_char_count() == 0)]
        if not dirty:
            self._store_state()
            for p in self.pages():
                p.disconnect_all()
            return False
        names = "\n".join(f"• {p.props.title}" for p in dirty[:8]) + ("\n…" if len(dirty) > 8 else "")
        dialog = Adw.AlertDialog(
            heading=_("Save Changes?"),
            body=(_("%d documents have unsaved changes:") % len(dirty) if len(dirty) > 1 else _("One document has unsaved changes:")) + "\n\n" + names,
        )
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("discard", _("_Discard All"))
        dialog.add_response("save", _("_Save All"))
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def finish_close():
            self._closing_confirmed = True
            self.close()

        def on_response(_d, response):
            if response == "discard":
                finish_close()
            elif response == "save":
                self.save_all(lambda ok: finish_close() if ok else None)

        dialog.connect("response", on_response)
        dialog.present(self)
        return True

    def _store_state(self) -> None:
        s = self.settings.props
        if not self.is_maximized() and not self.is_fullscreen():
            w, h = self.get_default_size()
            s.window_width, s.window_height = w, h
        s.window_maximized = self.is_maximized()
        app = self.get_application()
        if app is not None and len(app.get_windows()) <= 1:
            s.session_files = json.dumps(self.session_paths())
            tp = self.tab_view.get_selected_page()
            s.session_active = self.tab_view.get_page_position(tp) if tp else 0
        self.settings.flush()
