import SwiftUI

/// The channel table — the Web UI's `renderChannels` / `chanRow` / `buildIpCell`.
struct ChannelsView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.theme) private var theme
    @State private var renaming: String?
    @State private var draftLabel = ""

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.gap) {
            Panel(
                title: "Channels",
                subtitle: "One proxy port per channel. Probe measures latency and exit IP."
            ) {
                HStack(spacing: 10) {
                    RowButton(title: "Probe all", busy: model.isBusy("test:all")) {
                        Task { await model.test(nil, speed: false) }
                    }
                    RowButton(title: "Speed test all", busy: model.isBusy("test:all")) {
                        Task { await model.test(nil, speed: true) }
                    }
                    Spacer()
                }

                if model.status.channels.isEmpty {
                    EmptyHint(
                        text: "No channels yet. Add a provider with the CLI or import a bundle.")
                } else {
                    VStack(spacing: 0) {
                        header
                        ForEach(model.status.channels) { channel in
                            Divider().overlay(theme.line)
                            row(channel)
                        }
                    }
                }
            }
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Text("CHANNEL").frame(width: 210, alignment: .leading)
            Text("LOCATION").frame(width: 150, alignment: .leading)
            Text("STATE").frame(width: 120, alignment: .leading)
            Text("EXIT IP").frame(width: 130, alignment: .leading)
            Text("LATENCY").frame(width: 70, alignment: .trailing)
            Spacer()
        }
        .font(.system(size: 10, weight: .semibold))
        .tracking(0.5)
        .foregroundStyle(theme.faint)
        .padding(.vertical, 6)
    }

    @ViewBuilder private func row(_ channel: Channel) -> some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 1) {
                if renaming == channel.id {
                    TextField("Label", text: $draftLabel, onCommit: { commitRename(channel) })
                        .textFieldStyle(.roundedBorder)
                        .font(.system(size: 12))
                } else {
                    Text(channel.displayName)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(theme.text)
                        .onTapGesture(count: 2) {
                            draftLabel = channel.label ?? ""
                            renaming = channel.id
                        }
                }
                Text(channel.id)
                    .font(.system(size: 10))
                    .foregroundStyle(theme.faint)
            }
            .frame(width: 210, alignment: .leading)

            Text(channel.location)
                .font(.system(size: 12))
                .foregroundStyle(theme.muted)
                .frame(width: 150, alignment: .leading)

            StatePill(state: channel.state, text: channel.stateLabel)
                .frame(width: 120, alignment: .leading)

            Text(channel.exitIP ?? "—")
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(channel.exitIP == nil ? theme.faint : theme.text)
                .textSelection(.enabled)
                .frame(width: 130, alignment: .leading)

            Text(channel.latencyMs.map { "\(Int($0)) ms" } ?? "—")
                .font(.system(size: 11))
                .tabularNumbers()
                .foregroundStyle(theme.muted)
                .frame(width: 70, alignment: .trailing)

            Spacer(minLength: 8)

            HStack(spacing: 6) {
                RowButton(
                    title: channel.enabled ? "Disable" : "Enable",
                    busy: model.isBusy(channel.id)
                ) { Task { await model.setChannelEnabled(channel, !channel.enabled) } }
                RowButton(title: "Probe", busy: model.isBusy("\(channel.id):probe")) {
                    Task { await model.test(channel, speed: false) }
                }
                RowButton(title: "Speed", busy: model.isBusy("\(channel.id):speed")) {
                    Task { await model.test(channel, speed: true) }
                }
            }
        }
        .padding(.vertical, 8)
        .opacity(channel.enabled ? 1 : 0.55)
    }

    private func commitRename(_ channel: Channel) {
        let label = draftLabel.trimmingCharacters(in: .whitespacesAndNewlines)
        renaming = nil
        guard !label.isEmpty, label != channel.label else { return }
        Task { await model.relabel(channel, to: label) }
    }
}
