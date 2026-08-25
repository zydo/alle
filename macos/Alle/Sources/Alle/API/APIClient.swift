import CryptoKit
import Foundation

protocol HTTPSession: Sendable {
    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

extension URLSession: HTTPSession {
    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await data(for: request, delegate: nil)
        guard let http = response as? HTTPURLResponse else {
            throw CompanionError.daemonUnavailable(
                "cannot reach the alle daemon: non-HTTP response")
        }
        return (data, http)
    }
}

enum CompanionError: Error, LocalizedError, Equatable {
    case daemonUnavailable(String)
    case apiError(String)

    var errorDescription: String? {
        switch self {
        case .daemonUnavailable(let message), .apiError(let message):
            return message
        }
    }
}

struct Endpoint: Equatable {
    var address: String
    var secret: String
    var host: String
}

final class CompanionClient: Sendable {
    private let timeout: TimeInterval
    private let session: any HTTPSession
    private let environment: @Sendable (String) -> String?
    private let homeDirectory: @Sendable () -> URL
    private let randomBytes: @Sendable (Int) throws -> [UInt8]
    private let now: @Sendable () -> Int

    init(
        timeout: TimeInterval = 4.0,
        session: any HTTPSession = URLSession.shared,
        environment: @escaping @Sendable (String) -> String? = {
            ProcessInfo.processInfo.environment[$0]
        },
        homeDirectory: @escaping @Sendable () -> URL = {
            FileManager.default.homeDirectoryForCurrentUser
        },
        randomBytes: @escaping @Sendable (Int) throws -> [UInt8] = CompanionClient
            .secureRandomBytes,
        now: @escaping @Sendable () -> Int = { Int(Date().timeIntervalSince1970) }
    ) {
        self.timeout = timeout
        self.session = session
        self.environment = environment
        self.homeDirectory = homeDirectory
        self.randomBytes = randomBytes
        self.now = now
    }

    func endpoint() throws -> Endpoint {
        guard let cfg = readControlAPI() else {
            throw CompanionError.daemonUnavailable(
                "alle daemon is not configured yet (no control endpoint). Start it: alle start"
            )
        }
        let address = clientAddress(for: cfg)
        let secret = try apiSecret(for: cfg)
        return Endpoint(address: address, secret: secret, host: cfg.host)
    }

    func healthOK() async -> Bool {
        guard let api = try? endpoint() else {
            return false
        }
        return await challengeOK(api)
    }

    // MARK: - Reads

    func status() async throws -> StatusSnapshot {
        StatusSnapshot(try await request("GET", "status"))
    }

    func routes() async throws -> RoutesSnapshot {
        RoutesSnapshot(try await request("GET", "routes"))
    }

    func providers() async throws -> [Provider] {
        let payload = try await request("GET", "providers")
        return JSON.array(payload["providers"] ?? payload["items"]).map(Provider.init)
    }

    func providerCatalog() async throws -> [String] {
        let payload = try await request("GET", "providers/catalog")
        let entries = JSON.array(payload["providers"] ?? payload["catalog"])
        if !entries.isEmpty {
            return entries.compactMap { JSON.string($0["id"] ?? $0["name"]) }
        }
        return (payload["providers"] as? [String]) ?? []
    }

    func channels() async throws -> [Channel] {
        let payload = try await request("GET", "channels")
        return JSON.array(payload["channels"] ?? payload["items"]).map(Channel.init)
    }

    func locations() async throws -> [String: Any] {
        try await request("GET", "locations")
    }

    func metrics(channel: String? = nil) async throws -> [String: Any] {
        let query = channel.map { "?channel=\($0.urlQueryEncoded)" } ?? ""
        return try await request("GET", "metrics\(query)")
    }

    func logs(lines: Int = 500) async throws -> String {
        let payload = try await request("GET", "logs?lines=\(lines)")
        return JSON.string(payload["text"]) ?? ""
    }

