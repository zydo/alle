import SwiftUI

/// Rules, rulesets, geo data, and trace — the Web UI's `renderRoutes`,
/// `rulesetBar`, and `orderedRulesets`.
struct RoutesView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.theme) private var theme
    @State private var traceHost = ""
    @State private var traceResult: String?
    @State private var renaming: String?
    @State private var draftName = ""

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.gap) {
            Panel(title: "Rules", subtitle: "Evaluated top to bottom; first match wins.") {
                if model.routes.rules.isEmpty {
                    EmptyHint(text: "No routing rules. All traffic follows the default channel.")
                } else {
                    ForEach(Array(model.routes.rules.enumerated()), id: \.element.id) { idx, rule in
                        if idx > 0 { Divider().overlay(theme.line) }
                        ruleRow(rule, isFirst: idx == 0, isLast: idx == model.routes.rules.count - 1)
                    }
                }
            }

            Panel(title: "Rule sets", subtitle: "Imported lists, matched as a group.") {
                let sets = model.routes.orderedRulesets
                if sets.isEmpty {
                    EmptyHint(text: "No rule sets imported.")
                } else {
                    ForEach(sets) { set in
                        rulesetRow(set)
                        if set.id != sets.last?.id { Divider().overlay(theme.line) }
                    }
                }
            }

            Panel(title: "Geo data", subtitle: "Offline geosite/geoip categories.") {
                HStack(spacing: 10) {
                    RowButton(title: "Refresh", busy: model.isBusy("geo")) {
                        Task { await model.refreshGeo() }
                    }
                    Text("Downloads the pinned rule-set bundles into local state.")
                        .font(.system(size: 11))
                        .foregroundStyle(theme.faint)
                    Spacer()
                }
            }

            Panel(title: "Trace", subtitle: "Which channel would a destination take?") {
                HStack(spacing: 10) {
                    TextField("example.com", text: $traceHost)
                        .textFieldStyle(.roundedBorder)
                        .frame(maxWidth: 280)
                    RowButton(title: "Trace", busy: model.isBusy("trace")) {
                        Task { traceResult = await model.trace(host: traceHost) }
                    }
                    Spacer()
                }
                if let traceResult {
                    Text(traceResult)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(theme.text)
                        .textSelection(.enabled)
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 6).fill(theme.ink2))
                }
            }
        }
        .task { _ = try? await Task.sleep(nanoseconds: 1) }
    }

    @ViewBuilder private func ruleRow(_ rule: RouteRule, isFirst: Bool, isLast: Bool) -> some View {
        HStack(spacing: 10) {
            Text(rule.kind ?? "rule")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(theme.route)
                .frame(width: 74, alignment: .leading)
            Text(rule.value ?? "—")
                .font(.system(size: 12, design: .monospaced))
                .foregroundStyle(theme.text)
            Spacer(minLength: 8)
            Text(rule.target ?? "—")
                .font(.system(size: 11))
                .foregroundStyle(theme.muted)
            HStack(spacing: 6) {
                RowButton(title: "↑", busy: model.isBusy("route:\(rule.id)")) {
                    Task { await model.moveRoute(rule, direction: "up") }
                }
                .disabled(isFirst)
                RowButton(title: "↓", busy: model.isBusy("route:\(rule.id)")) {
                    Task { await model.moveRoute(rule, direction: "down") }
                }
                .disabled(isLast)
                RowButton(
                    title: "Remove", busy: model.isBusy("route:\(rule.id)"), destructive: true
                ) { Task { await model.removeRoute(rule) } }
            }
        }
        .padding(.vertical, 7)
    }

    @ViewBuilder private func rulesetRow(_ set: RuleSet) -> some View {
        HStack(spacing: 10) {
            if renaming == set.id {
                TextField("Name", text: $draftName, onCommit: { commitRename(set) })
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 200)
            } else {
                Text(set.displayName)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(theme.text)
                    .onTapGesture(count: 2) {
                        draftName = set.name ?? ""
                        renaming = set.id
                    }
            }
            if let count = set.ruleCount {
                Text("\(count) rules")
                    .font(.system(size: 11))
                    .tabularNumbers()
                    .foregroundStyle(theme.faint)
            }
            Spacer(minLength: 8)
            Text(set.target ?? "—")
                .font(.system(size: 11))
                .foregroundStyle(theme.muted)
            HStack(spacing: 6) {
                RowButton(title: "Update", busy: model.isBusy("ruleset:\(set.id)")) {
                    Task { await model.updateRuleSet(set) }
                }
                RowButton(
                    title: "Remove", busy: model.isBusy("ruleset:\(set.id)"), destructive: true
                ) { Task { await model.removeRuleSet(set) } }
            }
        }
        .padding(.vertical, 7)
    }

    private func commitRename(_ set: RuleSet) {
        let name = draftName.trimmingCharacters(in: .whitespacesAndNewlines)
        renaming = nil
        guard !name.isEmpty, name != set.name else { return }
        Task { await model.renameRuleSet(set, to: name) }
    }
}
