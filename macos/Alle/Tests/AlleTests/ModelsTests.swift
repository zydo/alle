import Testing

@testable import Alle

/// The version-skew contract is the reason these models decode defensively, so
/// the tests are mostly about payloads a strict decoder would reject.

@Test func statusDecodesTheDocumentedShape() {
    let status = StatusSnapshot([
        "running": true,
        "router": ["port": 1080, "tun": true, "killswitch": false, "lan": true],
        "daemon": ["pid": 42, "version": "0.1.14", "installed_version": "0.1.14"],
        "channels": [
            ["provider": "protonvpn", "name": "nl-1", "state": "Active", "enabled": true,
             "ip": "1.2.3.4", "latency_ms": 31.5, "country": "Netherlands", "city": "Amsterdam"]
        ],
        "channel_count": 1,
        "provider_count": 1,
    ])

    #expect(status.running)
    #expect(status.router.tun)
    #expect(status.router.port == 1080)
    #expect(status.channels.count == 1)
    #expect(status.channels[0].id == "protonvpn/nl-1")
    #expect(status.channels[0].state == .active)
    #expect(status.channels[0].location == "Amsterdam, Netherlands")
    #expect(status.trayIcon == .tun)
}

@Test func statusSurvivesAnEmptyPayload() {
    let status = StatusSnapshot([:])
    #expect(!status.running)
    #expect(status.channels.isEmpty)
    #expect(status.channelSummary == "no channels")
    #expect(status.trayIcon == .stopped)
}

@Test func statusSurvivesWrongTypes() {
    // A daemon that starts sending strings where ints were, or vice versa, must
    // not blank the UI.
    let status = StatusSnapshot([
        "running": 1,
        "router": ["port": "1080", "tun": "true"],
        "channels": "not-an-array",
    ])
    #expect(status.running)
    #expect(status.router.port == 1080)
    #expect(status.router.tun)
    #expect(status.channels.isEmpty)
}

@Test func unknownChannelStateStillRenders() {
    let channel = Channel(["provider": "p", "name": "n", "state": "Warp-driving"])
    #expect(channel.state == .unknown)
    #expect(channel.stateLabel == "Warp-driving")
}

@Test(arguments: [
    ("Active", RunState.active),
    ("Disabled", RunState.disabled),
    ("Pending", RunState.pending),
    ("Reconnecting (2)", RunState.reconnecting),
    ("Reconnect failed", RunState.failed),
])
func coreStateLabelsClassify(_ label: String, _ expected: RunState) {
    #expect(RunState(label: label) == expected)
}

@Test func placeholderCitiesFallBackToCountry() {
    // The core emits these placeholders; they must never reach the user.
    for placeholder in ["(Unknown)", "(Any City)"] {
        let channel = Channel([
            "provider": "p", "name": "n", "city": placeholder, "country": "Japan",
        ])
        #expect(channel.location == "Japan")
    }
}

@Test func channelSummaryCountsOnlyActive() {
    let status = StatusSnapshot([
        "channels": [
            ["provider": "a", "name": "1", "state": "Active"],
            ["provider": "a", "name": "2", "state": "Pending"],
            ["provider": "b", "name": "3", "state": "Active"],
        ]
    ])
    #expect(status.channelSummary == "2/3 healthy")
    #expect(status.providerCount == 2)
}

@Test func versionSkewIsDetected() {
    let skewed = DaemonInfo(["version": "0.1.13", "installed_version": "0.1.14"])
    #expect(skewed.hasVersionSkew)
    let matched = DaemonInfo(["version": "0.1.14", "installed_version": "0.1.14"])
    #expect(!matched.hasVersionSkew)
    // Absent fields must not read as skew.
    #expect(!DaemonInfo([:]).hasVersionSkew)
}

@Test func rulesetsOrderByExplicitOrderThenPosition() {
    let routes = RoutesSnapshot([
        "rulesets": [
            ["id": "c", "order": 2],
            ["id": "a"],
            ["id": "b", "order": 1],
        ]
    ])
    // "a" has no explicit order, so it keeps its payload position (index 1)
    // rather than sorting to the front as a 0 would.
    #expect(routes.orderedRulesets.map(\.id) == ["b", "a", "c"])
}

@Test func helperOwnershipClassifiesAndGates() {
    #expect(HelperOwnership(probe: ["state": "ok"]) == .ours)
    #expect(HelperOwnership(probe: ["state": "foreign", "home": "/x"]) == .foreign(home: "/x"))
    #expect(HelperOwnership(probe: ["state": "stale"]) == .stale)
    #expect(HelperOwnership(probe: [:]) == .absent)
    // Only "ours" permits enabling TUN.
    #expect(!HelperOwnership.ours.blocksTun)
    #expect(HelperOwnership.foreign(home: "/x").blocksTun)
    #expect(HelperOwnership.stale.blocksTun)
}
