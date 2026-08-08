import Foundation

/// Typed views over `/api/v1` payloads.
///
/// These decode from `[String: Any]` rather than adopting `Decodable`, and that
/// is deliberate. `Decodable` fails the whole payload when one field changes
/// shape; the app's contract with the daemon is that it may run a version ahead
/// or behind, so a payload it partly understands must still render. Every
/// initialiser here reads what it recognises, defaults what it does not, and
/// never throws.
///
/// Field names track `service.status_snapshot()` and friends.

// MARK: - Coercion helpers
//
// The daemon is Python and JSON-encodes ints, bools, and strings the way Python
// does; a field that is `1` today can be `true` tomorrow without either side
// being wrong. Coerce rather than pattern-match on one representation.

enum JSON {
    static func bool(_ value: Any?) -> Bool {
        if let value = value as? Bool { return value }
        if let value = value as? NSNumber { return value.boolValue }
        if let value = value as? String {
            return ["1", "true", "yes", "on"].contains(value.lowercased())
        }
        return false
    }

    static func int(_ value: Any?) -> Int? {
        if let value = value as? Int { return value }
        if let value = value as? NSNumber { return value.intValue }
        if let value = value as? String { return Int(value) }
        return nil
    }

    static func double(_ value: Any?) -> Double? {
        if let value = value as? Double { return value }
        if let value = value as? NSNumber { return value.doubleValue }
        if let value = value as? String { return Double(value) }
        return nil
    }

    static func string(_ value: Any?) -> String? {
        if let value = value as? String { return value.isEmpty ? nil : value }
        if let value = value as? NSNumber { return value.stringValue }
        return nil
    }

    static func object(_ value: Any?) -> [String: Any] {
        value as? [String: Any] ?? [:]
    }

    static func array(_ value: Any?) -> [[String: Any]] {
        value as? [[String: Any]] ?? []
    }
}

// MARK: - Status

struct DaemonInfo: Equatable, Sendable {
    var pid: Int?
    var version: String?
    var installedVersion: String?

    /// True when the running daemon and the installed package disagree — the
    /// CLI warns about this and the app surfaces the same thing.
    var hasVersionSkew: Bool {
        guard let version, let installedVersion else { return false }
        return version != installedVersion
    }

    init(_ raw: [String: Any]) {
        pid = JSON.int(raw["pid"])
        version = JSON.string(raw["version"])
        installedVersion = JSON.string(raw["installed_version"]) ?? JSON.string(raw["version"])
    }
}

struct RouterInfo: Equatable, Sendable {
    var port: Int?
    var address: String?
    var tun: Bool
    var killswitch: Bool
    var lan: Bool

    init(_ raw: [String: Any]) {
        port = JSON.int(raw["port"])
        address = JSON.string(raw["address"])
        tun = JSON.bool(raw["tun"])
        killswitch = JSON.bool(raw["killswitch"])
        lan = JSON.bool(raw["lan"])
    }
}

struct Channel: Equatable, Sendable, Identifiable {
    var provider: String
    var name: String
    var label: String?
    var port: Int?
    var country: String?
    var city: String?
    var stateLabel: String
    var state: RunState
    var enabled: Bool
    var ipv6: Bool
    var latencyMs: Double?
    var exitIP: String?
    var exitIPv6: String?
    var revision: Int?

    /// `provider/name` — the key the CLI and the API both address channels by,
    /// and what `chanKey()` builds in the Web UI.
    var id: String { "\(provider)/\(name)" }

    /// What the Web UI's `loc()` renders: city when it is a real city, else the
    /// country alone. The core emits `(Unknown)`/`(Any City)` placeholders that
    /// must not reach the user.
    var location: String {
        let placeholders = ["(Unknown)", "(Any City)"]
        if let city, !placeholders.contains(city), let country {
            return "\(city), \(country)"
        }
        return country ?? "—"
    }

    var displayName: String {
        if let label, !label.isEmpty { return label }
        return name
    }

    init(_ raw: [String: Any]) {
        provider = JSON.string(raw["provider"]) ?? "?"
        name = JSON.string(raw["name"]) ?? "?"
        label = JSON.string(raw["label"])
        port = JSON.int(raw["port_number"])
        country = JSON.string(raw["country"])
        city = JSON.string(raw["city"])
        stateLabel = JSON.string(raw["state"]) ?? "Unknown"
        state = RunState(label: stateLabel)
        enabled = JSON.bool(raw["enabled"])
        ipv6 = JSON.bool(raw["ipv6"])
        latencyMs = JSON.double(raw["latency_ms"])
        exitIP = JSON.string(raw["ip"])
        exitIPv6 = JSON.string(raw["ipv6_exit"])
        revision = JSON.int(raw["revision"])
    }
}

struct StatusSnapshot: Equatable, Sendable {
    var running: Bool
    var daemon: DaemonInfo
    var router: RouterInfo
    var channels: [Channel]
    var providerCount: Int
    var channelCount: Int

    static let empty = StatusSnapshot([:])

    /// The one-line channel health the tray title and the toolbar pill show —
    /// the Web UI's `makeChannelSummary`.
    var channelSummary: String {
        if channels.isEmpty { return "no channels" }
        let healthy = channels.filter { $0.state == .active }.count
        return "\(healthy)/\(channels.count) healthy"
    }

