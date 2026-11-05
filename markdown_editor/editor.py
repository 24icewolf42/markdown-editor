"""Markdown editing commands on a GtkSourceView buffer."""

from __future__ import annotations

import re

from gi.repository import Gdk, GLib, Gtk, GtkSource

from . import markdown_utils as mu

URL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|mailto:|www\.)\S+$", re.I)
HEADING_PREFIX_RE = re.compile(r"^ {0,3}#{1,6}[ \t]+|^ {0,3}#{1,6}$")
LINE_PREFIX_RES = {
    "bullet": re.compile(r"^([ \t]*)[-*+][ \t]+(?!\[[ xX]\])"),
    "numbered": re.compile(r"^([ \t]*)\d{1,9}[.)][ \t]+"),
    "task": re.compile(r"^([ \t]*)[-*+][ \t]+\[[ xX]\][ \t]+"),
    "quote": re.compile(r"^([ \t]*)>[ \t]?"),
}

ALERT_KINDS = ("NOTE", "TIP", "IMPORTANT", "WARNING", "CAUTION")

TEMPLATES = {
    "table": "| Column 1 | Column 2 | Column 3 |\n| -------- | -------- | -------- |\n|          |          |          |\n",
    "mermaid": "```mermaid\n%%{init: { 'theme': 'neutral' } }%%\nflowchart LR\n  A[Start] --> B[End]\n```\n",
    "sequence": "```mermaid\n%%{init: { 'theme': 'neutral' } }%%\nsequenceDiagram\n  participant A\n  participant B\n  A->>B: Request\n  B-->>A: Response\n```\n",
    "math": "$$\n\n$$\n",
    "details": "<details>\n<summary>Summary</summary>\n\n\n\n</details>\n",
    "front-matter": "---\ntitle: \nversion: \n---\n\n",
    "carousel": "````carousel\n![First](image-1.png)\n<!-- slide -->\n![Second](image-2.png)\n````\n",
    "footnote": None,  # handled specially
    "hr": "\n---\n\n",
}


