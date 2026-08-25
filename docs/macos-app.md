# macOS app installer (`.pkg`)

`alle` ships as a **hermetic, self-contained** macOS menu-bar app, delivered as
a one-shot `.pkg`. One admin auth installs `/Applications/Alle.app`, which
contains everything it needs: the native tray, the bundled Python `alle` core,
and the pinned `sing-box` binary. There is no `curl|sh`, no separate `uv`
install, and no root helper by default.

## What the installer does

The `.pkg` payload places a self-contained `Alle.app` at `/Applications`. Its
`postinstall` simply launches it for the installing user. From there the app:

- registers a **per-user** login item (SMAppService) so the tray starts at login;
- starts the daemon from its **bundled core** on first run;
- keeps its state at `~/Library/Application Support/Alle` (not `~/.alle`).

The tray owns the daemon's lifetime symmetrically: it starts the daemon on
launch, and **Quit stops it**, so the data plane (sing-box, and TUN if it was
on) never outlives the menu-bar app. Use **Run Core at Login** if you want the
daemon supervised independently of the tray.

The **privileged TUN helper is opt-in** (it's a root LaunchDaemon, the one thing
that can't live in the app). The first time you enable **System VPN (TUN)**, the
tray offers to install it with a native admin-password prompt. You can decline
and TUN simply isn't available; per-app routing needs no helper.

## Build

From the repository root:

```bash
uv run --group macos python packaging/macos/build_pkg.py
```

Outputs:

```text
dist/macos/Alle-<version>-macos-<arch>.pkg
dist/macos/Alle-<version>-macos-<arch>.pkg.sha256
```

## Install

```bash
sudo installer -pkg dist/macos/Alle-<version>-macos-<arch>.pkg -target /
```

Then click the `alle` menu-bar item. The app is a menu-bar utility
(`LSUIElement`): no Dock icon.

## Uninstall

Run the bundled uninstaller **before** trashing the app (it uses the bundled
`alle` to stop/unregister the daemon and helper cleanly, then deletes the app):

```bash
# usual case (you never enabled TUN — no helper installed):
/Applications/Alle.app/Contents/Resources/uninstall.sh

# if you enabled TUN (the root helper is present):
sudo /Applications/Alle.app/Contents/Resources/uninstall.sh
```

Trashing `/Applications/Alle.app` removes the whole runtime (tray + core +
sing-box); only the small user-level residue remains — the per-user login item
registration, `~/Library/Application Support/Alle`, and the opt-in helper if you
installed it. `uninstall.sh` clears all of that.

## Coexistence with a CLI install

The hermetic app can coexist with a CLI install (Homebrew / `uv` / `pipx`).
They do **not** share configuration:

| | Hermetic app (`.pkg`) | CLI install |
| --- | --- | --- |
| State dir (`ALLE_HOME`) | `~/Library/Application Support/Alle` | `~/.alle` |
| Control API / Web UI port | generated per-install, its own | generated per-install, its own |
| Channel proxy ports | auto-reallocate on collision | auto-reallocate on collision |

Each daemon binds its own control-API port and Clash-API port (pinned under its
own state dir), and channel proxy ports reallocate automatically if the two
happen to pick the same one — so **per-app routing does not conflict**.

The only shared, system-wide resources are the **TUN device** (one system-wide
VPN at a time) and the **privileged helper** (one per machine, bound to one
`ALLE_HOME`). Don't enable TUN on both installs; if you switch which install
owns TUN, reinstall the helper for that install.

## Scope and signing

The `.pkg` and app are ad-hoc signed for sideloading — **not** Developer-ID
signed or notarized. A downloaded copy may trigger Gatekeeper; run it via
`sudo installer -pkg … -target /` (which authorizes implicitly) or right-click →
Open. `spctl --assess` is expected to fail for this phase; `codesign --verify`
is the local integrity check.