    /// The tray icon only distinguishes three situations, so collapse to those.
    var trayIcon: StatusIconKind {
        if !running { return .stopped }
        return router.tun ? .tun : .running
    }

    init(_ raw: [String: Any]) {
        running = JSON.bool(raw["running"])
        daemon = DaemonInfo(JSON.object(raw["daemon"]))
        router = RouterInfo(JSON.object(raw["router"]))
        channels = JSON.array(raw["channels"]).map(Channel.init)
        channelCount = JSON.int(raw["channel_count"]) ?? channels.count
        providerCount =
            JSON.int(raw["provider_count"]) ?? Set(channels.map(\.provider)).count
    }
}

// MARK: - Routing

struct RouteRule: Equatable, Sendable, Identifiable {
    var id: String
    var kind: String?
    var value: String?
    var target: String?

    init(_ raw: [String: Any]) {
        id = JSON.string(raw["id"]) ?? UUID().uuidString
        kind = JSON.string(raw["kind"]) ?? JSON.string(raw["type"])
        value = JSON.string(raw["value"]) ?? JSON.string(raw["match"])
        target = JSON.string(raw["target"])
    }
}

struct RuleSet: Equatable, Sendable, Identifiable {
    var id: String
    var name: String?
    var target: String?
    var order: Int?
    var ruleCount: Int?

    /// The Web UI's `rulesetDisplayName` — fall back to the id when unnamed.
    var displayName: String {
        if let name, !name.isEmpty { return name }
        return id
    }

    init(_ raw: [String: Any]) {
        id = JSON.string(raw["id"]) ?? UUID().uuidString
        name = JSON.string(raw["name"])
        target = JSON.string(raw["target"])
        order = JSON.int(raw["order"])
        ruleCount = JSON.int(raw["rule_count"]) ?? JSON.int(raw["count"])
    }
}

struct RoutesSnapshot: Equatable, Sendable {
    var rules: [RouteRule]
    var rulesets: [RuleSet]
    var killswitch: Bool
    var lan: Bool

    static let empty = RoutesSnapshot([:])

    /// `orderedRulesets` in the Web UI: explicit `order` wins, and anything
    /// without one keeps its payload position rather than jumping to the front.
    var orderedRulesets: [RuleSet] {
        rulesets.enumerated()
            .sorted { lhs, rhs in
                let l = lhs.element.order ?? lhs.offset
                let r = rhs.element.order ?? rhs.offset
                return l == r ? lhs.offset < rhs.offset : l < r
            }
            .map(\.element)
    }

    init(_ raw: [String: Any]) {
        rules = JSON.array(raw["rules"]).map(RouteRule.init)
        rulesets = JSON.array(raw["rulesets"]).map(RuleSet.init)
        killswitch = JSON.bool(raw["killswitch"])
        lan = JSON.bool(raw["lan"])
    }
}

// MARK: - Providers

struct Provider: Equatable, Sendable, Identifiable {
    var id: String
    var channelCount: Int?
    var hasCredentials: Bool

    init(_ raw: [String: Any]) {
        id = JSON.string(raw["id"]) ?? JSON.string(raw["name"]) ?? "?"
        channelCount = JSON.int(raw["channel_count"]) ?? JSON.int(raw["channels"])
        hasCredentials = JSON.bool(raw["has_credentials"]) || JSON.bool(raw["configured"])
    }
}

// MARK: - Bundle / backup

struct BackupStatus: Equatable, Sendable {
    var enabled: Bool
    var lastRun: String?
    var path: String?
    var count: Int?

    static let empty = BackupStatus([:])

    init(_ raw: [String: Any]) {
        enabled = JSON.bool(raw["enabled"])
        lastRun = JSON.string(raw["last_run"]) ?? JSON.string(raw["last"])
        path = JSON.string(raw["path"]) ?? JSON.string(raw["dir"])
        count = JSON.int(raw["count"])
    }
}

// MARK: - Upgrade

struct UpgradeCheck: Equatable, Sendable {
    var current: String?
    var latest: String?
    var available: Bool
    var channel: String?
    var message: String?

    init(_ raw: [String: Any]) {
        current = JSON.string(raw["current"]) ?? JSON.string(raw["installed"])
        latest = JSON.string(raw["latest"])
        channel = JSON.string(raw["channel"])
        message = JSON.string(raw["message"]) ?? JSON.string(raw["detail"])
        if let explicit = raw["available"] ?? raw["update_available"] {
            available = JSON.bool(explicit)
        } else if let current, let latest {
            available = current != latest
        } else {
            available = false
        }
    }
}

// MARK: - Helper ownership

/// How the machine's single privileged TUN helper relates to *this* install.
///
/// Mirrors `alle.helper.probe()`. `foreign` is the case the UI must name rather
/// than swallow: another install (typically a CLI one) owns the helper, so TUN
/// is unavailable here until it is explicitly taken over.
enum HelperOwnership: Equatable, Sendable {
    case absent
    case stale
    case foreign(home: String?)
    case ours

    init(probe raw: [String: Any]) {
        switch JSON.string(raw["state"]) {
        case "ok": self = .ours
        case "foreign": self = .foreign(home: JSON.string(raw["home"]))
        case "stale": self = .stale
        default: self = .absent
        }
    }

    var blocksTun: Bool {
        switch self {
        case .ours: return false
        case .absent, .stale, .foreign: return true
        }
    }
}
