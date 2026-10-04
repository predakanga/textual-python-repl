# Python REPL for Textual
#
#   make                build build/PythonREPL.bundle
#   make install        hand it to Textual's extension installer
#   make dist           zip it up for a release
#   make test           run the test suite against a mock Textual
#   make lint           ruff check + ruff format --check
#
# See CONTRIBUTING.md for the rest.

VERSION := $(shell cat VERSION)
BUNDLE_ID := io.github.predakanga.TextualPythonREPL
MACOSX_DEPLOYMENT_TARGET := 11.0

# Embedded interpreter: python-build-standalone, verified against PBS_SHA256.
# Bumping Python means updating all four, then `make lock`.
PYTHON_VERSION := 3.13.15
PYTHON_MAJOR_MINOR := 3.13
PBS_RELEASE := 20260901
PBS_SHA256 := d3904bd6a072246e07aa0bdadee9a14e80521e42a943c0848059feb16a2816dc
PBS_ASSET := cpython-$(PYTHON_VERSION)+$(PBS_RELEASE)-aarch64-apple-darwin-install_only_stripped.tar.gz
PBS_URL := https://github.com/astral-sh/python-build-standalone/releases/download/$(PBS_RELEASE)/$(subst +,%2B,$(PBS_ASSET))

# "-" signs ad hoc. Set to a "Developer ID Application" identity for releases.
SIGN_IDENTITY ?= -

BUILD := build
CACHE := .cache
RUNTIME := $(BUILD)/runtime
BUNDLE := $(BUILD)/PythonREPL.bundle
RESOURCES := $(BUNDLE)/Contents/Resources
DIST := $(BUILD)/TextualPythonREPL-$(VERSION).zip
HARNESS := $(BUILD)/tests/Harness
HARNESS_APP := $(BUILD)/tests/Harness.app

CC := clang
CFLAGS := -fobjc-arc -fmodules -O2 -arch arm64 -mmacosx-version-min=$(MACOSX_DEPLOYMENT_TARGET) -Wall -Wextra -Wno-unused-parameter -Werror

