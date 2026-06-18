#!/usr/bin/env python3
"""Plain GNOME indicator for the Kerberos TGT.

Shows in the panel (top right):
  - whether a ticket exists
  - whether it is valid
  - the principal and remaining lifetime (e.g. "alice/admin 23h 45m")

Polls `klist` periodically and updates icon, label and menu.
Works on any distro with GNOME/AppIndicator (see README).
"""

import fcntl
import os
import re
import subprocess
import sys
import threading
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
# Ayatana first (current), fall back to legacy AppIndicator.
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppIndicator

from gi.repository import GLib, Gtk  # noqa: E402

POLL_SECONDS = 30
INDICATOR_ID = "krb-ticket-indicator"
# Request a renewable ticket so "Renew ticket (kinit -R)" works afterwards.
# Bounded by the KDC's max_renewable_life; ignored if the KDC forbids it.
RENEW_LIFETIME = "7d"

CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "krb-ticket-indicator.conf",
)


DEFAULT_CONFIG = {
    "principal": "",
    "ticket_lifetime": "",
    "renew_lifetime": RENEW_LIFETIME,
    "renewable": "true",
    "auto_renew": "false",
    "warn_minutes": "30",
    "show_principal": "true",
    "show_time": "true",
}


def load_config():
    """Optional config file with 'key = value' lines (# comments allowed).

    Keys: principal, ticket_lifetime, renew_lifetime, renewable, auto_renew,
    warn_minutes, show_principal, show_time.
    """
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                cfg[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return cfg


def save_config(cfg):
    """Write the config file (creating its directory if needed)."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        fh.write(
            "# Configuration for krb-ticket-indicator.\n"
            "# Format: \"key = value\" per line; '#' starts a comment.\n\n"
            f"principal = {cfg.get('principal', '')}\n"
            f"ticket_lifetime = {cfg.get('ticket_lifetime', '')}\n"
            f"renew_lifetime = {cfg.get('renew_lifetime', RENEW_LIFETIME)}\n"
            f"renewable = {cfg.get('renewable', 'true')}\n"
            f"auto_renew = {cfg.get('auto_renew', 'false')}\n"
            f"warn_minutes = {cfg.get('warn_minutes', '30')}\n"
            f"show_principal = {cfg.get('show_principal', 'true')}\n"
            f"show_time = {cfg.get('show_time', 'true')}\n"
        )


def cfg_bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def cfg_int(value, default):
    try:
        return int(str(value).strip())
    except ValueError:
        return default


# Icons per state (symbolic -> adapts to the panel theme).
# Key-themed: Kerberos is key-based authentication.
ICONS = {
    "valid": "dialog-password-symbolic",   # key
    "soon": "dialog-warning-symbolic",     # below the warning threshold
    "expired": "emblem-important-symbolic",
    "none": "action-unavailable-symbolic",
}

# klist date formats (de + en).
DATE_FORMATS = ("%d.%m.%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m/%d/%y %H:%M:%S")
DATE_RE = re.compile(r"\d{2}[./]\d{2}[./]\d{2,4} \d{2}:\d{2}:\d{2}")


def parse_dt(token):
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(token, fmt)
        except ValueError:
            continue
    return None


def _status(state, principal="", expires=None, remaining=None,
            renewable=False, renew_until=None):
    return {"state": state, "principal": principal, "expires": expires,
            "remaining": remaining, "renewable": renewable,
            "renew_until": renew_until}


def read_ticket(warn_minutes=30):
    """Read klist and return a status dict.

    state: 'valid' | 'soon' | 'expired' | 'none'
    'soon' means less than warn_minutes of remaining lifetime.
    """
    # -s: exit code 0 => a valid TGT exists. Cheap and locale-independent.
    try:
        valid = subprocess.run(["klist", "-s"]).returncode == 0
    except FileNotFoundError:
        return _status("none")

    try:
        # -f also prints the ticket flags (needed to detect 'R' = renewable).
        out = subprocess.run(["klist", "-f"], capture_output=True, text=True).stdout
    except Exception:  # noqa: BLE001
        return _status("none")

    principal = ""
    expires = None
    renewable = False
    renew_until = None
    lines = out.splitlines()
    for i, line in enumerate(lines):
        low = line.lower()
        # "standard-principal" also matches the German-locale klist label.
        if low.startswith("standard-principal") or low.startswith("default principal"):
            principal = line.split(":", 1)[1].strip()
        elif "krbtgt/" in line:
            stamps = DATE_RE.findall(line)
            if len(stamps) >= 2:
                expires = parse_dt(stamps[1])  # 2nd stamp = Expires
            # The following indented line carries the flags and, for a
            # renewable ticket, the "renew until" timestamp. Flag letters are
            # not localized; 'R' means renewable.
            if i + 1 < len(lines):
                nxt = lines[i + 1]
                flags = re.search(r":\s*([A-Za-z]+)\s*$", nxt)
                if flags and "R" in flags.group(1).upper():
                    renewable = True
                ru = DATE_RE.findall(nxt)
                if ru:
                    renew_until = parse_dt(ru[0])

    if expires is None and not valid:
        return _status("none", principal=principal)

    remaining = (expires - datetime.now()) if expires else None

    if remaining is not None and remaining.total_seconds() <= 0:
        state = "expired"
    elif not valid:
        state = "expired"
    elif remaining is not None and remaining.total_seconds() < warn_minutes * 60:
        state = "soon"
    else:
        state = "valid"

    return _status(state, principal=principal, expires=expires,
                   remaining=remaining, renewable=renewable,
                   renew_until=renew_until)


def fmt_hms(delta):
    """Remaining lifetime as 'Hh MMm'. None or expired -> '0h 00m'."""
    if delta is None:
        return "0h 00m"
    secs = max(int(delta.total_seconds()), 0)
    h, rem = divmod(secs, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m:02d}m"


def _menu_item(label, icon_name, on_activate=None):
    """Menu item with a symbolic icon. The icon is sent to GNOME via dbusmenu
    (icon-name) and themes itself automatically."""
    item = Gtk.ImageMenuItem(label=label)
    item.set_image(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU))
    item.set_always_show_image(True)
    if on_activate:
        item.connect("activate", on_activate)
    return item


class KrbIndicator:
    def __init__(self):
        self.ind = AppIndicator.Indicator.new(
            INDICATOR_ID, ICONS["none"],
            AppIndicator.IndicatorCategory.SYSTEM_SERVICES,
        )
        self.ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.ind.set_title("Kerberos ticket")

        self.menu = Gtk.Menu()
        # Info rows deliberately without an icon (only actions get icons).
        self.item_principal = Gtk.MenuItem(label="–")
        self.item_principal.set_sensitive(False)
        self.item_expires = Gtk.MenuItem(label="–")
        self.item_expires.set_sensitive(False)
        self.item_remaining = Gtk.MenuItem(label="–")
        self.item_remaining.set_sensitive(False)
        self.item_renewable = Gtk.MenuItem(label="–")
        self.item_renewable.set_sensitive(False)
        # Shown only for renewable tickets (managed in refresh()).
        self.item_renew_until = Gtk.MenuItem(label="–")
        self.item_renew_until.set_sensitive(False)
        self.item_renew_until.set_no_show_all(True)

        item_refresh = _menu_item("Refresh", "view-refresh-symbolic",
                                  lambda _w: self.refresh())
        item_kinit = _menu_item("New ticket (kinit)",
                                "dialog-password-symbolic", self.on_kinit)
        self.item_renew = _menu_item("Renew ticket (kinit -R)",
                                     "media-playlist-repeat-symbolic",
                                     self.on_renew)
        self.item_destroy = _menu_item("Destroy ticket (kdestroy)",
                                       "edit-delete-symbolic",
                                       self.on_destroy_ticket)
        # Visibility is managed in refresh(): renew only when the ticket is
        # renewable, destroy only when a ticket exists. no_show_all keeps
        # show_all() from forcing them visible again.
        self.item_renew.set_no_show_all(True)
        self.item_destroy.set_no_show_all(True)
        item_settings = _menu_item("Settings", "preferences-system-symbolic",
                                   self.on_settings)
        item_quit = _menu_item("Quit", "application-exit-symbolic",
                               lambda _w: Gtk.main_quit())

        for w in (
            self.item_principal, self.item_expires, self.item_remaining,
            self.item_renewable, self.item_renew_until,
            item_refresh,
            Gtk.SeparatorMenuItem(), item_kinit, self.item_renew,
            self.item_destroy,
            Gtk.SeparatorMenuItem(), item_settings, item_quit,
        ):
            self.menu.append(w)
        self.menu.show_all()
        self.ind.set_menu(self.menu)

        self._flip = False
        self._auto_renew_expiry = None  # guard: auto-renew once per expiry
        self.refresh()
        # Refresh once more right after startup, when the indicator is
        # registered with the panel, so label/icon are correct immediately
        # instead of only after the first poll.
        GLib.timeout_add_seconds(1, lambda: (self.refresh(), False)[1])
        GLib.timeout_add_seconds(POLL_SECONDS, self._tick)

    def _tick(self):
        self.refresh()
        return True  # keep going

    def on_renew(self, _w):
        # Manual renew: notify on success and failure.
        self._renew(auto=False)

    def _renew(self, auto):
        # kinit -R extends a RENEWABLE ticket without a password, as long as
        # the "renew until" window is still open. Runs in the background.
        def worker():
            try:
                rc = subprocess.run(["kinit", "-R"],
                                    capture_output=True, text=True).returncode
            except FileNotFoundError:
                rc = 1
            GLib.idle_add(self._renew_done, rc, auto)

        threading.Thread(target=worker, daemon=True).start()

    def _renew_done(self, rc, auto):
        if rc == 0:
            self._notify("Kerberos ticket " + ("auto-renewed" if auto
                                               else "renewed"),
                         "Remaining: " + fmt_hms(read_ticket()["remaining"]))
        elif not auto:
            # Stay silent on auto-renew failures (the per-expiry guard already
            # prevents retries) -> only the manual action reports problems.
            self._notify("Renewal failed",
                         "Ticket is not renewable or the renew window is "
                         "closed — use “New ticket (kinit)”.")
        self.refresh()
        return False  # GLib.idle_add one-shot

    def _maybe_auto_renew(self, cfg, st):
        # Auto-renew a renewable ticket once it drops below the warning
        # threshold ('soon'). Only once per expiry: a success pushes the expiry
        # out (re-arming for the next cycle), a failure keeps it (no retry).
        if not cfg_bool(cfg["auto_renew"]):
            return
        if st["state"] != "soon" or not st.get("renewable"):
            return
        if st["expires"] == self._auto_renew_expiry:
            return
        self._auto_renew_expiry = st["expires"]
        self._renew(auto=True)

    @staticmethod
    def _dialog(title, icon_name, heading, subtitle="", ok_label="OK"):
        """Build a modern dialog: header-bar Cancel/<ok_label> buttons, a large
        icon on the left, heading + dim subtitle, and a form box on the right.
        Consistent margins and spacing. Returns (dialog, form_box)."""
        dlg = Gtk.Dialog(title=title, use_header_bar=True)
        dlg.set_resizable(False)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok = dlg.add_button(ok_label, Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        dlg.set_default_response(Gtk.ResponseType.OK)

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        outer.set_margin_top(20)
        outer.set_margin_bottom(20)
        outer.set_margin_start(20)
        outer.set_margin_end(20)

        icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.DIALOG)
        icon.set_pixel_size(48)
        icon.set_valign(Gtk.Align.START)
        outer.add(icon)

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        head = Gtk.Label(xalign=0)
        head.set_markup(f"<span size='large' weight='bold'>{heading}</span>")
        form.add(head)
        if subtitle:
            sub = Gtk.Label(label=subtitle, xalign=0)
            sub.set_line_wrap(True)
            sub.get_style_context().add_class("dim-label")
            form.add(sub)

        outer.add(form)
        dlg.get_content_area().add(outer)
        return dlg, form

    def _ask_principal(self, default, renewable_default):
        """Dialog for principal + password + renewable mode. Returns
        (principal, password, renewable) or None if cancelled. Empty principal
        => kinit default. The password is only kept in memory."""
        dlg, form = self._dialog(
            "New Kerberos ticket", "dialog-password-symbolic",
            "New Kerberos ticket",
            "Authenticate to obtain a Kerberos ticket.", ok_label="Get ticket")

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        grid.attach(Gtk.Label(label="Principal", xalign=0), 0, 0, 1, 1)
        e_principal = Gtk.Entry()
        e_principal.set_text(default)
        e_principal.set_placeholder_text("empty = kinit default")
        e_principal.set_width_chars(28)
        e_principal.set_hexpand(True)
        e_principal.set_activates_default(True)
        grid.attach(e_principal, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Password", xalign=0), 0, 1, 1, 1)
        e_pw = Gtk.Entry()
        e_pw.set_visibility(False)
        e_pw.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        e_pw.set_activates_default(True)
        grid.attach(e_pw, 1, 1, 1, 1)
        form.add(grid)

        renew_chk = Gtk.CheckButton(label="Renewable ticket (kinit -r)")
        renew_chk.set_active(renewable_default)
        form.add(renew_chk)

        dlg.show_all()
        (e_pw if default else e_principal).grab_focus()
        response = dlg.run()
        result = ((e_principal.get_text().strip(), e_pw.get_text(),
                   renew_chk.get_active())
                  if response == Gtk.ResponseType.OK else None)
        dlg.destroy()
        return result

    def on_settings(self, _w):
        """Settings window, grouped into 'New ticket' and 'Panel' sections.
        Writes the config file on Save."""
        cfg = load_config()
        dlg, form = self._dialog(
            "Settings", "preferences-system-symbolic", "Settings",
            "Defaults for new tickets and the panel display.", ok_label="Save")

        def section(title, top=14):
            lbl = Gtk.Label(xalign=0)
            lbl.set_markup(f"<b>{title}</b>")
            lbl.set_margin_top(top)
            form.add(lbl)

        def field(grid, row, label, text, placeholder, chars=22):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            entry = Gtk.Entry()
            entry.set_text(text)
            entry.set_placeholder_text(placeholder)
            entry.set_width_chars(chars)
            entry.set_hexpand(True)
            grid.attach(entry, 1, row, 1, 1)
            return entry

        # --- New ticket ---
        section("New ticket", top=4)
        g1 = Gtk.Grid(column_spacing=12, row_spacing=8)
        e_principal = field(g1, 0, "Default principal", cfg["principal"],
                            "empty = kinit default")
        e_tlife = field(g1, 1, "Ticket lifetime", cfg["ticket_lifetime"],
                        "empty = KDC default, e.g. 10h")
        e_rlife = field(g1, 2, "Renewable lifetime", cfg["renew_lifetime"],
                        "e.g. 7d, 1h30m (must be ≥ ticket lifetime)")
        form.add(g1)
        chk_renew = Gtk.CheckButton(label="Request renewable by default")
        chk_renew.set_active(cfg_bool(cfg["renewable"]))
        form.add(chk_renew)

        # --- Panel ---
        section("Panel")
        chk_name = Gtk.CheckButton(label="Show ticket name next to icon")
        chk_name.set_active(cfg_bool(cfg["show_principal"]))
        form.add(chk_name)
        chk_time = Gtk.CheckButton(label="Show remaining time next to icon")
        chk_time.set_active(cfg_bool(cfg["show_time"]))
        form.add(chk_time)
        g2 = Gtk.Grid(column_spacing=12, row_spacing=8)
        e_warn = field(g2, 0, "Warning threshold (min)", cfg["warn_minutes"],
                       "30", chars=6)
        form.add(g2)

        # --- Auto-renew ---
        section("Auto-renew")
        chk_auto = Gtk.CheckButton(
            label="Auto-renew renewable tickets below the warning threshold")
        chk_auto.set_active(cfg_bool(cfg["auto_renew"]))
        form.add(chk_auto)

        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            save_config({
                "principal": e_principal.get_text().strip(),
                "ticket_lifetime": e_tlife.get_text().strip(),
                "renew_lifetime": e_rlife.get_text().strip() or RENEW_LIFETIME,
                "renewable": "true" if chk_renew.get_active() else "false",
                "auto_renew": "true" if chk_auto.get_active() else "false",
                "warn_minutes": str(cfg_int(e_warn.get_text(), 30)),
                "show_principal": "true" if chk_name.get_active() else "false",
                "show_time": "true" if chk_time.get_active() else "false",
            })
            self.refresh()  # apply changes immediately
        dlg.destroy()

    def on_destroy_ticket(self, _w):
        # kdestroy clears the credential cache (invalidates the TGT).
        # No password needed and no confirmation -> "just kdestroy".
        try:
            rc = subprocess.run(["kdestroy"], capture_output=True,
                                text=True).returncode
        except FileNotFoundError:
            rc = 1
        if rc == 0:
            self._notify("Kerberos ticket destroyed",
                         "The credential cache was cleared.")
        else:
            self._notify("kdestroy failed",
                         "Could not clear the credential cache.")
        self.refresh()

    @staticmethod
    def _notify(title, body):
        # Desktop notification, best effort (no-op if notify-send is missing).
        try:
            subprocess.Popen(
                ["notify-send", "-a", "Kerberos ticket",
                 "-i", "dialog-password-symbolic", title, body])
        except FileNotFoundError:
            pass

    def on_kinit(self, _w):
        # Ask for principal + password + renewable mode, then run kinit in the
        # background. -r requests a RENEWABLE ticket so "Renew" works after.
        cfg = load_config()
        st = read_ticket()
        default = cfg["principal"] or (
            st["principal"].split("@")[0] if st["principal"] else "")
        result = self._ask_principal(default, cfg_bool(cfg["renewable"]))
        if result is None:
            return  # cancelled
        principal, password, renewable = result
        cmd = ["kinit"]
        if cfg["ticket_lifetime"]:          # -l: ticket lifetime
            cmd += ["-l", cfg["ticket_lifetime"]]
        if renewable:                        # -r: renewable lifetime (>= -l)
            cmd += ["-r", cfg["renew_lifetime"]]
        if principal:
            cmd.append(principal)
        self._run_kinit(cmd, password)

    def _run_kinit(self, cmd, password):
        # Run kinit in the background and feed the password via stdin (pipe).
        # The password never appears in argv -> invisible to `ps` and never in
        # any shell history. Runs in a thread so the UI stays responsive.
        def worker():
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    text=True)
                proc.communicate(input=password + "\n")
                rc = proc.returncode
            except FileNotFoundError:
                rc = 1
            GLib.idle_add(self._kinit_done, rc)

        threading.Thread(target=worker, daemon=True).start()

    def _kinit_done(self, rc):
        if rc == 0:
            self._notify("Kerberos ticket obtained",
                         "Remaining: " + fmt_hms(read_ticket()["remaining"]))
        else:
            self._notify("kinit failed",
                         "Wrong password or KDC error — check the principal "
                         "and try again.")
        self.refresh()
        return False  # GLib.idle_add one-shot

    def refresh(self):
        cfg = load_config()
        st = read_ticket(warn_minutes=cfg_int(cfg["warn_minutes"], 30))
        state = st["state"]
        self.ind.set_icon_full(ICONS.get(state, ICONS["none"]), state)

        # Panel label, "<name> <time>" — name and time toggle independently
        # (both off = icon only). The full principal incl. realm is in the menu.
        parts = []
        if cfg_bool(cfg["show_principal"]):
            parts.append((st["principal"].split("@")[0]
                          if st["principal"] else "") or "krb")
        if cfg_bool(cfg["show_time"]):
            parts.append(fmt_hms(st["remaining"]))
        text = " ".join(parts)
        # GNOME only re-renders the label when the FINAL value changes (equality
        # check in the appindicator extension). With a constant value we
        # alternately append an invisible zero-width space so it always changes.
        if text:
            self._flip = not self._flip
            text += "​" if self._flip else ""
        self.ind.set_label(text, "Kerberos")

        self.item_principal.set_label(st["principal"] or "No principal")
        self.item_expires.set_label(
            "Valid until: " + (st["expires"].strftime("%Y-%m-%d %H:%M")
                               if st["expires"] else "–")
        )
        self.item_remaining.set_label("Remaining: " + fmt_hms(st["remaining"]))
        self.item_renewable.set_label(
            "Renewable: " + ("–" if state == "none"
                             else "True" if st.get("renewable") else "False")
        )
        # Renew only works on a STILL-VALID renewable ticket — an expired
        # ticket cannot be renewed, even within its renew window. So gate the
        # renew action (and the "renew until" info) on valid/soon, not just on
        # the renewable flag. Destroy applies whenever a cache exists.
        renewable_now = bool(st.get("renewable")) and state in ("valid", "soon")
        renew_until = st.get("renew_until")
        show_until = renewable_now and renew_until is not None
        self.item_renew_until.set_visible(show_until)
        if show_until:
            self.item_renew_until.set_label(
                "Renew until: " + renew_until.strftime("%Y-%m-%d %H:%M"))

        self.item_renew.set_visible(renewable_now)
        self.item_destroy.set_visible(state != "none")

        self._maybe_auto_renew(cfg, st)
        return False


def acquire_single_instance():
    """Single-instance lock via flock. A second start exits immediately.

    The lock is held by the kernel and released automatically when the
    process ends (even on crash/SIGKILL) -> no stale lock file.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~/.cache")
    lock_fd = open(os.path.join(runtime, "krb-ticket-indicator.lock"), "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("krb-ticket-indicator is already running — exiting.", file=sys.stderr)
        sys.exit(0)
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    return lock_fd  # keep the reference, otherwise the lock is released


def main():
    _lock = acquire_single_instance()  # noqa: F841 (reference holds the lock)
    KrbIndicator()
    Gtk.main()


if __name__ == "__main__":
    main()
