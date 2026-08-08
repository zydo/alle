import SwiftUI
import UniformTypeIdentifiers

/// Config import/export/validate and backup — the Web UI's Bundle page.
struct BundleView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.theme) private var theme
    @State private var draft = ""

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.gap) {
            Panel(title: "Backup", subtitle: "Snapshots of the current configuration.") {
                LabeledContent("Status") {
                    Text(model.backup.enabled ? "Enabled" : "Disabled")
                        .foregroundStyle(model.backup.enabled ? theme.live : theme.muted)
                }
                if let last = model.backup.lastRun {
                    LabeledContent("Last run") {
                        Text(last).foregroundStyle(theme.muted)
                    }
                }
                if let path = model.backup.path {
                    LabeledContent("Location") {
                        Text(path)
                            .font(.system(size: 11, design: .monospaced))
                            .textSelection(.enabled)
                            .foregroundStyle(theme.muted)
                    }
                }
                HStack {
                    RowButton(title: "Back up now", busy: model.isBusy("backup")) {
                        Task { await model.backupNow() }
                    }
                    Spacer()
                }
            }

            Panel(
                title: "Configuration",
                subtitle: "Export the live config, or validate and import one."
            ) {
                HStack(spacing: 10) {
                    RowButton(title: "Export", busy: model.isBusy("export")) {
                        Task {
                            if let text = await model.exportConfig() { draft = text }
                        }
                    }
                    RowButton(title: "Save to file…") { save() }
                    RowButton(title: "Open file…") { load() }
                    Spacer()
                }

                TextEditor(text: $draft)
                    .font(.system(size: 11, design: .monospaced))
                    .frame(minHeight: 300)
                    .padding(6)
                    .background(RoundedRectangle(cornerRadius: 6).fill(theme.ink2))
                    .overlay(
                        RoundedRectangle(cornerRadius: 6).strokeBorder(theme.line, lineWidth: 1))

                HStack(spacing: 10) {
                    RowButton(title: "Validate", busy: model.isBusy("validate")) {
                        Task { await model.validate(draft) }
                    }
                    // Import replaces live configuration, so it is deliberately
                    // the last action and never the default one.
                    RowButton(title: "Import", busy: model.isBusy("import"), destructive: true) {
                        Task { await model.importConfig(draft) }
                    }
                    Spacer()
                }
            }
        }
        .task { await model.refreshBackup() }
    }

    private func save() {
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.json]
        panel.nameFieldStringValue = "alle-config.json"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        try? draft.write(to: url, atomically: true, encoding: .utf8)
    }

    private func load() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.json]
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url,
            let text = try? String(contentsOf: url, encoding: .utf8)
        else { return }
        draft = text
    }
}