    func upgradeCheck(prerelease: Bool = false) async throws -> UpgradeCheck {
        UpgradeCheck(try await request("GET", "upgrade/check?prerelease=\(prerelease)"))
    }

    func backupStatus() async throws -> BackupStatus {
        BackupStatus(try await request("GET", "backup"))
    }

    func exportConfig() async throws -> [String: Any] {
        try await request("GET", "export")
    }

    func geoStatus() async throws -> [String: Any] {
        try await request("GET", "routes/geo")
    }

    func geoCategories(kind: String, query: String?) async throws -> [String] {
        var path = "routes/geo/categories?kind=\(kind.urlQueryEncoded)"
        if let query, !query.isEmpty {
            path += "&q=\(query.urlQueryEncoded)"
        }
        let payload = try await request("GET", path)
        if let names = payload["categories"] as? [String] { return names }
        return JSON.array(payload["categories"]).compactMap { JSON.string($0["name"]) }
    }

    // MARK: - Lifecycle

    @discardableResult
    func start() async throws -> [String: Any] {
        try await request("POST", "lifecycle/start", body: [:])
    }

    @discardableResult
    func stop() async throws -> [String: Any] {
        try await request("POST", "lifecycle/stop", body: [:])
    }

    @discardableResult
    func restart() async throws -> [String: Any] {
        try await request("POST", "lifecycle/restart", body: [:])
    }

    @discardableResult
    func upgrade() async throws -> [String: Any] {
        // The daemon replaces itself and defers its own restart until after this
        // response flushes, so a longer ceiling here is about the swap, not slack.
        try await request("POST", "upgrade", body: [:], timeout: 120)
    }

    // MARK: - Toggles

    @discardableResult
    func setTun(_ enabled: Bool) async throws -> [String: Any] {
        // Bringing TUN up installs routes and may wait on the helper.
        try await request("POST", "tun", body: ["enabled": enabled], timeout: 60)
    }

    @discardableResult
    func setKillswitch(_ enabled: Bool) async throws -> [String: Any] {
        try await request("POST", "routes/killswitch", body: ["enabled": enabled])
    }

    @discardableResult
    func setLAN(_ enabled: Bool) async throws -> [String: Any] {
        try await request("POST", "routes/lan", body: ["enabled": enabled])
    }

    // MARK: - Channels

    @discardableResult
    func setChannelEnabled(_ channel: String, _ enabled: Bool) async throws -> [String: Any] {
        try await request(
            "POST", "channels/\(channel.urlPathEncoded)/enabled", body: ["enabled": enabled])
    }

    @discardableResult
    func setChannelLabel(_ channel: String, _ label: String) async throws -> [String: Any] {
        try await request(
            "POST", "channels/\(channel.urlPathEncoded)/label", body: ["label": label])
    }

    @discardableResult
    func addChannel(_ body: [String: Any]) async throws -> [String: Any] {
        try await request("POST", "channels", body: body, timeout: 60)
    }

    @discardableResult
    func removeChannel(_ channel: String) async throws -> [String: Any] {
        try await request("DELETE", "channels/\(channel.urlPathEncoded)")
    }

    /// Probe (and optionally speed-test) one channel or all of them.
    ///
    /// Speed tests pull real traffic through the tunnel, so the ceiling is
    /// generous — the Web UI shows a per-row spinner for the same reason.
    @discardableResult
    func test(channel: String? = nil, speed: Bool = false) async throws -> [String: Any] {
        var body: [String: Any] = ["speed": speed]
        if let channel { body["channel"] = channel }
        return try await request("POST", "test", body: body, timeout: speed ? 300 : 90)
    }

    // MARK: - Providers

    @discardableResult
    func addProvider(_ body: [String: Any]) async throws -> [String: Any] {
        try await request("POST", "providers", body: body, timeout: 120)
    }

    @discardableResult
    func removeProvider(_ provider: String) async throws -> [String: Any] {
        try await request("DELETE", "providers/\(provider.urlPathEncoded)")
    }

