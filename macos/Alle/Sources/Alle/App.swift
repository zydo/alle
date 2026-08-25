import AppKit
import SwiftUI

/// Owns the status item, the window, and the Dock-presence policy.
///
/// The app ships `LSUIElement`, so it starts with no Dock icon and no menu bar
/// of its own. That is right at rest — it is a menu-bar utility — but wrong
/// while a window is open, where the user expects Cmd-W, a File menu, and a Dock
/// icon to come back to. So activation policy is switched at runtime rather than
/// fixed in Info.plist.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    static let showWindowNotification = Notification.Name("io.github.zydo.alle.showWindow")

    private var statusItem: NSStatusItem?
    private var window: NSWindow?
    private let model = AppModel()
    private let loginItem = LoginItem()
    private var refreshTask: Task<Void, Never>?

    func applicationDidFinishLaunching(_ notification: Notification) {
        installStatusItem()
        startRefreshing()

        DistributedNotificationCenter.default().addObserver(
            self, selector: #selector(showWindow),
            name: Self.showWindowNotification, object: nil)

        // Start the daemon from the bundled core if it is not already up. The
        // app is the thing the user launched, so it owns bringing the runtime
        // online — and `quit` is symmetric with it (see applicationWillTerminate).
        Task { await model.ensureDaemonRunning() }
    }

    func applicationWillTerminate(_ notification: Notification) {
        refreshTask?.cancel()
        // Symmetric with the auto-start above: the app started the daemon, so
        // quitting the app stops it. Synchronous on purpose — the process is
        // going away and an async stop would be cut off mid-flight.
        model.stopDaemonBlocking()
    }

    // MARK: - Status item

    private func installStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = StatusIcons.image(for: .stopped)
        item.button?.image?.isTemplate = true
        item.menu = buildMenu()
        statusItem = item
    }

    private func buildMenu() -> NSMenu {
        let menu = NSMenu()
        menu.delegate = self

        let open = NSMenuItem(
            title: "Open Alle", action: #selector(showWindow), keyEquivalent: "")
        open.target = self
        menu.addItem(open)
        menu.addItem(.separator())

        // Status line — not clickable, just the summary the icon cannot carry.
        let summary = NSMenuItem(title: "Loading…", action: nil, keyEquivalent: "")
        summary.isEnabled = false
        summary.tag = MenuTag.summary.rawValue
        menu.addItem(summary)
        menu.addItem(.separator())

        for (title, tag, action) in [
            ("TUN", MenuTag.tun, #selector(toggleTun)),
            ("Kill switch", MenuTag.killswitch, #selector(toggleKillswitch)),
        ] {
            let entry = NSMenuItem(title: title, action: action, keyEquivalent: "")
            entry.target = self
            entry.tag = tag.rawValue
            menu.addItem(entry)
        }
        menu.addItem(.separator())

        let login = NSMenuItem(
            title: loginItem.status.menuTitle, action: #selector(toggleLoginItem),
            keyEquivalent: "")
        login.target = self
        login.tag = MenuTag.loginItem.rawValue
        menu.addItem(login)
        menu.addItem(.separator())

        let quit = NSMenuItem(
            title: "Quit Alle", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(quit)
        return menu
    }

    private enum MenuTag: Int {
        case summary = 1
        case tun = 2
        case killswitch = 3
        case loginItem = 4
    }

    private func refreshMenu() {
        guard let menu = statusItem?.menu else { return }
        let status = model.status
        menu.item(withTag: MenuTag.summary.rawValue)?.title =
            switch model.isReachable {
            case .none: "Connecting…"
            case .some(false): "Daemon unreachable"
            case .some(true):
                "\(status.running ? "Running" : "Stopped") — \(status.channelSummary)"
            }
        menu.item(withTag: MenuTag.tun.rawValue)?.state = status.router.tun ? .on : .off
        menu.item(withTag: MenuTag.killswitch.rawValue)?.state =
            status.router.killswitch ? .on : .off
        if let entry = menu.item(withTag: MenuTag.loginItem.rawValue) {
            let login = loginItem.status
            entry.state = login.isChecked ? .on : .off
            entry.title = login.menuTitle
        }

        statusItem?.button?.image = StatusIcons.image(for: status.trayIcon)
        statusItem?.button?.image?.isTemplate = true
    }

    // MARK: - Polling

    private func startRefreshing() {
        refreshTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.model.refresh()
                self?.refreshMenu()
                // 3s is the Web UI's dashboard cadence; RefreshCoordinator keeps
                // a slow request from stacking behind the next tick.
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
    }

    // MARK: - Window + Dock presence

    @objc func showWindow() {
        if window == nil {
            let hosting = NSHostingController(
                rootView: RootView().environmentObject(model))
            let created = NSWindow(contentViewController: hosting)
            created.title = "Alle"
            created.setContentSize(NSSize(width: 1_020, height: 680))
            created.contentMinSize = NSSize(width: 720, height: 480)
            created.styleMask = [.titled, .closable, .miniaturizable, .resizable]
            created.center()
            created.isReleasedWhenClosed = false
            created.delegate = self
            window = created
        }
        // Become a regular app *before* ordering front: doing it after leaves the
        // window behind the previously-active app on the first open.
        setRegularActivation(true)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Switch between menu-bar-only and full app presence.
    ///
    /// `setActivationPolicy` is only honoured off the launch path, and flipping
    /// to `.regular` steals focus, so this is never called during
    /// `applicationDidFinishLaunching` — the app comes up quiet and only becomes
    /// a Dock citizen when a window actually exists.
    private func setRegularActivation(_ regular: Bool) {
        let wanted: NSApplication.ActivationPolicy = regular ? .regular : .accessory
        guard NSApp.activationPolicy() != wanted else { return }
        NSApp.setActivationPolicy(wanted)
    }
}

extension AppDelegate: NSMenuDelegate {
    func menuWillOpen(_ menu: NSMenu) {
        refreshMenu()
    }
}

extension AppDelegate: NSWindowDelegate {
    func windowWillClose(_ notification: Notification) {
        // Back to a quiet menu-bar app once the last window goes. Deferred to the
        // next runloop turn: the window is still in `NSApp.windows` during
        // `windowWillClose`, so counting here would always see one left open.
        DispatchQueue.main.async { [weak self] in
            let visible = NSApp.windows.contains { $0.isVisible && $0 !== self?.window }
            if !visible {
                self?.setRegularActivation(false)
            }
        }
    }
}

extension AppDelegate {
    @objc fileprivate func toggleTun() {
        Task { await model.setTun(!model.status.router.tun) }
    }

    @objc fileprivate func toggleKillswitch() {
        Task { await model.setKillswitch(!model.status.router.killswitch) }
    }

    @objc fileprivate func toggleLoginItem() {
        do {
            try loginItem.toggle()
        } catch {
            // Registration can be refused (approval revoked, bundle unsigned in
            // a way SMAppService rejects). Point at Login Items rather than
            // failing silently, since the user can fix it there.
            loginItem.openLoginItemsSettings()
        }
        refreshMenu()
    }
}
