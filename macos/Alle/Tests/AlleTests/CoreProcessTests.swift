import Foundation
import Testing

@testable import Alle

@Test func shellQuotingSurvivesEmbeddedQuotes() {
    #expect(shellQuoted("/Applications/Alle.app/x") == "'/Applications/Alle.app/x'")
    // A path containing a single quote must not be able to end the quoted run.
    #expect(shellQuoted("/a/it's/alle") == #"'/a/it'\''s/alle'"#)
}

@Test func appleScriptQuotingEscapesBackslashAndQuote() {
    #expect(appleScriptQuoted(#"say "hi""#) == #""say \"hi\"""#)
    #expect(appleScriptQuoted(#"a\b"#) == #""a\\b""#)
}

@Test func pathEncodingKeepsTheChannelSeparator() {
    // Channel ids are provider/name and that slash is a real path separator the
    // API routes on — only the pieces get escaped.
    #expect("protonvpn/nl 1".urlPathEncoded == "protonvpn/nl%201")
    #expect("a/b/c".urlPathEncoded == "a/b/c")
}

@Test func queryEncodingEscapesEverythingUnsafe() {
    #expect("a b&c".urlQueryEncoded == "a%20b%26c")
}

@Test func bundledExecutableIsNotResolvedFromPATH() {
    // With no app bundle around, there must be no fallback to a PATH `alle` —
    // that would drive a Homebrew/uv install's state directory.
    let core = CoreProcess(executable: nil, resourceURL: nil, environment: [:])
    #expect(!core.isAvailable)
}

@Test func processEnvironmentPinsTheAppIdentity() {
    let resources = URL(fileURLWithPath: "/Applications/Alle.app/Contents/Resources")
    let core = CoreProcess(
        executable: nil, resourceURL: resources, environment: ["PATH": "/usr/bin"])
    let env = core.processEnvironment()
    #expect(env["ALLE_SERVICE_OWNER"] == "macos-app")
    #expect(env["ALLE_SERVICE_PREFIX"] == resources.path)
    #expect(env["ALLE_HOME"]?.hasSuffix("Library/Application Support/Alle") == true)
}

@Test func explicitHomeIsNotOverridden() {
    let core = CoreProcess(
        executable: nil, resourceURL: URL(fileURLWithPath: "/tmp/R"),
        environment: ["ALLE_HOME": "/custom/home"])
    #expect(core.processEnvironment()["ALLE_HOME"] == "/custom/home")
}
