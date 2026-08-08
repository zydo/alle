import SwiftUI

/// Entry point, engine toggles, and helper ownership — the Web UI's
/// `renderEntry` and `renderTun` panels.
struct DashboardView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.theme) private var theme

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.gap) {
            Panel(title: "Entrypoint", subtitle: "Where local clients point.") {
                if let port = model.status.router.port {
                    LabeledContent("Router") {
                        Text("127.0.0.1:\(String(port))")
                            .font(.system(size: 12, design: .monospaced))
                            .textSelection(.enabled)
                            .foregroundStyle(theme.text)
                    }
                } else {
                    EmptyHint(text: "No router entrypoint configured yet.")
                }
                LabeledContent("Channels") {
                    Text(model.status.channelSummary).foregroundStyle(theme.muted)
                }
                LabeledContent("Providers") {
                    Text("\(model.status.providerCount)")
                        .tabularNumbers()
                        .foregroundStyle(theme.muted)
                }
                if model.status.daemon.hasVersionSkew {
                    Text(
                        "The running daemon is \(model.status.daemon.version ?? "?") but "
                            + "\(model.status.daemon.installedVersion ?? "?") is installed. "
                            + "Restart to pick up the new version."
                    )
                    .font(.system(size: 11))
                    .foregroundStyle(theme.warn)
                }
            }

            Panel(title: "Engine", subtitle: "System-wide routing controls.") {
                helperNotice

                ToggleRow(
                    title: "TUN",
                    detail: tunDetail,
                    isOn: model.status.router.tun,
                    busy: model.isBusy("tun"),
                    // Turning TUN off must stay available even when another
                    // install owns the helper, or a user could never bring an
                    // already-up tunnel down from here.
                    disabled: !model.status.router.tun && model.helper.blocksTun
                ) { value in Task { await model.setTun(value) } }

                Divider().overlay(theme.line)

                ToggleRow(
                    title: "Kill switch",
                    detail: "Block traffic that would otherwise leave outside the tunnel.",
                    isOn: model.status.router.killswitch,
                    busy: model.isBusy("killswitch")
                ) { value in Task { await model.setKillswitch(value) } }

                Divider().overlay(theme.line)

                ToggleRow(
                    title: "Allow LAN",
                    detail: "Keep local-network destinations off the tunnel.",
                    isOn: model.status.router.lan,
                    busy: model.isBusy("lan")
                ) { value in Task { await model.setLAN(value) } }
            }

            Panel(title: "Daemon") {
                HStack(spacing: 10) {
                    RowButton(title: "Restart", busy: model.isBusy("restart")) {
                        Task { await model.setTun(model.status.router.tun) }
                    }
                    RowButton(title: "Probe all", busy: model.isBusy("test:all")) {
                        Task { await model.test(nil, speed: false) }
                    }
                    Spacer()
                }
            }
        }
        .task { await model.refreshHelperOwnership() }
    }

    private var tunDetail: String {
        switch model.helper {
        case .ours, .absent:
            return "Route all system traffic through the tunnel."
        case .stale:
            return "The installed helper predates home scoping and must be reinstalled."
        case .foreign(let home):
            return "Owned by another install\(home.map { " at \($0)" } ?? "")."
        }
    }

    /// Helper ownership as a first-class state, not an error toast.
    ///
    /// There is one privileged helper per machine, bound to one ALLE_HOME. When
    /// a CLI install owns it, TUN simply cannot work here until it is taken
    /// over — so say that, and offer the one action that resolves it.
    @ViewBuilder private var helperNotice: some View {
        switch model.helper {
        case .ours:
            EmptyView()
        case .absent:
            noticeBox(
                text: "Enabling TUN will ask for administrator access once to install the "
                    + "privileged helper.",
                accent: theme.faint, action: nil)
        case .stale:
            noticeBox(
                text: "The installed TUN helper is too old to identify which install it serves. "
                    + "Reinstall it to continue.",
                accent: theme.warn, action: ("Reinstall", { Task { await model.takeOverHelper() } }))
        case .foreign(let home):
            noticeBox(
                text: "TUN is owned by another alle install\(home.map { " at \($0)" } ?? ""). "
                    + "Taking it over gives this app control; the other install loses TUN until "
                    + "it reinstalls. Turn TUN off there first — a takeover is refused while it "
                    + "is holding a tunnel up.",
                accent: theme.warn,
                action: ("Take over", { Task { await model.takeOverHelper() } }))
        }
    }

    @ViewBuilder private func noticeBox(
        text: String, accent: Color, action: (String, () -> Void)?
    ) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Text(text)
                .font(.system(size: 11))
                .foregroundStyle(theme.muted)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 8)
            if let action {
                RowButton(title: action.0, busy: model.isBusy("helper"), action: action.1)
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 6).fill(theme.panel2))
        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(accent.opacity(0.5), lineWidth: 1))
    }
}