PLUGIN_SOURCES := $(wildcard src/plugin/*.m)
PLUGIN_HEADERS := $(wildcard src/plugin/*.h)
PYTHON_SOURCES := $(wildcard src/textual_repl/*.py)

RUFF_VERSION := 0.16.10
RUFF ?= uvx ruff@$(RUFF_VERSION)

ifeq ($(SIGN_IDENTITY),-)
CODESIGN := codesign --force --sign - --timestamp=none
else
CODESIGN := codesign --force --sign "$(SIGN_IDENTITY)" --timestamp --options runtime
endif

.PHONY: all bundle install dist test test-sandbox check lint lock clean distclean

all: bundle

bundle: $(BUNDLE)/Contents/Info.plist

# --------------------------------------------------------------------------
# Python runtime: download, verify, trim, install locked dependencies
# --------------------------------------------------------------------------

$(CACHE)/$(PBS_ASSET):
	@mkdir -p $(CACHE)
	@echo "==> Downloading Python $(PYTHON_VERSION) ($(PBS_RELEASE))"
	curl --fail --location --silent --show-error --retry 3 --output "$@.part" "$(PBS_URL)"
	@echo "$(PBS_SHA256)  $@.part" | shasum -a 256 --check --status || { echo "Checksum mismatch for $(PBS_ASSET)" >&2; rm -f "$@.part"; exit 1; }
	@mv "$@.part" "$@"

$(RUNTIME)/.stamp: $(CACHE)/$(PBS_ASSET) requirements.txt
	@echo "==> Preparing Python runtime"
	rm -rf $(RUNTIME) && mkdir -p $(RUNTIME)
	tar -xzf $(CACHE)/$(PBS_ASSET) -C $(RUNTIME) --strip-components 1
	cd $(RUNTIME) && rm -rf share bin/idle* lib/tcl* lib/tk* lib/itcl* lib/thread* lib/libtcl* lib/libtk* \
		lib/python$(PYTHON_MAJOR_MINOR)/test lib/python$(PYTHON_MAJOR_MINOR)/idlelib \
		lib/python$(PYTHON_MAJOR_MINOR)/tkinter lib/python$(PYTHON_MAJOR_MINOR)/turtledemo \
		lib/python$(PYTHON_MAJOR_MINOR)/lib-dynload/_tkinter*
	@echo "==> Installing locked dependencies"
	PIP_CACHE_DIR=$(abspath $(CACHE))/pip $(RUNTIME)/bin/python3 -m pip install --quiet --disable-pip-version-check \
		--require-hashes --only-binary :all: --no-deps -r requirements.txt
	rm -rf $(RUNTIME)/lib/python$(PYTHON_MAJOR_MINOR)/site-packages/PyObjCTest
	find $(RUNTIME) -name __pycache__ -type d -prune -exec rm -rf {} +
	@touch $@

# --------------------------------------------------------------------------
# Plugin bundle
# --------------------------------------------------------------------------

$(BUILD)/PythonREPL: $(PLUGIN_SOURCES) $(PLUGIN_HEADERS) $(RUNTIME)/.stamp
	@echo "==> Compiling plugin"
	@mkdir -p $(BUILD)
	$(CC) -bundle $(CFLAGS) \
		-I$(RUNTIME)/include/python$(PYTHON_MAJOR_MINOR) \
		-L$(RUNTIME)/lib -lpython$(PYTHON_MAJOR_MINOR) \
		-Wl,-rpath,@loader_path/../Resources/python/lib \
		-framework Foundation -framework AppKit \
		-o $@ $(PLUGIN_SOURCES)

$(BUNDLE)/Contents/Info.plist: $(BUILD)/PythonREPL $(RUNTIME)/.stamp $(PYTHON_SOURCES) src/plugin/Info.plist.in VERSION
	@echo "==> Assembling $(BUNDLE)"
	rm -rf $(BUNDLE)
	mkdir -p $(BUNDLE)/Contents/MacOS $(RESOURCES)/lib
	cp $(BUILD)/PythonREPL $(BUNDLE)/Contents/MacOS/
	rsync -a $(RUNTIME)/ $(RESOURCES)/python/ --exclude .stamp
	rsync -a --exclude __pycache__ src/textual_repl $(RESOURCES)/lib/
	echo '__version__ = "$(VERSION)"' > $(RESOURCES)/lib/textual_repl/_version.py
	@# The plugin runs with bytecode writing off so it never modifies the
	@# signed bundle; ship hash-based .pyc files instead.
	$(RESOURCES)/python/bin/python3 -m compileall -q -j 0 --invalidation-mode unchecked-hash \
		$(RESOURCES)/python/lib/python$(PYTHON_MAJOR_MINOR) $(RESOURCES)/lib > /dev/null
	@echo "==> Signing ($(SIGN_IDENTITY))"
	find $(RESOURCES) -type f \( -name '*.so' -o -name '*.dylib' \) -print0 | xargs -0 -n 64 $(CODESIGN)
	$(CODESIGN) $(RESOURCES)/python/bin/python$(PYTHON_MAJOR_MINOR)
	sed -e 's/@VERSION@/$(VERSION)/g' -e 's/@BUNDLE_ID@/$(BUNDLE_ID)/g' \
		-e 's/@MACOSX_DEPLOYMENT_TARGET@/$(MACOSX_DEPLOYMENT_TARGET)/g' \
		src/plugin/Info.plist.in > $@
	$(CODESIGN) $(BUNDLE)
	@du -sh $(BUNDLE)

# --------------------------------------------------------------------------
# Installing
#
# Plugins live in Textual's sandbox group container, and macOS only lets
# Textual itself write there: cp, ditto, rsync and even Installer.app fail
# with EINTR ("Interrupted system call"). Textual has its own importer for
# .bundle files, though, so hand the bundle to that. It moves any previous
# version to the Trash.
# --------------------------------------------------------------------------

install: bundle
	rm -rf $(BUILD)/install && mkdir -p $(BUILD)/install
	ditto $(BUNDLE) $(BUILD)/install/PythonREPL.bundle
	open -a Textual $(BUILD)/install/PythonREPL.bundle
	@echo "==> Confirm the install in Textual, then restart Textual to load it."

dist: $(DIST)

$(DIST): $(BUNDLE)/Contents/Info.plist
	rm -f $@
	ditto -c -k --sequesterRsrc --keepParent $(BUNDLE) $@
	@ls -lh $@ | awk '{ print "    " $$5 }'

# --------------------------------------------------------------------------
# Tests (against a mock Textual; see tests/harness/Harness.m)
# --------------------------------------------------------------------------

$(HARNESS): tests/harness/Harness.m
	@mkdir -p $(dir $@)
	$(CC) -fobjc-arc -fmodules -arch arm64 -Wall -Werror -Wno-deprecated-declarations \
		-framework Foundation -framework AppKit -o $@ $<

$(HARNESS_APP)/.stamp: $(HARNESS) $(BUNDLE)/Contents/Info.plist tests/harness/Info.plist tests/harness/harness.entitlements
	rm -rf $(HARNESS_APP)
	mkdir -p $(HARNESS_APP)/Contents/MacOS $(HARNESS_APP)/Contents/PlugIns
	cp $(HARNESS) $(HARNESS_APP)/Contents/MacOS/
	cp tests/harness/Info.plist $(HARNESS_APP)/Contents/
	ditto $(BUNDLE) $(HARNESS_APP)/Contents/PlugIns/PythonREPL.bundle
	codesign --force --sign - --entitlements tests/harness/harness.entitlements $(HARNESS_APP)
	@touch $@

test: bundle $(HARNESS)
	HARNESS="$(abspath $(HARNESS))" BUNDLE="$(abspath $(BUNDLE))" \
		$(RESOURCES)/python/bin/python3 -m unittest discover --start-directory tests --top-level-directory . $(if $(V),--verbose)

# Same suite with the harness running under the App Sandbox, like Textual.
test-sandbox: bundle $(HARNESS_APP)/.stamp
	HARNESS="$(abspath $(HARNESS_APP))/Contents/MacOS/Harness" \
		BUNDLE="$(abspath $(HARNESS_APP))/Contents/PlugIns/PythonREPL.bundle" HARNESS_SANDBOXED=1 \
		$(RESOURCES)/python/bin/python3 -m unittest discover --start-directory tests --top-level-directory . $(if $(V),--verbose)

lint:
	$(RUFF) check src tests examples
	$(RUFF) format --check src tests examples

check: lint test

# Regenerate requirements.txt from requirements.in (needs uv).
lock:
	uv pip compile requirements.in --quiet --generate-hashes \
		--python-version $(PYTHON_MAJOR_MINOR) --python-platform aarch64-apple-darwin \
		--only-binary :all: --custom-compile-command "make lock" -o requirements.txt

clean:
	rm -rf $(BUILD)

distclean: clean
	rm -rf $(CACHE)