class MarkdownEditing:
    """Formatting commands and smart-typing behaviour for one view."""

    def __init__(self, view: GtkSource.View, settings) -> None:
        self.view = view
        self.buffer: GtkSource.Buffer = view.get_buffer()
        self.settings = settings
        key = Gtk.EventControllerKey()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key.connect("key-pressed", self._on_key_pressed)
        view.add_controller(key)
        view.connect("paste-clipboard", self._on_paste)

    # ------------------------------------------------------------------
    # Helpers

    def _sel_bounds(self):
        b = self.buffer
        has = b.get_has_selection()
        if has:
            start, end = b.get_selection_bounds()
        else:
            start = b.get_iter_at_mark(b.get_insert())
            end = start.copy()
        return has, start, end

    def _line_text(self, line: int) -> str:
        b = self.buffer
        ok, start = b.get_iter_at_line(line)
        end = start.copy()
        if not end.ends_line():
            end.forward_to_line_end()
        return b.get_text(start, end, True)

    def _replace_line(self, line: int, text: str) -> None:
        b = self.buffer
        ok, start = b.get_iter_at_line(line)
        end = start.copy()
        if not end.ends_line():
            end.forward_to_line_end()
        b.delete(start, end)
        ok, start = b.get_iter_at_line(line)
        b.insert(start, text)

    def _indent_unit(self) -> str:
        return " " * self.view.get_tab_width() if self.view.get_insert_spaces_instead_of_tabs() else "\t"

    def _in_code_block(self, line: int) -> bool:
        b = self.buffer
        start = b.get_start_iter()
        ok, end = b.get_iter_at_line(line)
        lines = b.get_text(start, end, True).split("\n")[:-1]
        lines.append(self._line_text(line))
        return mu.code_line_mask(lines)[-1]

    # ------------------------------------------------------------------
    # Inline formatting

    def wrap(self, left: str, right: str | None = None, placeholder: str = "") -> None:
        right = left if right is None else right
        b = self.buffer
        has, start, end = self._sel_bounds()
        b.begin_user_action()
        try:
            if has:
                text = b.get_text(start, end, True)
                # Toggle off if already wrapped (inside or just outside the selection).
                if len(text) >= len(left) + len(right) and text.startswith(left) and text.endswith(right):
                    inner = text[len(left) : len(text) - len(right)]
                    off = start.get_offset()
                    b.delete(start, end)
                    b.insert(b.get_iter_at_offset(off), inner)
                    b.select_range(b.get_iter_at_offset(off), b.get_iter_at_offset(off + len(inner)))
                    return
                outer_s = start.copy()
                outer_e = end.copy()
                if outer_s.backward_chars(len(left)) and outer_e.forward_chars(len(right)):
                    if b.get_text(outer_s, start, True) == left and b.get_text(end, outer_e, True) == right:
                        off = outer_s.get_offset()
                        b.delete(outer_s, outer_e)
                        b.insert(b.get_iter_at_offset(off), text)
                        b.select_range(b.get_iter_at_offset(off), b.get_iter_at_offset(off + len(text)))
                        return
                off = start.get_offset()
                b.delete(start, end)
                b.insert(b.get_iter_at_offset(off), f"{left}{text}{right}")
                b.select_range(b.get_iter_at_offset(off + len(left)), b.get_iter_at_offset(off + len(left) + len(text)))
            else:
                off = start.get_offset()
                b.insert(start, f"{left}{placeholder}{right}")
                b.select_range(b.get_iter_at_offset(off + len(left)), b.get_iter_at_offset(off + len(left) + len(placeholder)))
        finally:
            b.end_user_action()
        self.view.scroll_mark_onscreen(b.get_insert())

    def bold(self):
        self.wrap("**", placeholder="bold text")

    def italic(self):
        self.wrap("_", placeholder="italic text")

    def strikethrough(self):
        self.wrap("~~", placeholder="struck text")

    def highlight(self):
        self.wrap("==", placeholder="highlighted text")

    def inline_code(self):
        has, start, end = self._sel_bounds()
        text = self.buffer.get_text(start, end, True) if has else ""
        if "\n" in text:
            self.code_block()
            return
        fence = "``" if "`" in text else "`"
        self.wrap(fence, placeholder="code")

    def inline_math(self):
        self.wrap("$", placeholder="x^2")

    def link(self, image: bool = False):
        b = self.buffer
        has, start, end = self._sel_bounds()
        text = b.get_text(start, end, True) if has else ""
        bang = "!" if image else ""
        b.begin_user_action()
        try:
            off = start.get_offset()
            if has:
                b.delete(start, end)
            if text and URL_RE.match(text.strip()):
                label = "alt text" if image else "text"
                b.insert(b.get_iter_at_offset(off), f"{bang}[{label}]({text.strip()})")
                b.select_range(b.get_iter_at_offset(off + len(bang) + 1), b.get_iter_at_offset(off + len(bang) + 1 + len(label)))
            else:
                label = text or ("alt text" if image else "text")
                url = "https://"
                b.insert(b.get_iter_at_offset(off), f"{bang}[{label}]({url})")
                s = off + len(bang) + len(label) + 3
                if text:
                    b.select_range(b.get_iter_at_offset(s), b.get_iter_at_offset(s + len(url)))
                else:
                    b.select_range(b.get_iter_at_offset(off + len(bang) + 1), b.get_iter_at_offset(off + len(bang) + 1 + len(label)))
        finally:
            b.end_user_action()

    def insert_image_path(self, rel_path: str, alt: str) -> None:
        b = self.buffer
        has, start, end = self._sel_bounds()
        b.begin_user_action()
        if has:
            b.delete(start, end)
        b.insert_at_cursor(f"![{alt}]({rel_path.replace(' ', '%20')})")
        b.end_user_action()

    # ------------------------------------------------------------------
    # Line-level formatting

    def _selected_lines(self) -> tuple[int, int]:
        has, start, end = self._sel_bounds()
        first = start.get_line()
        last = end.get_line()
        if has and end.starts_line() and last > first:
            last -= 1
        return first, last

    def heading(self, level: int) -> None:
        first, last = self._selected_lines()
        b = self.buffer
        b.begin_user_action()
        for line in range(first, last + 1):
            text = self._line_text(line)
            m = HEADING_PREFIX_RE.match(text)
            current = len(m.group(0).strip()) if m else 0
            body = text[m.end() :] if m else text.lstrip()
            new = body if (level == current or level == 0) else f"{'#' * level} {body}"
            self._replace_line(line, new)
        b.end_user_action()
        if first == last:
            ok, it = b.get_iter_at_line(first)
            if not it.ends_line():
                it.forward_to_line_end()
            b.place_cursor(it)

    def toggle_line_prefix(self, kind: str) -> None:
        first, last = self._selected_lines()
        lines = [self._line_text(i) for i in range(first, last + 1)]
        pattern = LINE_PREFIX_RES[kind]
        nonblank = [l for l in lines if l.strip()]
        remove = bool(nonblank) and all(pattern.match(l) for l in nonblank)
        b = self.buffer
        b.begin_user_action()
        n = 1
        for i, text in enumerate(lines):
            line = first + i
            if not text.strip() and len(lines) > 1:
                continue
            if remove:
                new = pattern.sub(r"\1", text, count=1)
            else:
                indent = re.match(r"^[ \t]*", text).group(0)
                body = text[len(indent) :]
                # Swap other list markers rather than stacking them.
                for other in ("task", "bullet", "numbered"):
                    if other != kind and kind != "quote":
                        body = LINE_PREFIX_RES[other].sub("", body, count=1)
                prefix = {"bullet": "- ", "numbered": f"{n}. ", "task": "- [ ] ", "quote": "> "}[kind]
                new = indent + prefix + body
                n += 1
            self._replace_line(line, new)
        b.end_user_action()

    def code_block(self, lang: str = "") -> None:
        b = self.buffer
        has, start, end = self._sel_bounds()
        b.begin_user_action()
        try:
            if has:
                first, last = self._selected_lines()
                ok, s = b.get_iter_at_line(first)
                ok, e = b.get_iter_at_line(last)
                if not e.ends_line():
                    e.forward_to_line_end()
                text = b.get_text(s, e, True)
                fence = "````" if "```" in text else "```"
                off = s.get_offset()
                b.delete(s, e)
                b.insert(b.get_iter_at_offset(off), f"{fence}{lang}\n{text}\n{fence}")
                b.place_cursor(b.get_iter_at_offset(off + len(fence) + len(lang)))
            else:
                it = start
                prefix = "" if it.starts_line() else "\n"
                off = it.get_offset() + len(prefix)
                b.insert(it, f"{prefix}```{lang}\n\n```\n")
                b.place_cursor(b.get_iter_at_offset(off + 3 + len(lang)))
        finally:
            b.end_user_action()

    def insert_block(self, text: str, cursor_marker: str | None = None) -> None:
        """Insert a block template on its own lines at the cursor."""
        b = self.buffer
        has, start, end = self._sel_bounds()
        b.begin_user_action()
        if has:
            b.delete(start, end)
            start = b.get_iter_at_mark(b.get_insert())
        prefix = ""
        if not start.starts_line():
            prefix = "\n\n"
        elif start.get_line() > 0 and self._line_text(start.get_line() - 1).strip():
            prefix = "\n"
        off = start.get_offset() + len(prefix)
        b.insert(start, prefix + text)
        if cursor_marker and cursor_marker in text:
            pos = off + text.index(cursor_marker)
            b.select_range(b.get_iter_at_offset(pos), b.get_iter_at_offset(pos + len(cursor_marker)))
        b.end_user_action()
        self.view.scroll_mark_onscreen(b.get_insert())

    def insert_template(self, name: str) -> None:
        if name == "footnote":
            self.footnote()
            return
        if name.startswith("alert-"):
            kind = name.split("-", 1)[1].upper()
            self.insert_block(f"> [!{kind}]\n> Text\n", "Text")
            return
        markers = {"table": "Column 1", "mermaid": "A[Start] --> B[End]", "details": "Summary", "front-matter": None}
        self.insert_block(TEMPLATES[name], markers.get(name))

    def footnote(self) -> None:
        b = self.buffer
        text = b.get_text(b.get_start_iter(), b.get_end_iter(), True)
        used = {int(n) for n in re.findall(r"\[\^(\d+)\]", text)}
        n = 1
        while n in used:
            n += 1
        b.begin_user_action()
        b.insert_at_cursor(f"[^{n}]")
        end = b.get_end_iter()
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        b.insert(end, f"{sep}[^{n}]: ")
        b.place_cursor(b.get_end_iter())
        b.end_user_action()
        self.view.scroll_mark_onscreen(b.get_insert())

    def insert_toc(self) -> None:
        b = self.buffer
        text = b.get_text(b.get_start_iter(), b.get_end_iter(), True)
        toc = mu.table_of_contents(mu.parse_headings(text))
        if toc:
            self.insert_block(toc + "\n")

    def format_table(self) -> bool:
        b = self.buffer
        line = b.get_iter_at_mark(b.get_insert()).get_line()
        if not mu.is_table_line(self._line_text(line)):
            return False
        first = line
        while first > 0 and mu.is_table_line(self._line_text(first - 1)):
            first -= 1
        last = line
        nlines = b.get_line_count()
        while last + 1 < nlines and mu.is_table_line(self._line_text(last + 1)):
            last += 1
        lines = [self._line_text(i) for i in range(first, last + 1)]
        out = mu.format_table(lines)
        if out is None or out == lines:
            return out is not None
        cursor = b.get_iter_at_mark(b.get_insert())
        cur_line, cur_col = cursor.get_line(), cursor.get_line_offset()
        b.begin_user_action()
        ok, s = b.get_iter_at_line(first)
        ok, e = b.get_iter_at_line(last)
        if not e.ends_line():
            e.forward_to_line_end()
        b.delete(s, e)
        ok, s = b.get_iter_at_line(first)
        b.insert(s, "\n".join(out))
        b.end_user_action()
        ok, it = b.get_iter_at_line(cur_line)
        it.set_line_offset(min(cur_col, max(0, it.get_chars_in_line() - 1)))
        b.place_cursor(it)
        return True

    def toggle_task_at_line(self, line: int) -> bool:
        if line < 0 or line >= self.buffer.get_line_count():
            return False
        new = mu.toggle_task(self._line_text(line))
        if new is None:
            return False
        self.buffer.begin_user_action()
        self._replace_line(line, new)
        self.buffer.end_user_action()
        return True

    # ------------------------------------------------------------------
    # Smart typing

    def _on_key_pressed(self, _ctrl, keyval, _keycode, state) -> bool:
        mods = state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SHIFT_MASK)
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not mods:
            return self._continue_list()
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab) and not (mods & ~Gdk.ModifierType.SHIFT_MASK):
            return self._indent_list_item(outdent=keyval == Gdk.KEY_ISO_Left_Tab or bool(mods & Gdk.ModifierType.SHIFT_MASK))
        return False

    def _continue_list(self) -> bool:
        if not self.settings.props.smart_lists:
            return False
        b = self.buffer
        if b.get_has_selection():
            return False
        it = b.get_iter_at_mark(b.get_insert())
        line = it.get_line()
        text = self._line_text(line)
        col = it.get_line_offset()
        prefix, empty = mu.list_continuation(text)
        if prefix is None or col < len(prefix.rstrip()) or self._in_code_block(line):
            return False
        b.begin_user_action()
        try:
            if empty and col >= len(text.rstrip()):
                # Empty item: end the list (remove the marker).
                self._replace_line(line, "")
                ok, it = b.get_iter_at_line(line)
                b.place_cursor(it)
            else:
                b.insert_at_cursor("\n" + prefix)
        finally:
            b.end_user_action()
        self.view.scroll_mark_onscreen(b.get_insert())
        return True

    def _indent_list_item(self, outdent: bool) -> bool:
        b = self.buffer
        if b.get_has_selection():
            return False  # GtkSourceView indents selections itself
        it = b.get_iter_at_mark(b.get_insert())
        line = it.get_line()
        text = self._line_text(line)
        if not mu.LIST_RE.match(text) or self._in_code_block(line):
            return False
        col = it.get_line_offset()
        unit = self._indent_unit()
        b.begin_user_action()
        if outdent:
            m = re.match(r"^( {1,%d}|\t)" % max(1, len(unit)), text)
            if not m:
                b.end_user_action()
                return True
            self._replace_line(line, text[m.end() :])
            col = max(0, col - m.end())
        else:
            self._replace_line(line, unit + text)
            col += len(unit)
        ok, it = b.get_iter_at_line_offset(line, min(col, len(self._line_text(line))))
        b.place_cursor(it)
        b.end_user_action()
        return True

    def _on_paste(self, view) -> None:
        """Pasting a URL over a selection turns the selection into a link."""
        b = self.buffer
        if not b.get_has_selection():
            return
        start, end = b.get_selection_bounds()
        selected = b.get_text(start, end, True)
        if "\n" in selected or URL_RE.match(selected.strip()):
            return
        clipboard = view.get_clipboard()
        formats = clipboard.get_formats()
        if not formats.contain_gtype(GLib.GType.from_name("gchararray")) and not formats.contain_mime_type("text/plain"):
            return
        view.stop_emission_by_name("paste-clipboard")
        so, eo = start.get_offset(), end.get_offset()

        def done(cb, res):
            try:
                text = cb.read_text_finish(res)
            except GLib.Error:
                text = None
            s = b.get_iter_at_offset(so)
            e = b.get_iter_at_offset(eo)
            b.begin_user_action()
            b.delete(s, e)
            s = b.get_iter_at_offset(so)
            if text and URL_RE.match(text.strip()) and "\n" not in text.strip():
                b.insert(s, f"[{selected}]({text.strip()})")
            elif text is not None:
                b.insert(s, text)
            b.end_user_action()

        clipboard.read_text_async(None, done)
