import SwiftUI

/// Shared building blocks, matching the Web UI's component grammar so the two
/// surfaces stay recognisably one product: a raised `panel` with a hairline
/// border and 9px radius, an eyebrow-cased section header, and the state pill.

/// The `.state-pill` from the masthead: a coloured dot plus a label.
struct StatePill: View {
    @Environment(\.theme) private var theme
    var state: RunState
    var text: String

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(theme.color(for: state))
                .frame(width: 8, height: 8)
            Text(text)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(theme.muted)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(
            Capsule().fill(theme.panel2)
        )
        .overlay(Capsule().strokeBorder(theme.line, lineWidth: 1))
    }
}

/// A raised surface. `--panel` on `--ink`, hairline `--line`, `--radius`.
struct Panel<Content: View>: View {
    @Environment(\.theme) private var theme
    var title: String?
    var subtitle: String?
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let title {
                VStack(alignment: .leading, spacing: 3) {
                    Text(title.uppercased())
                        .font(.system(size: 11, weight: .semibold))
                        .tracking(0.6)
                        .foregroundStyle(theme.faint)
                    if let subtitle {
                        Text(subtitle)
                            .font(.system(size: 12))
                            .foregroundStyle(theme.muted)
                    }
                }
            }
            content
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Theme.radius).fill(theme.panel))
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radius).strokeBorder(theme.line, lineWidth: 1)
        )
        .shadow(color: theme.shadowSoft, radius: 2, y: 1)
    }
}

/// A labelled switch with an explanatory line, used for TUN / kill switch / LAN.
struct ToggleRow: View {
    @Environment(\.theme) private var theme
    var title: String
    var detail: String
    var isOn: Bool
    var busy: Bool
    var disabled: Bool = false
    var action: (Bool) -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(theme.text)
                Text(detail)
                    .font(.system(size: 11))
                    .foregroundStyle(theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 12)
            if busy {
                ProgressView().controlSize(.small)
            }
            Toggle(
                "",
                isOn: Binding(get: { isOn }, set: { action($0) })
            )
            .labelsHidden()
            .toggleStyle(.switch)
            .disabled(busy || disabled)
        }
    }
}

/// Transient feedback. The Web UI floats these bottom-right; same here.
struct ToastView: View {
    @Environment(\.theme) private var theme
    var toast: AppModel.Toast

    var body: some View {
        Text(toast.message)
            .font(.system(size: 12))
            .foregroundStyle(theme.text)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(RoundedRectangle(cornerRadius: Theme.radius).fill(theme.panel2))
            .overlay(
                RoundedRectangle(cornerRadius: Theme.radius)
                    .strokeBorder(toast.kind == .error ? theme.down : theme.line, lineWidth: 1)
            )
            .shadow(color: theme.shadowPop, radius: 10, y: 4)
            .frame(maxWidth: 420, alignment: .leading)
    }
}

/// Shown when a screen has nothing to list.
struct EmptyHint: View {
    @Environment(\.theme) private var theme
    var text: String

    var body: some View {
        Text(text)
            .font(.system(size: 12))
            .foregroundStyle(theme.faint)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 8)
    }
}

/// A compact bordered button matching the Web UI's row actions.
struct RowButton: View {
    @Environment(\.theme) private var theme
    var title: String
    var busy: Bool = false
    var destructive: Bool = false
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                if busy { ProgressView().controlSize(.mini) }
                Text(title).font(.system(size: 11, weight: .medium))
            }
            .padding(.horizontal, 9)
            .padding(.vertical, 4)
        }
        .buttonStyle(.plain)
        .foregroundStyle(destructive ? theme.down : theme.brand)
        .background(RoundedRectangle(cornerRadius: 6).fill(theme.panel2))
        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(theme.line, lineWidth: 1))
        .disabled(busy)
    }
}
