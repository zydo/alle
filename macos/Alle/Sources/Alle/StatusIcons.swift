import AppKit
import Foundation

/// The three menu-bar states alle's tray distinguishes.
enum StatusIconKind: Sendable {
    case stopped
    case running
    case tun
}

/// Loads the monochrome template glyphs for the status item.
///
/// Two runtime contexts resolve the same assets:
/// - the packaged ``Alle.app``: PDFs sit in ``Contents/Resources/`` and are
///   found via ``Bundle.main``;
/// - a raw ``swift run`` dev build: the PDFs are an SPM resource, found via
///   ``Bundle.module``.
///
/// The glyphs are template images (``NSImage.isTemplate``): macOS keeps only
/// the alpha and tints them to match the menu bar in light/dark mode. Callers
/// that get ``nil`` should fall back to a plain-text glyph so the tray still
/// renders when the assets are absent.
@MainActor
enum StatusIcons {
    private static var cache: [StatusIconKind: NSImage] = [:]

    /// Whether we are running from a packaged `.app` rather than an SPM build.
    ///
    /// This gates the `Bundle.module` fallback, which *traps* instead of
    /// returning nil when the generated resource bundle is missing — and
    /// missing is exactly the packaged case: the build copies the PDFs into
    /// `Contents/Resources` and never ships `Alle_Alle.bundle`. Reaching
    /// for it there would turn "assets absent" — the one case this type
    /// documents as returning nil — into a crash on launch.
    private static let isPackagedApp = Bundle.main.bundleURL.pathExtension == "app"

    static func image(for kind: StatusIconKind) -> NSImage? {
        if let cached = cache[kind] {
            return cached
        }
        let name: String
        switch kind {
        case .stopped: name = "status-stopped"
        case .running: name = "status-running"
        case .tun: name = "status-tun"
        }
        // Bundle.main first: in the .app the assets live in Contents/Resources.
        // Bundle.module covers the SPM dev/test build, where it always exists.
        var url = Bundle.main.url(forResource: name, withExtension: "pdf")
        if url == nil, !isPackagedApp {
            url = Bundle.module.url(forResource: name, withExtension: "pdf")
        }
        guard let url, let image = NSImage(contentsOf: url) else {
            return nil
        }
        image.isTemplate = true
        // A PDF-backed NSImage loads at its native (large) page size; without a
        // menu-bar-sized logical size the status item renders it enormous. 20pt
        // lands the ~70%-fill glyph near the ~16pt optical weight of system
        // status items, inside the 22pt bar.
        image.size = NSSize(width: 20, height: 20)
        cache[kind] = image
        return image
    }

    /// Drop cached images (tests re-enter fresh).
    static func reset() {
        cache.removeAll()
    }
}
