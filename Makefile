APP            := krb-ticket-indicator
BIN_DIR        := $(if $(XDG_BIN_HOME),$(XDG_BIN_HOME),$(HOME)/.local/bin)
CONFIG_DIR     := $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME),$(HOME)/.config)
AUTOSTART_DIR  := $(CONFIG_DIR)/autostart
APPLICATIONS_DIR := $(if $(XDG_DATA_HOME),$(XDG_DATA_HOME),$(HOME)/.local/share)/applications
TARGET_BIN     := $(BIN_DIR)/$(APP).py
TARGET_DESKTOP := $(AUTOSTART_DIR)/$(APP).desktop
TARGET_APP     := $(APPLICATIONS_DIR)/$(APP).desktop
TARGET_CONFIG  := $(CONFIG_DIR)/$(APP).conf

.PHONY: help install uninstall reinstall start stop restart status check-deps

help:
	@echo "Kerberos Ticket Indicator"
	@echo "  make install     check deps, install script + config + autostart, start"
	@echo "  make uninstall   stop and remove all files (incl. config)"
	@echo "  make reinstall   reinstall program files, keep existing config"
	@echo "  make restart     restart the indicator"
	@echo "  make status      klist status + running process"

check-deps:
	@miss=""; \
	command -v klist >/dev/null || miss="$$miss klist(krb5)"; \
	python3 -c "import gi" 2>/dev/null || miss="$$miss python3-gi"; \
	python3 -c "import gi; \
	  exec('try:\n gi.require_version(\"AyatanaAppIndicator3\",\"0.1\")\nexcept ValueError:\n gi.require_version(\"AppIndicator3\",\"0.1\")')" \
	  2>/dev/null || miss="$$miss appindicator-gir"; \
	if [ -n "$$miss" ]; then \
	  echo "MISSING DEPENDENCIES:$$miss"; \
	  if   command -v apt    >/dev/null; then echo "  sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1 krb5-user"; \
	  elif command -v dnf    >/dev/null; then echo "  sudo dnf install python3-gobject libayatana-appindicator-gtk3 krb5-workstation"; \
	  elif command -v pacman >/dev/null; then echo "  sudo pacman -S python-gobject libayatana-appindicator krb5"; \
	  elif command -v zypper >/dev/null; then echo "  sudo zypper install python3-gobject libayatana-appindicator3-1 krb5-client"; fi; \
	  exit 1; \
	fi; \
	echo "dependencies ok"

# Stops only running python interpreters of this script. The ps comm == python*
# check makes sure no shell is hit, even if its command line contains the
# script name as text.
stop:
	@pgrep -f "$(APP).py" 2>/dev/null | while read -r pid; do \
	  case "$$(ps -o comm= -p $$pid 2>/dev/null)" in \
	    python*) kill $$pid 2>/dev/null && echo "stopped: PID $$pid" || true ;; \
	  esac; \
	done; true

start:
	@setsid "$(TARGET_BIN)" >/dev/null 2>&1 </dev/null & \
	echo "started: $(TARGET_BIN)"

restart: stop start

install: check-deps
	@mkdir -p "$(BIN_DIR)" "$(AUTOSTART_DIR)" "$(APPLICATIONS_DIR)" "$(CONFIG_DIR)"
	@install -m 0755 bin/$(APP).py "$(TARGET_BIN)"
	@sed 's|__EXEC__|$(TARGET_BIN)|g' share/$(APP).desktop.in > "$(TARGET_DESKTOP)"
	@sed 's|__EXEC__|$(TARGET_BIN)|g' share/$(APP).desktop.in > "$(TARGET_APP)"
	@if [ -e "$(TARGET_CONFIG)" ]; then \
	  echo "config kept (already exists): $(TARGET_CONFIG)"; \
	else \
	  install -m 0644 share/$(APP).conf "$(TARGET_CONFIG)"; \
	  echo "config -> $(TARGET_CONFIG)"; \
	fi
	@$(MAKE) --no-print-directory restart
	@echo "installed -> $(TARGET_BIN)"
	@echo "autostart -> $(TARGET_DESKTOP)"
	@echo "launcher  -> $(TARGET_APP)"
	@echo "GNOME: if needed, 'gnome-extensions enable ubuntu-appindicators@ubuntu.com'"

uninstall: stop
	@rm -fv "$(TARGET_BIN)" "$(TARGET_DESKTOP)" "$(TARGET_APP)" "$(TARGET_CONFIG)"
	@echo "uninstalled (config removed too)"

# Reinstall the program files but keep an existing config.
reinstall: stop
	@rm -fv "$(TARGET_BIN)" "$(TARGET_DESKTOP)" "$(TARGET_APP)"
	@$(MAKE) --no-print-directory install

status:
	@echo "== klist =="; klist 2>&1 | head -5 || true
	@echo "== process =="; \
	found=; \
	for pid in $$(pgrep -f "$(APP).py" 2>/dev/null); do \
	  case "$$(ps -o comm= -p $$pid 2>/dev/null)" in \
	    python*) echo "running: PID $$pid"; found=1 ;; \
	  esac; \
	done; \
	[ -n "$$found" ] || echo "not running"
