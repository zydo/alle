import Combine
import Foundation

/// The single observable store every screen reads from.
///
/// Mirrors how the Web UI works: one poll refreshes a snapshot, actions fire and
/// then re-poll rather than mutating local state optimistically. That costs a
/// round trip but means the UI never claims something the daemon did not do —
/// which matters here because most actions (TUN, kill switch, channel start) can
/// fail in the engine well after the API returns 200.
@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var status: StatusSnapshot = .empty
    @Published private(set) var routes: RoutesSnapshot = .empty
    @Published private(set) var providers: [Provider] = []
    @Published private(set) var backup: BackupStatus = .empty
    @Published private(set) var helper: HelperOwnership = .absent
    @Published private(set) var upgrade: UpgradeCheck?
    @Published private(set) var logText: String = ""

    /// Nil until the first poll resolves, so the UI can distinguish "starting up"
    /// from "the daemon is genuinely down" and not flash an error on launch.
    @Published private(set) var isReachable: Bool?

    /// Keys currently in flight, so rows can spin individually — the Web UI's
    /// `busy` set and `chanBusy()`.
    @Published private(set) var busy: Set<String> = []
    @Published var toast: Toast?

    private let client: CompanionClient
    private let core: CoreProcess
    /// Guards against ticks stacking: the poll timer keeps firing while a slow
    /// refresh is in flight, and overlapping refreshes would interleave their
    /// writes to `status`.
    private var isRefreshing = false

    init(client: CompanionClient = CompanionClient(), core: CoreProcess = CoreProcess()) {
        self.client = client
        self.core = core
    }

    struct Toast: Identifiable, Equatable {
        enum Kind: Equatable {
            case info
            case error
        }
        let id = UUID()
        var kind: Kind
        var message: String
    }

    // MARK: - Polling

    /// One refresh cycle. Coalesced, so a slow speed test cannot stack ticks.
    func refresh() async {
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }

        do {
            let snapshot = try await client.status()
            status = snapshot
            isReachable = true
            // Routes are cheap and every screen's header depends on them; the
            // rest are pulled by the screen that needs them.
            routes = (try? await client.routes()) ?? routes
        } catch {
            isReachable = false
        }
    }

    func refreshProviders() async {
        providers = (try? await client.providers()) ?? providers
    }

    func refreshBackup() async {
        backup = (try? await client.backupStatus()) ?? backup
    }

    func refreshLogs(lines: Int) async {
        await run("logs") { [self] in
            logText = try await client.logs(lines: lines)
        }
    }

    func checkForUpdates() async {
        await run("upgrade-check") { [self] in
            upgrade = try await client.upgradeCheck()
            if let upgrade, upgrade.available, let latest = upgrade.latest {
                toast = Toast(kind: .info, message: "Version \(latest) is available.")
            } else {
                toast = Toast(kind: .info, message: "alle is up to date.")
            }
        }
    }

    // MARK: - Daemon lifecycle

    func ensureDaemonRunning() async {
        if await client.healthOK() { return }
        _ = try? await core.startDaemon()
        await refresh()
    }

    /// Stop the daemon on the way out of `applicationWillTerminate`, where there
    /// is no runloop left to await on.
    nonisolated func stopDaemonBlocking() {
        core.stopDaemonBlocking()
    }

    // MARK: - Toggles

    func setTun(_ enabled: Bool) async {
        // Turning TUN *on* is the only direction that needs the machine-wide
        // helper; turning it off must always be allowed, or a user whose helper
        // got taken over could never bring their tunnel down.
        if enabled, helper.blocksTun {
            await refreshHelperOwnership()
            if case .foreign(let home) = helper {
                toast = Toast(
                    kind: .error,
                    message:
                        "TUN is owned by another alle install\(home.map { " (\($0))" } ?? ""). "
                        + "Take it over from the TUN panel first.")
                return
            }
        }
        await run("tun") { [self] in
            try await client.setTun(enabled)
            await refresh()
        }
    }

    func setKillswitch(_ enabled: Bool) async {
        await run("killswitch") { [self] in
            try await client.setKillswitch(enabled)
            await refresh()
        }
    }

    func setLAN(_ enabled: Bool) async {
        await run("lan") { [self] in
            try await client.setLAN(enabled)
            await refresh()
        }
    }

    // MARK: - Channels

    func setChannelEnabled(_ channel: Channel, _ enabled: Bool) async {
        await run(channel.id) { [self] in
            try await client.setChannelEnabled(channel.id, enabled)
            await refresh()
        }
    }

    func relabel(_ channel: Channel, to label: String) async {
        await run("\(channel.id):label") { [self] in
            try await client.setChannelLabel(channel.id, label)
            await refresh()
        }
    }

    func test(_ channel: Channel?, speed: Bool) async {
        let key = channel.map { "\($0.id):\(speed ? "speed" : "probe")" } ?? "test:all"
        await run(key) { [self] in
            _ = try await client.test(channel: channel?.id, speed: speed)
            await refresh()
        }
    }

    func removeChannel(_ channel: Channel) async {
        await run(channel.id) { [self] in
            try await client.removeChannel(channel.id)
            await refresh()
        }
    }

    // MARK: - Routing

    func moveRoute(_ rule: RouteRule, direction: String) async {
        await run("route:\(rule.id)") { [self] in
            try await client.moveRoute(rule.id, direction: direction)
            routes = try await client.routes()
        }
    }

    func removeRoute(_ rule: RouteRule) async {
        await run("route:\(rule.id)") { [self] in
            try await client.removeRoute(rule.id)
            routes = try await client.routes()
        }
    }

    func renameRuleSet(_ set: RuleSet, to name: String) async {
        await run("ruleset:\(set.id)") { [self] in
            try await client.renameRuleSet(set.id, to: name)
            routes = try await client.routes()
        }
    }

    func retargetRuleSet(_ set: RuleSet, to target: String) async {
        await run("ruleset:\(set.id)") { [self] in
            try await client.retargetRuleSet(set.id, to: target)
            routes = try await client.routes()
        }
    }

    func updateRuleSet(_ set: RuleSet) async {
        await run("ruleset:\(set.id)") { [self] in
            try await client.updateRuleSet(set.id)
            routes = try await client.routes()
            toast = Toast(kind: .info, message: "Updated \(set.displayName).")
        }
    }

    func removeRuleSet(_ set: RuleSet) async {
        await run("ruleset:\(set.id)") { [self] in
            try await client.removeRuleSet(set.id)
            routes = try await client.routes()
        }
    }

    func refreshGeo() async {
        await run("geo") { [self] in
            _ = try await client.refreshGeo()
            toast = Toast(kind: .info, message: "Geo data refreshed.")
        }
    }

    func trace(host: String) async -> String? {
        var result: String?
        await run("trace") { [self] in
            let payload = try await client.trace(["host": host])
            result =
                JSON.string(payload["target"]) ?? JSON.string(payload["result"])
                ?? String(describing: payload)
        }
        return result
    }

    // MARK: - Bundle

    func backupNow() async {
        await run("backup") { [self] in
            _ = try await client.backupNow()
            await refreshBackup()
            toast = Toast(kind: .info, message: "Backup written.")
        }
    }

    func exportConfig() async -> String? {
        var text: String?
        await run("export") { [self] in
            let payload = try await client.exportConfig()
            let data = try JSONSerialization.data(
                withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
            text = String(data: data, encoding: .utf8)
        }
        return text
    }

    func validate(_ text: String) async {
        await run("validate") { [self] in
            guard let data = text.data(using: .utf8),
                let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            else {
                toast = Toast(kind: .error, message: "That is not a JSON object.")
                return
            }
            _ = try await client.validate(object)
            toast = Toast(kind: .info, message: "Configuration is valid.")
        }
    }

    func importConfig(_ text: String) async {
        await run("import") { [self] in
            guard let data = text.data(using: .utf8),
                let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            else {
                toast = Toast(kind: .error, message: "That is not a JSON object.")
                return
            }
            _ = try await client.importConfig(object)
            await refresh()
            toast = Toast(kind: .info, message: "Configuration imported.")
        }
    }

    // MARK: - Helper ownership

    func refreshHelperOwnership() async {
        helper = await core.helperOwnership()
    }

    /// Take the machine's single helper over for this install.
    ///
    /// Runs behind an admin prompt because it writes to /Library/LaunchDaemons.
    /// The core refuses a takeover while the *other* install is holding TUN up,
    /// and that refusal is surfaced verbatim rather than second-guessed here.
    func takeOverHelper() async {
        await run("helper") { [self] in
            let result = try await core.installHelper(takeover: true)
            await refreshHelperOwnership()
            toast = Toast(
                kind: result.status == 0 ? .info : .error,
                message: result.status == 0
                    ? "This install now owns the TUN helper."
                    : result.combinedOutput)
        }
    }

    // MARK: - Plumbing

    func isBusy(_ key: String) -> Bool {
        busy.contains(key)
    }

    /// Run one action with a busy key held and any failure surfaced as a toast,
    /// so no call site has to repeat that.
    private func run(_ key: String, _ body: () async throws -> Void) async {
        busy.insert(key)
        defer { busy.remove(key) }
        do {
            try await body()
        } catch {
            toast = Toast(kind: .error, message: error.localizedDescription)
        }
    }
}
