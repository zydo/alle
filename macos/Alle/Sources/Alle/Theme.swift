import SwiftUI

/// The Web UI's design tokens, ported verbatim.
///
/// Every value here is a transcription of a `--custom-property` in
/// `src/alle/assets/style.css` — the `:root` block for light, `:root.dark` for
/// dark. Keeping the two surfaces token-for-token identical is what stops the
/// native app and the Web UI drifting into two different-looking products, so
/// prefer editing both together over inventing a value here.
///
/// The CSS applies dark via a `.dark` class on `<html>`, driven from
/// localStorage or the OS preference. The app has no theme toggle: it follows
/// the system appearance, so `Theme` is selected from the SwiftUI environment's
/// `colorScheme` and nothing persists.
struct Theme: Equatable, Sendable {
    // Surfaces, page backdrop outward to the most-raised panel.
    let ink: Color
    let ink2: Color
    let panel: Color
    let panel2: Color
    let panel3: Color

    // Borders.
    let line: Color
    let line2: Color

    // Text, in descending emphasis.
    let text: Color
    let muted: Color
    let faint: Color

    // Accents. `live`/`warn`/`down` are the channel-state vocabulary; `route`
    // marks routing UI; `brand` is the primary action.
    let brand: Color
    let live: Color
    let warn: Color
    let down: Color
    let route: Color

    // Elevation. Light throws a cool teal-tinted shadow (the CSS comment calls
    // this out); dark goes black, because a tinted shadow reads as mud there.
    let shadowSoft: Color
    let shadowPop: Color

    /// `--gap: 22px` — the single spacing rhythm the Web UI lays out on.
    static let gap: CGFloat = 22
    /// `--radius: 9px`.
    static let radius: CGFloat = 9

    static let light = Theme(
        ink: .hex(0xe7_ed_f1),
        ink2: .hex(0xd7_e0_e6),
        panel: .hex(0xfb_fc_fb),
        panel2: .hex(0xee_f3_f2),
        panel3: .hex(0xe4_ec_eb),
        line: .hex(0xce_d9_d8),
        line2: .hex(0x94_a6_a4),
        text: .hex(0x17_20_1f),
        muted: .hex(0x53_64_62),
        faint: .hex(0x81_90_8e),
        brand: .hex(0x3b_82_f6),
        live: .hex(0x00_7f_73),
        warn: .hex(0xa2_62_16),
        down: .hex(0xbd_33_2b),
        route: .hex(0x66_55_df),
        shadowSoft: .hex(0x17_20_1f, alpha: 0.05),
        shadowPop: .hex(0x17_20_1f, alpha: 0.16)
    )

    static let dark = Theme(
        ink: .hex(0x0e_14_16),
        ink2: .hex(0x16_1d_1f),
        panel: .hex(0x1c_24_26),
        panel2: .hex(0x23_2c_2e),
        panel3: .hex(0x2b_34_37),
        line: .hex(0x32_3c_3e),
        line2: .hex(0x4c_5a_5c),
        text: .hex(0xe7_ed_f1),
        muted: .hex(0x9f_b0_ad),
        faint: .hex(0x7a_8c_89),
        brand: .hex(0x0a_84_ff),
        live: .hex(0x2b_bd_9e),
        warn: .hex(0xe0_a0_50),
        down: .hex(0xe5_66_5c),
        route: .hex(0x9a_86_ff),
        shadowSoft: .hex(0x00_00_00, alpha: 0.28),
        shadowPop: .hex(0x00_00_00, alpha: 0.5)
    )

    static func of(_ scheme: ColorScheme) -> Theme {
        scheme == .dark ? .dark : .light
    }

    /// The accent a channel/daemon state renders in, matching the Web UI's
    /// `state-pill` colouring.
    func color(for state: RunState) -> Color {
        switch state {
        case .active: return live
        case .reconnecting, .pending: return warn
        case .failed: return down
        case .disabled: return faint
        case .unknown: return faint
        }
    }
}

/// The state vocabulary the status pill and channel rows share.
///
/// The core's `status_snapshot` emits human-facing labels rather than an enum —
/// `Active`, `Disabled`, `Pending`, `Reconnecting (2)`, `Reconnect failed`, or a
/// probe-derived label from `probe.state_label`. Classify defensively: an
/// unrecognised label lands on `.unknown` and still renders, which is the
/// version-skew contract applied to a field whose vocabulary can grow.
enum RunState: Sendable, Equatable {
    case active
    case reconnecting
    case pending
    case failed
    case disabled
    case unknown

    init(label raw: String?) {
        let value = (raw ?? "").lowercased()
        if value.isEmpty {
            self = .unknown
        } else if value.hasPrefix("active") || value.hasPrefix("healthy") {
            self = .active
        } else if value.hasPrefix("disabled") {
            self = .disabled
        } else if value.hasPrefix("reconnect failed") || value.contains("failed") {
            self = .failed
        } else if value.hasPrefix("reconnecting") {
            self = .reconnecting
        } else if value.hasPrefix("pending") {
            self = .pending
        } else {
            self = .unknown
        }
    }
}

extension Color {
    /// 0xRRGGBB, so the token tables read like the CSS they came from.
    static func hex(_ value: UInt32, alpha: Double = 1) -> Color {
        Color(
            .sRGB,
            red: Double((value >> 16) & 0xff) / 255,
            green: Double((value >> 8) & 0xff) / 255,
            blue: Double(value & 0xff) / 255,
            opacity: alpha
        )
    }
}

/// Passed down so views take the theme from context rather than re-deriving it
/// from `colorScheme` at every level.
private struct ThemeKey: EnvironmentKey {
    static let defaultValue = Theme.light
}

extension EnvironmentValues {
    var theme: Theme {
        get { self[ThemeKey.self] }
        set { self[ThemeKey.self] = newValue }
    }
}

extension View {
    /// Numeric cells align in columns — the CSS uses `tabular-nums` for this.
    func tabularNumbers() -> some View {
        monospacedDigit()
    }
}
