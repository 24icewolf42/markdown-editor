"""Persistent application settings.

Stored as JSON in ``$XDG_CONFIG_HOME/gnome-markdown-editor/settings.json`` so
the editor runs straight from a source checkout without installing and
compiling a GSettings schema. Every key is a GObject property, so widgets can
bind to it and code can listen to ``notify::<key>``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from gi.repository import GLib, GObject

CONFIG_DIR = Path(GLib.get_user_config_dir()) / "gnome-markdown-editor"
CONFIG_FILE = CONFIG_DIR / "settings.json"

# key: (GObject type, default)
SCHEMA: dict[str, tuple[type, object]] = {
    # Editor
    "use-system-font": (bool, True),
    "custom-font": (str, "Monospace 11"),
    "zoom": (int, 0),  # font size delta in points
    "style-scheme": (str, "Adwaita"),
    "color-scheme": (str, "default"),  # default | light | dark
    "show-line-numbers": (bool, True),
    "highlight-current-line": (bool, True),
    "show-right-margin": (bool, False),
    "right-margin-position": (int, 80),
    "wrap-text": (bool, True),
    "tab-width": (int, 4),
    "insert-spaces": (bool, True),
    "auto-indent": (bool, True),
    "smart-lists": (bool, True),
    "spellcheck": (bool, True),
    "show-map": (bool, False),
    "highlight-matching-brackets": (bool, True),
    # Preview
    "view-mode": (str, "split"),  # editor | split | preview
    "sync-scroll": (bool, True),
    "preview-font-size": (int, 15),
    "preview-full-width": (bool, False),
    "preview-position": (str, "right"),  # right | left
    "collapse-changelogs": (bool, True),
    # Behaviour
    "restore-session": (bool, True),
    "auto-reload": (bool, True),
    "show-outline": (bool, False),
    # Window state
    "window-width": (int, 1280),
    "window-height": (int, 820),
    "window-maximized": (bool, False),
    "session-files": (str, "[]"),  # JSON list of paths
    "session-active": (int, 0),
}


def _prop_name(key: str) -> str:
    return key.replace("-", "_")


class _SettingsBase(GObject.Object):
    __gtype_name__ = "GmeSettingsBase"

    _instance: "Settings | None" = None

    def __init__(self) -> None:
        super().__init__()
        self._loading = True
        self._save_source = 0
        data: dict = {}
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for key, (typ, default) in SCHEMA.items():
            value = data.get(key, default)
            if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
                value = default
            self.set_property(key, value)
        self._loading = False
        self.connect("notify", self._on_notify)

    @staticmethod
    def get() -> "Settings":
        if _SettingsBase._instance is None:
            _SettingsBase._instance = Settings()
        return _SettingsBase._instance

    def _on_notify(self, _obj, _pspec) -> None:
        if self._loading:
            return
        if self._save_source:
            GLib.source_remove(self._save_source)
        self._save_source = GLib.timeout_add(400, self._save_idle)

    def _save_idle(self) -> bool:
        self._save_source = 0
        self.save()
        return GLib.SOURCE_REMOVE

    def save(self) -> None:
        data = {key: self.get_property(key) for key in SCHEMA}
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, CONFIG_FILE)
        except OSError as exc:  # pragma: no cover - reported, not fatal
            print(f"gnome-markdown-editor: could not save settings: {exc}")

    def flush(self) -> None:
        if self._save_source:
            GLib.source_remove(self._save_source)
            self._save_source = 0
        self.save()


def _properties() -> dict:
    props: dict = {}
    for key, (typ, default) in SCHEMA.items():
        if typ is bool:
            props[_prop_name(key)] = GObject.Property(type=bool, default=default, nick=key)
        elif typ is int:
            props[_prop_name(key)] = GObject.Property(
                type=int, default=default, minimum=-1000, maximum=100000, nick=key
            )
        else:
            props[_prop_name(key)] = GObject.Property(type=str, default=default, nick=key)
    return props


# GObject properties must exist when the class is created, so the concrete
# class is assembled from SCHEMA.
Settings = type("Settings", (_SettingsBase,), {"__gtype_name__": "GmeSettings", **_properties()})
