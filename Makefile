PREFIX ?= $(HOME)/.local
DATADIR := $(PREFIX)/share
APPDIR := $(DATADIR)/markdown-editor
APP_ID := io.github._24icewolf42.markdown-editor
# 0 skips the desktop/icon cache refresh (Flatpak builds export their own).
UPDATE_CACHES ?= 1
# Machine-specific paths (SMOKE_BIG, DIRS) can live in an untracked local.mk.
-include local.mk
SMOKE_BIG ?=

.PHONY: run test test-unit test-render test-gui test-corpus vendor install uninstall uninstall-legacy appimage flatpak flatpak-node-sources flathub

run:
	./bin/markdown-editor

test: test-unit test-render test-gui

test-unit:
	python3 -m unittest discover tests -v

# Section-by-section preview rendering must match whole-document rendering.
test-render:
	cd web-src && node test-chunks.mjs

test-gui:
	python3 tests/gui_smoke.py $(if $(SMOKE_BIG),$(if $(wildcard $(SMOKE_BIG)),--big $(SMOKE_BIG)))

# Renders every Markdown file below the given directories through the preview
# bundle (Node.js + jsdom) and reports failures:  make test-corpus DIRS=~/docs
test-corpus:
	cd web-src && node test-corpus.mjs $(DIRS)

# Rebuilds markdown_editor/web/vendor from web-src (needs Node.js).
vendor:
	cd web-src && npm ci --legacy-peer-deps && node build.mjs

# Before 0.4.0 the app was installed as gnome-markdown-editor.
LEGACY_ID := io.github.gnome_markdown_editor

install: uninstall-legacy
	install -d $(APPDIR) $(PREFIX)/bin
	rm -rf $(APPDIR)/markdown_editor
	cp -r markdown_editor $(APPDIR)/
	find $(APPDIR) -name __pycache__ -prune -exec rm -rf {} +
	sed 's|@APPDIR@|$(APPDIR)|' bin/markdown-editor > $(PREFIX)/bin/markdown-editor
	chmod 755 $(PREFIX)/bin/markdown-editor
	install -d $(DATADIR)/applications
	sed 's|^Exec=markdown-editor|Exec=$(PREFIX)/bin/markdown-editor|' data/$(APP_ID).desktop > $(DATADIR)/applications/$(APP_ID).desktop
	install -Dm644 data/$(APP_ID).metainfo.xml $(DATADIR)/metainfo/$(APP_ID).metainfo.xml
	install -Dm644 markdown_editor/data/icons/hicolor/scalable/apps/$(APP_ID).svg $(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg
	install -Dm644 markdown_editor/data/icons/hicolor/symbolic/apps/$(APP_ID)-symbolic.svg $(DATADIR)/icons/hicolor/symbolic/apps/$(APP_ID)-symbolic.svg
	install -Dm644 markdown_editor/data/icons/hicolor/256x256/apps/$(APP_ID).png $(DATADIR)/icons/hicolor/256x256/apps/$(APP_ID).png
ifneq ($(UPDATE_CACHES),0)
	-update-desktop-database -q $(DATADIR)/applications
	-gtk-update-icon-cache -q -t $(DATADIR)/icons/hicolor
endif

uninstall:
	rm -rf $(APPDIR) $(PREFIX)/bin/markdown-editor
	rm -f $(DATADIR)/applications/$(APP_ID).desktop $(DATADIR)/metainfo/$(APP_ID).metainfo.xml
	rm -f $(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg $(DATADIR)/icons/hicolor/symbolic/apps/$(APP_ID)-symbolic.svg
	rm -f $(DATADIR)/icons/hicolor/256x256/apps/$(APP_ID).png

uninstall-legacy:
	rm -rf $(DATADIR)/gnome-markdown-editor $(PREFIX)/bin/gnome-markdown-editor
	rm -f $(DATADIR)/applications/$(LEGACY_ID).desktop $(DATADIR)/metainfo/$(LEGACY_ID).metainfo.xml
	rm -f $(DATADIR)/icons/hicolor/scalable/apps/$(LEGACY_ID).svg $(DATADIR)/icons/hicolor/symbolic/apps/$(LEGACY_ID)-symbolic.svg
	rm -f $(DATADIR)/icons/hicolor/256x256/apps/$(LEGACY_ID).png

# Release artifacts (CI builds both on every push; a vX.Y.Z tag publishes them).
VERSION := $(shell sed -n 's/^VERSION = "\(.*\)"$$/\1/p' markdown_editor/__init__.py)

appimage:
	packaging/appimage/build.sh

flatpak:
	flatpak-builder --user --force-clean --repo=build/flatpak-repo build/flatpak packaging/flatpak/$(APP_ID).yml
	mkdir -p dist
	flatpak build-bundle build/flatpak-repo dist/markdown-editor-$(VERSION)-x86_64.flatpak $(APP_ID)

# Offline npm sources for the Flatpak build of the preview bundle; run after
# changing web-src/package-lock.json (needs uv and network).
flatpak-node-sources:
	cd web-src && uvx --from "git+https://github.com/flatpak/flatpak-builder-tools.git#subdirectory=node" \
	  flatpak-node-generator npm package-lock.json -o ../packaging/flatpak/node-sources.json

# Git-based Flatpak manifest for the current commit (pushed, ideally tagged).
flathub:
	packaging/flathub/generate.py dist/flathub --commit $$(git rev-parse HEAD) \
	  $$(git describe --exact-match --tags HEAD 2>/dev/null | sed 's/^/--tag /')
