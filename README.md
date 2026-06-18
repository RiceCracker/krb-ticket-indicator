# Kerberos Ticket Indicator

Plain panel indicator (top right) that shows:

- **whether** a Kerberos ticket (TGT) exists,
- **whether** it is valid,
- the **principal (without realm) and the remaining lifetime** right next
  to the icon, e.g. `alice/admin 23h 45m`.

The time always carries `h`/`m` units, so it reads as a duration rather
than a clock time: expired or no ticket → `0h 00m`. The principal prefix
makes it recognizable as the Kerberos ticket; the full principal incl.
realm is shown in the menu. If no principal is available the prefix falls
back to `krb`. The state is distinguished by the icon. Polls `klist`
every 30 s.

## States

| Panel label              | Icon (key-themed)   | Meaning                       |
|--------------------------|---------------------|-------------------------------|
| `alice/admin 23h 45m`    | dialog-password     | valid TGT, remaining lifetime |
| `alice/admin 0h 18m`     | dialog-warning      | valid, below warn threshold   |
| `alice/admin 0h 00m`     | emblem-important    | ticket present, but expired   |
| `krb 0h 00m`             | action-unavailable  | no cache / no TGT             |

## Installation

```bash
cd ~/git/krb-ticket-indicator
make install
```

`make install` checks the dependencies, copies the script to
`~/.local/bin/`, creates the autostart entry in `~/.config/autostart/`
and an app launcher in `~/.local/share/applications/`, and starts the
indicator immediately. No root required.

It then starts automatically at every login. To start it manually (e.g.
after **Quit**), launch **Kerberos Ticket Indicator** from the app grid,
run `make start`, or run `~/.local/bin/krb-ticket-indicator.py`. The
single-instance lock prevents duplicates.

| Target           | Effect                                    |
|------------------|-------------------------------------------|
| `make install`   | check deps, install (script+config+autostart), start |
| `make uninstall` | stop and remove all files (incl. config)  |
| `make reinstall` | uninstall + install                       |
| `make restart`   | restart the indicator                     |
| `make status`    | klist status + running process            |

## Dependencies

`python3-gi`, the AppIndicator GIR (Ayatana preferred) and `klist`
(`krb5-user`). On missing packages `make install` prints the matching
command for the detected distro:

| Distro        | Packages |
|---------------|----------|
| Debian/Ubuntu | `python3-gi gir1.2-ayatanaappindicator3-0.1 krb5-user` |
| Fedora        | `python3-gobject libayatana-appindicator-gtk3 krb5-workstation` |
| Arch          | `python-gobject libayatana-appindicator krb5` |
| openSUSE      | `python3-gobject libayatana-appindicator3-1 krb5-client` |

GNOME only shows AppIndicator items with the AppIndicator extension
enabled (on by default under Ubuntu), otherwise:

```bash
gnome-extensions enable ubuntu-appindicators@ubuntu.com
```

On KDE Plasma, XFCE, Cinnamon, MATE and Budgie the indicator works
without an extension — there the StatusNotifier/AppIndicator is native.

## Menu

Icons appear only on the actions; the status rows have none.

- **Principal / Valid until / Remaining / Renewable** – current status;
  a **Renew until** row appears for renewable tickets
- **Refresh** – read state immediately
- **New ticket (kinit)** – opens a dialog for principal + password (and
  whether to request a renewable ticket), then runs `kinit` in the
  background
- **Renew ticket (kinit -R)** – extends a renewable ticket without a
  password (shown only while the ticket is renewable)
- **Destroy ticket (kdestroy)** – clears the credential cache immediately,
  no confirmation (shown only while a ticket exists)
- **Settings** – grouped into *New ticket* (default principal, ticket and
  renewable lifetimes, renewable default), *Panel* (warning threshold, and
  whether to show the ticket name / remaining time next to the icon), and
  *Auto-renew* (renew renewable tickets automatically below the threshold)
- **Quit** – close the indicator

## Configuration

Settings live in `~/.config/krb-ticket-indicator.conf` (`key = value`).
Edit it via the **Settings** menu entry or by hand:

```ini
# New ticket
principal       =        # default principal, prefilled in the dialog
ticket_lifetime =        # kinit -l; empty = KDC default (e.g. 10h)
renew_lifetime  = 7d      # kinit -r; must be >= ticket lifetime
renewable       = true    # request renewable by default

# Panel
warn_minutes    = 30      # below this: warning icon (and auto-renew trigger)
show_principal  = true    # show ticket name next to the icon
show_time       = true    # show remaining time next to the icon

# Auto-renew
auto_renew      = false   # auto 'kinit -R' when renewable and below threshold
```

`ticket_lifetime`/`renew_lifetime` are only requests — the KDC may clamp
them to its policy. `show_principal` and `show_time` toggle the two parts
of the panel label independently (both off = icon only). With `auto_renew`,
a renewable ticket is renewed automatically once it drops below
`warn_minutes` (once per expiry, so no hammering; expired tickets are not
renewed).

`make install` creates it if absent (an existing file is kept);
`make uninstall` removes it.

## Layout

```
krb-ticket-indicator/
├── bin/krb-ticket-indicator.py           # the indicator
├── share/krb-ticket-indicator.desktop.in # autostart template (__EXEC__)
├── share/krb-ticket-indicator.conf       # default config (installed if absent)
├── Makefile                              # install / uninstall / status …
└── README.md
```

## How it works

- Validity comes from `klist -s` (exit code, locale-independent, robust).
- Remaining lifetime is parsed from the `krbtgt/…` line; date parsing
  handles the German (`16.06.2026 14:47:20`) and English klist formats.
- Panel label and menu share the same `fmt_hms` function (`Hh MMm`,
  expired/None → `0h 00m`).
- Renewability is read from the ticket flags (`klist -f`, the `R` flag).

### Password handling

`New ticket` asks for the password in its dialog and feeds it to `kinit`
via **stdin** (a pipe), never as a command-line argument. So the password
is invisible to `ps`, never lands in any shell history, and is not written
to disk. `kinit` runs in a background thread, keeping the UI responsive.

### Why the label "flickers" (zero-width-space toggle)

GNOME shows tray labels via the `ubuntu-appindicators` extension — but
only when the **final value changes**. Both `libayatana-appindicator` and
the extension (`appIndicator.js`, async property read with an equality
check) suppress unchanged values. With a constant remaining time
(expired = `0h 00m`, or the same minute across two polls) the label
disappears after a drop event (lock screen, shell reload) and never
returns.

Fix: on every poll an invisible **zero-width space (U+200B)** is appended
alternately. Visually identical, but the value is guaranteed to change →
GNOME re-renders reliably. (Background: `set_label` support was broken in
Ubuntu Noble for a while, LP #2059818, fixed since package `58-1`.)

### Single instance (flock)

`acquire_single_instance()` holds a `flock` on
`$XDG_RUNTIME_DIR/krb-ticket-indicator.lock`. A second start fails on the
lock and exits immediately — whether launched via autostart, `make start`
or manually. The kernel releases the lock automatically when the process
ends (even on crash/SIGKILL), so there is no stale lock file.

### Clean stopping

`make` only stops real python interpreters (`ps comm == python*`), so it
never hits a shell — even if its command line contains the script name as
text (otherwise `pkill -f` would kill itself).
