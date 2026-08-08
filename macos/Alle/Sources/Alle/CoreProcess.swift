import Foundation

struct CoreProcessResult: Sendable, Equatable {
    var status: Int32
    var stdout: String
    var stderr: String

    var combinedOutput: String {
        [stdout, stderr].filter { !$0.isEmpty }.joined(separator: "\n")
    }
}

enum CoreProcessError: Error, LocalizedError, Equatable {
    case unavailable(String)
    case timedOut(String)

    var errorDescription: String? {
        switch self {
        case .unavailable(let message), .timedOut(let message):
            return message
        }
    }
}

/// The narrow subprocess surface: everything the app can do over `/api/v1` goes
/// over `/api/v1`.
///
/// Only three things genuinely cannot. Starting the daemon has no API (there is
/// nothing listening yet), stopping it on quit must outlive the runloop, and
/// installing the privileged helper needs an admin prompt. Anything else added
/// here should be an API call instead — two paths to the same capability is how
/// the tray-era code ended up with duplicated, diverging logic.
struct CoreProcess: Sendable {
    var executable: URL?
    var resourceURL: URL?
    var environment: [String: String]

    init(
        executable: URL? = CoreProcess.defaultExecutable(),
        resourceURL: URL? = Bundle.main.resourceURL,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.executable = executable
        self.resourceURL = resourceURL
        self.environment = environment
    }

    /// The bundled wrapper, never a PATH lookup.
    ///
    /// Resolving `alle` from PATH would find a Homebrew/uv CLI install and drive
    /// *its* state directory — the exact cross-contamination the hermetic bundle
    /// exists to prevent.
    static func defaultExecutable() -> URL? {
        guard let resources = Bundle.main.resourceURL else { return nil }
        let wrapper = resources.appendingPathComponent("bin/alle")
        return FileManager.default.isExecutableFile(atPath: wrapper.path) ? wrapper : nil
    }

    var isAvailable: Bool {
        guard let executable else { return false }
        return FileManager.default.isExecutableFile(atPath: executable.path)
    }

    @discardableResult
    func startDaemon() async throws -> CoreProcessResult {
        try await run(["start"], timeout: 60)
    }

    /// Stop the daemon synchronously, for `applicationWillTerminate`.
    func stopDaemonBlocking() {
        guard let executable, isAvailable else { return }
        let process = Process()
        process.executableURL = executable
        process.arguments = ["stop"]
        process.environment = processEnvironment()
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try? process.run()
        // Bounded: a wedged stop must not hold up quit indefinitely. macOS gives
        // an app a few seconds at terminate, so leave headroom under that.
        let deadline = Date().addingTimeInterval(5)
        while process.isRunning && Date() < deadline {
            usleep(50_000)
        }
        if process.isRunning { process.terminate() }
    }

    /// How the machine's helper relates to this install.
    func helperOwnership() async -> HelperOwnership {
        guard let result = try? await run(["helper", "status", "--json"], timeout: 8),
            result.status == 0,
            let data = result.stdout.data(using: .utf8),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return .absent
        }
        // `helper status` reports the plist's bound home; compare it to ours to
        // classify, which works even when the helper is installed but not
        // answering.
        guard JSON.bool(object["installed"]) else { return .absent }
        guard let served = JSON.string(object["serves_home"]) else { return .stale }
        return served == stateDirectory() ? .ours : .foreign(home: served)
    }

    /// Install the privileged helper behind an admin prompt.
    ///
    /// `osascript … with administrator privileges` is the supported way to get
    /// one from an unsigned, sideloaded app; a signed build would use
    /// SMJobBless/SMAppService instead.
    func installHelper(takeover: Bool) async throws -> CoreProcessResult {
        guard let executable, isAvailable else {
            throw CoreProcessError.unavailable("the bundled alle core is missing from this bundle")
        }
        let command =
            "\(shellQuoted(executable.path)) helper install" + (takeover ? " --takeover" : "")
        let script =
            "do shell script \(appleScriptQuoted(command)) with administrator privileges"
        return try await runProcess(
            executable: URL(fileURLWithPath: "/usr/bin/osascript"),
            args: ["-e", script],
            timeout: 180,
            environment: processEnvironment())
    }

    private func run(_ args: [String], timeout: TimeInterval) async throws -> CoreProcessResult {
        guard let executable, isAvailable else {
            throw CoreProcessError.unavailable("the bundled alle core is missing from this bundle")
        }
        return try await runProcess(
            executable: executable, args: args, timeout: timeout,
            environment: processEnvironment())
    }

    private func stateDirectory() -> String {
        if let home = environment["ALLE_HOME"], !home.isEmpty { return home }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Alle").path
    }

    /// The environment a bundled child must inherit so it stays inside the app.
    func processEnvironment() -> [String: String] {
        var env = environment
        guard let resources = resourceURL else { return env }
        env["ALLE_SERVICE_OWNER"] = "macos-app"
        env["ALLE_SERVICE_PREFIX"] = resources.path
        if env["ALLE_HOME"]?.isEmpty ?? true {
            env["ALLE_HOME"] = stateDirectory()
        }
        return env
    }
}

private func runProcess(
    executable: URL, args: [String], timeout: TimeInterval, environment: [String: String]
) async throws -> CoreProcessResult {
    try await withCheckedThrowingContinuation { continuation in
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = executable
            process.arguments = args
            process.environment = environment
            let out = Pipe()
            let err = Pipe()
            process.standardOutput = out
            process.standardError = err
            do {
                try process.run()
            } catch {
                continuation.resume(
                    throwing: CoreProcessError.unavailable(
                        "could not run \(executable.lastPathComponent): \(error)"))
                return
            }
            // Drain concurrently with waiting: a child that fills the 64 KiB pipe
            // buffer blocks forever if we wait first and read after.
            var stdoutData = Data()
            var stderrData = Data()
            let group = DispatchGroup()
            for (pipe, sink) in [(out, { stdoutData = $0 }), (err, { stderrData = $0 })] {
                group.enter()
                DispatchQueue.global(qos: .utility).async {
                    sink(pipe.fileHandleForReading.readDataToEndOfFile())
                    group.leave()
                }
            }
            let deadline = Date().addingTimeInterval(timeout)
            while process.isRunning && Date() < deadline {
                usleep(50_000)
            }
            if process.isRunning {
                process.terminate()
                group.wait()
                continuation.resume(
                    throwing: CoreProcessError.timedOut(
                        "\(executable.lastPathComponent) \(args.joined(separator: " ")) "
                            + "did not finish within \(Int(timeout))s"))
                return
            }
            group.wait()
            continuation.resume(
                returning: CoreProcessResult(
                    status: process.terminationStatus,
                    stdout: String(decoding: stdoutData, as: UTF8.self)
                        .trimmingCharacters(in: .whitespacesAndNewlines),
                    stderr: String(decoding: stderrData, as: UTF8.self)
                        .trimmingCharacters(in: .whitespacesAndNewlines)))
        }
    }
}

/// Single-quote for /bin/sh: close, escape, reopen around embedded quotes.
func shellQuoted(_ value: String) -> String {
    "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
}

/// Double-quote for AppleScript, which only escapes backslash and quote.
func appleScriptQuoted(_ value: String) -> String {
    "\""
        + value.replacingOccurrences(of: "\\", with: "\\\\")
        .replacingOccurrences(of: "\"", with: "\\\"") + "\""
}
