import SwiftUI

/// The window shell: a source list, a native toolbar, and the selected screen.
///
/// The Web UI puts navigation in a horizontal masthead. That reads as a web page
/// inside a native window, so the structure here is a standard macOS sidebar
/// while the *styling* — colours, type, component shapes — stays the Web UI's.
struct RootView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.colorScheme) private var colorScheme
    @State private var screen: Screen? = .dashboard

    enum Screen: String, CaseIterable, Identifiable, Hashable {
        case dashboard, channels, routes, bundle, logs
        var id: String { rawValue }

        var title: String {
            switch self {
            case .dashboard: return "Dashboard"
            case .channels: return "Channels"
            case .routes: return "Routing"
            case .bundle: return "Bundle"
            case .logs: return "Logs"
            }
        }

        var symbol: String {
            switch self {
            case .dashboard: return "gauge"
            case .channels: return "point.3.connected.trianglepath.dotted"
            case .routes: return "arrow.triangle.branch"
            case .bundle: return "shippingbox"
            case .logs: return "text.alignleft"
            }
        }
    }

    private var theme: Theme { .of(colorScheme) }

    var body: some View {
        NavigationSplitView {
            List(Screen.allCases, selection: $screen) { item in
                NavigationLink(value: item) {
                    Label(item.title, systemImage: item.symbol)
                }
            }
            .navigationSplitViewColumnWidth(min: 168, ideal: 190, max: 240)
        } detail: {
            ScrollView {
                content
                    .padding(Theme.gap)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .background(theme.ink)
        }
        .environment(\.theme, theme)
        .toolbar { toolbarContent }
        .overlay(alignment: .bottomTrailing) {
            if let toast = model.toast {
                ToastView(toast: toast)
                    .environment(\.theme, theme)
                    .padding(Theme.gap)
                    .transition(.opacity)
                    .task(id: toast.id) {
                        try? await Task.sleep(nanoseconds: 4_000_000_000)
                        model.toast = nil
                    }
            }
        }
        .task { await model.refreshHelperOwnership() }
    }

    @ViewBuilder private var content: some View {
        switch screen ?? .dashboard {
        case .dashboard: DashboardView()
        case .channels: ChannelsView()
        case .routes: RoutesView()
        case .bundle: BundleView()
        case .logs: LogsView()
        }
    }

    @ToolbarContentBuilder private var toolbarContent: some ToolbarContent {
        ToolbarItem(placement: .primaryAction) {
            HStack(spacing: 10) {
                if let version = model.status.daemon.version {
                    Button {
                        Task { await model.checkForUpdates() }
                    } label: {
                        Text("v\(version)")
                            .font(.system(size: 11))
                            .tabularNumbers()
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(theme.faint)
                    .help("Check for updates")
                }
                StatePill(state: pillState, text: pillText)
                    .environment(\.theme, theme)
            }
        }
    }

    private var pillState: RunState {
        switch model.isReachable {
        case .none: return .pending
        case .some(false): return .failed
        case .some(true): return model.status.running ? .active : .disabled
        }
    }

    private var pillText: String {
        switch model.isReachable {
        case .none: return "connecting"
        case .some(false): return "disconnected"
        case .some(true): return model.status.running ? "running" : "stopped"
        }
    }
}