    @discardableResult
    func setProviderToken(_ provider: String, _ body: [String: Any]) async throws -> [String: Any] {
        try await request("POST", "providers/\(provider.urlPathEncoded)/token", body: body)
    }

    // MARK: - Routes

    @discardableResult
    func addRuleSet(_ body: [String: Any]) async throws -> [String: Any] {
        try await request("POST", "routes/rulesets", body: body, timeout: 60)
    }

    @discardableResult
    func removeRuleSet(_ id: String) async throws -> [String: Any] {
        try await request("DELETE", "routes/rulesets/\(id.urlPathEncoded)")
    }

    @discardableResult
    func renameRuleSet(_ id: String, to name: String) async throws -> [String: Any] {
        try await request("POST", "routes/rulesets/\(id.urlPathEncoded)/rename", body: ["name": name])
    }

    @discardableResult
    func retargetRuleSet(_ id: String, to target: String) async throws -> [String: Any] {
        try await request(
            "POST", "routes/rulesets/\(id.urlPathEncoded)/target", body: ["target": target])
    }

    @discardableResult
    func updateRuleSet(_ id: String) async throws -> [String: Any] {
        try await request("POST", "routes/rulesets/\(id.urlPathEncoded)/update", body: [:], timeout: 60)
    }

    @discardableResult
    func reorderRoutes(_ order: [String]) async throws -> [String: Any] {
        try await request("POST", "routes/reorder", body: ["order": order])
    }

    @discardableResult
    func moveRoute(_ id: String, direction: String) async throws -> [String: Any] {
        try await request("POST", "routes/move", body: ["id": id, "direction": direction])
    }

    @discardableResult
    func removeRoute(_ id: String) async throws -> [String: Any] {
        try await request("DELETE", "routes/\(id.urlPathEncoded)")
    }

    func trace(_ body: [String: Any]) async throws -> [String: Any] {
        try await request("POST", "routes/trace", body: body, timeout: 60)
    }

    @discardableResult
    func refreshGeo() async throws -> [String: Any] {
        try await request("POST", "routes/geo/refresh", body: [:], timeout: 300)
    }

    @discardableResult
    func setGeoSource(_ source: String) async throws -> [String: Any] {
        try await request("POST", "routes/geo/source", body: ["source": source])
    }

    // MARK: - Bundle

    @discardableResult
    func importConfig(_ body: [String: Any]) async throws -> [String: Any] {
        try await request("POST", "import", body: body, timeout: 120)
    }

    func validate(_ body: [String: Any]) async throws -> [String: Any] {
        try await request("POST", "validate", body: body, timeout: 60)
    }

    @discardableResult
    func backupNow() async throws -> [String: Any] {
        try await request("POST", "backup/now", body: [:], timeout: 60)
    }

    private func challengeOK(_ api: Endpoint) async -> Bool {
        let nonce: String
        do {
            nonce = try randomTokenURLSafe(byteCount: 16)
        } catch {
            return false
        }
        guard let url = URL(string: "http://\(api.address)/health?nonce=\(nonce)") else {
            return false
        }
        var request = URLRequest(url: url, timeoutInterval: 1)
        request.httpMethod = "GET"
        do {
            let (data, response) = try await session.data(for: request)
            guard (200..<300).contains(response.statusCode), data.count <= 4096 else {
                return false
            }
            guard
                let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                let proof = object["proof"] as? String
            else {
                return false
            }
            return constantTimeEqual(proof, healthProof(secret: api.secret, nonce: nonce))
        } catch {
            return false
        }
    }

