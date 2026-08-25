import AppKit
import Darwin
import SwiftUI

// Single-instance guard: the app can be launched more than once — by the
// SMAppService login item and by hand (or a second `open`). Without this, two
// status items would appear.
//
// flock() on a long-lived descriptor: the kernel drops the lock when the process
// exits, however it exits, so a crash or force-quit can never wedge the next
// launch out. (A filesystem lock such as NSDistributedLock would: it outlives
// the process that took it and has to be broken by hand.) The descriptor is
// deliberately never closed — holding it open is what holds the lock.
let lockPath = (NSTemporaryDirectory() as NSString)
    .appendingPathComponent("io.github.zydo.alle.app.lock")
let lockDescriptor = open(lockPath, O_CREAT | O_RDWR | O_CLOEXEC, 0o600)
// A descriptor we could not open means no guard, not no app: fail open, so a
// locked-down temp dir cannot stop the app from starting at all.
if lockDescriptor >= 0 && flock(lockDescriptor, LOCK_EX | LOCK_NB) != 0 {
    // Another instance owns the lock — quit this duplicate. Ask the live one to
    // show itself first, so a second launch reads as "focus it", not "nothing
    // happened".
    DistributedNotificationCenter.default().postNotificationName(
        AppDelegate.showWindowNotification, object: nil, userInfo: nil, deliverImmediately: true)
    exit(0)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
