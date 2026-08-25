// swift-tools-version: 6.0
import PackageDescription

// Built with `swift build` under Command Line Tools — there is no full Xcode on
// the build host, so this is an SPM executable target rather than an Xcode
// project. swift-testing's macro plugin and its framework search paths are not
// on the default CLT search path, so the test target names them explicitly.
let developerDir = Context.environment["DEVELOPER_DIR"] ?? "/Library/Developer/CommandLineTools"
let developerFrameworks = "\(developerDir)/Library/Developer/Frameworks"
let testingMacros = "\(developerDir)/usr/lib/swift/host/plugins/testing/libTestingMacros.dylib"
let testingInterop = "\(developerDir)/Library/Developer/usr/lib"

let package = Package(
    name: "Alle",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(name: "Alle", targets: ["Alle"])
    ],
    targets: [
        .executableTarget(
            name: "Alle",
            path: "Sources/Alle",
            resources: [.copy("Resources")]
        ),
        .testTarget(
            name: "AlleTests",
            dependencies: ["Alle"],
            path: "Tests/AlleTests",
            swiftSettings: [
                .unsafeFlags([
                    "-F", developerFrameworks,
                    "-load-plugin-library", testingMacros,
                ])
            ],
            linkerSettings: [
                .unsafeFlags([
                    "-F", developerFrameworks,
                    "-Xlinker", "-rpath",
                    "-Xlinker", developerFrameworks,
                    "-Xlinker", "-rpath",
                    "-Xlinker", testingInterop,
                ])
            ]
        ),
    ]
)