    private func request(
        _ method: String, _ path: String, body: [String: Any]? = nil,
        timeout override: TimeInterval? = nil
    ) async throws -> [String: Any] {
        let timeout = override ?? self.timeout
        let api = try endpoint()
        guard await challengeOK(api) else {
            throw CompanionError.daemonUnavailable(
                "no alle daemon is answering the health challenge at \(api.address) "
                    + "(not running, or a foreign process holds the port). Start it: alle start"
            )
        }
        guard
            let url = URL(
                string:
                    "http://\(api.address)/api/v1/\(path.trimmingCharacters(in: CharacterSet(charactersIn: "/")))"
            )
        else {
            throw CompanionError.daemonUnavailable("API configuration error: invalid API URL")
        }
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue("Bearer \(api.secret)", forHTTPHeaderField: "Authorization")
        // Redundant with the Host URLSession derives from the URL, but stated
        // explicitly because the daemon refuses any non-loopback Host.
        request.setValue(api.address, forHTTPHeaderField: "Host")
        if let body {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }

        do {
            let (data, response) = try await session.data(for: request)
            if response.statusCode == 404 {
                throw CompanionError.apiError("endpoint /\(path) not available on this daemon")
            }
            guard (200..<300).contains(response.statusCode) else {
                throw CompanionError.apiError(
                    errorMessage(
                        from: data,
                        fallback: HTTPURLResponse.localizedString(
                            forStatusCode: response.statusCode)))
            }
            guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw CompanionError.daemonUnavailable(
                    "cannot reach the alle daemon: invalid JSON response")
            }
            return object
        } catch let error as CompanionError {
            throw error
        } catch {
            throw CompanionError.daemonUnavailable("cannot reach the alle daemon: \(error)")
        }
    }

    private func errorMessage(from data: Data, fallback: String) -> String {
        guard
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let message = object["error"] as? String
        else {
            return fallback
        }
        return message
    }

    private struct ControlAPI {
        var address: String
        var secret: String
        var host: String
    }

    private func readControlAPI() -> ControlAPI? {
        let url = stateDirectory().appendingPathComponent("control_api.json")
        guard let data = try? Data(contentsOf: url),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return nil
        }
        return validControlAPI(object)
    }

    private func stateDirectory() -> URL {
        if let raw = environment("ALLE_HOME"), !raw.isEmpty {
            return URL(fileURLWithPath: raw)
        }
        return homeDirectory().appendingPathComponent(".alle")
    }

    private func validControlAPI(_ object: [String: Any]) -> ControlAPI? {
        guard
            let address = object["address"] as? String,
            let secret = object["secret"] as? String,
            let host = object["host"] as? String
        else {
            return nil
        }
        let parts = address.split(separator: ":", omittingEmptySubsequences: false)
        guard parts.count == 2,
            parts[0] == "127.0.0.1",
            let port = Int(parts[1]),
            port > 0,
            port <= 65_535,
            String(port) == parts[1]
        else {
            return nil
        }
        guard secret.range(of: #"^[0-9a-f]{64}$"#, options: .regularExpression) != nil else {
            return nil
        }
        guard host.range(of: #"^alle-[0-9a-f]{8}\.localhost$"#, options: .regularExpression) != nil
        else {
            return nil
        }
        return ControlAPI(address: address, secret: secret, host: host)
    }

    /// The `host:port` to actually connect to.
    ///
    /// `control_api.json` records the contract address; an operator-set
    /// `ALLE_API_LISTEN` can move the bind, and a wildcard bind is reached over
    /// loopback. An unset or unparseable override leaves the contract address
    /// as-is.
    private func clientAddress(for api: ControlAPI) -> String {
        guard
            let raw = environment("ALLE_API_LISTEN")?.trimmingCharacters(
                in: .whitespacesAndNewlines), !raw.isEmpty,
            let parsed = parseListen(raw),
            let contractPort = api.address.split(separator: ":").last.flatMap({ Int($0) })
        else {
            return api.address
        }
        let host = parsed.host == "0.0.0.0" ? "127.0.0.1" : parsed.host
        return "\(host):\(parsed.port ?? contractPort)"
    }

    private func parseListen(_ raw: String) -> (host: String, port: Int?)? {
        let pieces = raw.split(separator: ":", omittingEmptySubsequences: false)
        guard pieces.count <= 2 else {
            return nil
        }
        let host = String(pieces[0]).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !host.isEmpty, !host.contains("/"), !host.contains(" ") else {
            return nil
        }
        if pieces.count == 1 {
            return (host, nil)
        }
        guard let port = Int(pieces[1]), port >= 1, port <= 65_535 else {
            return nil
        }
        return (host, port)
    }

    private func apiSecret(for api: ControlAPI) throws -> String {
        let env = environment("ALLE_API_SECRET")
        let path = environment("ALLE_API_SECRET_FILE")
        if env != nil, path != nil {
            throw CompanionError.daemonUnavailable(
                "API configuration error: both ALLE_API_SECRET and ALLE_API_SECRET_FILE are set — set exactly one"
            )
        }
        let value: String
        if let path {
            do {
                value = try String(contentsOfFile: path).trimmingCharacters(
                    in: .whitespacesAndNewlines)
            } catch {
                throw CompanionError.daemonUnavailable(
                    "API configuration error: ALLE_API_SECRET_FILE \(path) is unreadable: \(error)")
            }
        } else if let env {
            value = env.trimmingCharacters(in: .whitespacesAndNewlines)
        } else {
            return api.secret
        }
        guard value.count >= 16 else {
            throw CompanionError.daemonUnavailable(
                "API configuration error: the injected API secret is too short — use at least 16 characters (e.g. openssl rand -hex 32)"
            )
        }
        return value
    }

    private func canonicalHost(for api: Endpoint) -> String {
        let port = api.address.split(separator: ":").last ?? ""
        return "\(api.host):\(port)"
    }

    private func mintLoginToken(_ secret: String) throws -> String {
        let nonce = base64URL(try Data(randomBytes(9)))
        let payload = "login:\(now()):\(nonce)"
        return "\(base64URL(Data(payload.utf8))).\(sign(secret: secret, message: payload))"
    }

    private func randomTokenURLSafe(byteCount: Int) throws -> String {
        base64URL(try Data(randomBytes(byteCount)))
    }

    private func healthProof(secret: String, nonce: String) -> String {
        sign(secret: secret, message: "health:\(nonce)")
    }

    private func sign(secret: String, message: String) -> String {
        let key = SymmetricKey(data: Data(secret.utf8))
        let mac = HMAC<SHA256>.authenticationCode(for: Data(message.utf8), using: key)
        return base64URL(Data(mac))
    }

    private static func secureRandomBytes(_ count: Int) throws -> [UInt8] {
        var bytes = [UInt8](repeating: 0, count: count)
        let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        if status != errSecSuccess {
            throw CompanionError.daemonUnavailable("cannot generate random bytes")
        }
        return bytes
    }
}

func base64URL(_ data: Data) -> String {
    data.base64EncodedString()
        .replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
}

func constantTimeEqual(_ lhs: String, _ rhs: String) -> Bool {
    let left = Array(lhs.utf8)
    let right = Array(rhs.utf8)
    let count = max(left.count, right.count)
    var diff = left.count ^ right.count
    for index in 0..<count {
        let l = index < left.count ? left[index] : 0
        let r = index < right.count ? right[index] : 0
        diff |= Int(l ^ r)
    }
    return diff == 0
}

extension String {
    /// Percent-encode for a path segment. Channel ids are `provider/name`, and
    /// that slash is a real path separator the API expects — so only the pieces
    /// are escaped, not the separator between them.
    var urlPathEncoded: String {
        split(separator: "/", omittingEmptySubsequences: false)
            .map { piece in
                piece.addingPercentEncoding(
                    withAllowedCharacters: .alphanumerics.union(CharacterSet(charactersIn: "-._~"))
                ) ?? String(piece)
            }
            .joined(separator: "/")
    }

    var urlQueryEncoded: String {
        addingPercentEncoding(
            withAllowedCharacters: .alphanumerics.union(CharacterSet(charactersIn: "-._~"))
        ) ?? self
    }
}
