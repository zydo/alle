import SwiftUI
import Testing

@testable import Alle

/// The theme is a transcription of style.css. These tests pin the values that
/// carry meaning, so an accidental edit here shows up as a failure rather than
/// as a silent divergence from the Web UI.

@Test func lightAndDarkAreDistinct() {
    #expect(Theme.light != Theme.dark)
    #expect(Theme.of(.light) == Theme.light)
    #expect(Theme.of(.dark) == Theme.dark)
}

@Test func geometryMatchesTheCSS() {
    #expect(Theme.gap == 22)
    #expect(Theme.radius == 9)
}

@Test func stateColoursComeFromTheStateTokens() {
    let theme = Theme.light
    #expect(theme.color(for: .active) == theme.live)
    #expect(theme.color(for: .failed) == theme.down)
    #expect(theme.color(for: .reconnecting) == theme.warn)
    #expect(theme.color(for: .pending) == theme.warn)
    #expect(theme.color(for: .disabled) == theme.faint)
    #expect(theme.color(for: .unknown) == theme.faint)
}

@Test func hexParsesChannelsInOrder() {
    #expect(Color.hex(0x00_7f_73) == Color(.sRGB, red: 0, green: 127 / 255, blue: 115 / 255,
                                           opacity: 1))
}
