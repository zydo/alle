import SwiftUI

/// The log tail. The only place a real monospace face is used — `--code` in the
/// CSS, reserved for content where a character grid is functional.
struct LogsView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.theme) private var theme
    @State private var lines = 500

    private let choices = [200, 500, 1_000, 5_000]

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.gap) {
            Panel(title: "Logs", subtitle: "The daemon's recent output.") {
                HStack(spacing: 12) {
                    Picker("Lines", selection: $lines) {
                        ForEach(choices, id: \.self) { Text("\($0)").tag($0) }
                    }
                    .frame(width: 150)
                    RowButton(title: "Refresh", busy: model.isBusy("logs")) {
                        Task { await model.refreshLogs(lines: lines) }
                    }
                    Spacer()
                }

                ScrollView([.vertical, .horizontal]) {
                    Text(model.logText.isEmpty ? "No log output yet." : model.logText)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(model.logText.isEmpty ? theme.faint : theme.text)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                }
                .frame(minHeight: 380)
                .background(RoundedRectangle(cornerRadius: 6).fill(theme.ink2))
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(theme.line, lineWidth: 1))
            }
        }
        .task { await model.refreshLogs(lines: lines) }
        .onChange(of: lines) { _ in Task { await model.refreshLogs(lines: lines) } }
    }
}
