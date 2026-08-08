import AppKit
import Foundation
import ServiceManagement

enum LoginItemStatus: Equatable {
    case enabled
    case notRegistered
    case requiresApproval
    case unavailable(String)

    var isChecked: Bool {
        self == .enabled
    }

    var menuTitle: String {
        switch self {
        case .enabled, .notRegistered:
            return "Launch Tray at Login"
        case .requiresApproval:
            return "Launch Tray at Login — requires approval"
        case .unavailable(let message):
            return "Launch Tray at Login — \(message)"
        }
    }
}

struct LoginItem {
    var status: LoginItemStatus {
        switch SMAppService.mainApp.status {
        case .enabled:
            return .enabled
        case .notRegistered:
            return .notRegistered
        case .requiresApproval:
            return .requiresApproval
        case .notFound:
            return .unavailable("app bundle not found")
        @unknown default:
            return .unavailable("unknown status")
        }
    }

    func toggle() throws {
        switch status {
        case .enabled:
            try SMAppService.mainApp.unregister()
        case .notRegistered:
            try SMAppService.mainApp.register()
        case .requiresApproval:
            openLoginItemsSettings()
        case .unavailable:
            break
        }
    }

    func openLoginItemsSettings() {
        let candidates = [
            "x-apple.systempreferences:com.apple.LoginItems-Settings.extension",
            "x-apple.systempreferences:com.apple.preferences.users?LoginItems",
        ]
        for raw in candidates {
            if let url = URL(string: raw), NSWorkspace.shared.open(url) {
                return
            }
        }
    }
}
