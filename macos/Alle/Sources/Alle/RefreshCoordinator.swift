import Foundation

actor RefreshCoordinator {
    private struct Pending: Sendable {
        var generation: Int
        var run: @Sendable () async -> Void
    }

    private var pending: Pending?
    private var generation = 0
    private var closed = false
    private var running = false

    func submit<T: Sendable>(
        work: @escaping @Sendable () async throws -> T,
        done: @escaping @Sendable @MainActor (Result<T, Error>) -> Void
    ) {
        guard !closed else {
            return
        }
        generation += 1
        let itemGeneration = generation
        pending = Pending(generation: itemGeneration) { [weak self] in
            let result: Result<T, Error>
            do {
                result = .success(try await work())
            } catch {
                result = .failure(error)
            }
            guard await self?.isCurrent(itemGeneration) == true else {
                return
            }
            await done(result)
        }
        if !running {
            running = true
            Task {
                await self.run()
            }
        }
    }

    func finish(
        timeoutSeconds: TimeInterval = 2.0,
        work: @escaping @Sendable () async -> Void
    ) async -> Bool {
        await withTaskGroup(of: Bool.self, returning: Bool.self) { group in
            group.addTask {
                await work()
                return true
            }
            group.addTask {
                let nanoseconds = UInt64(max(0, timeoutSeconds) * 1_000_000_000)
                try? await Task.sleep(nanoseconds: nanoseconds)
                return false
            }
            let completed = await group.next() ?? false
            group.cancelAll()
            return completed
        }
    }

    func close() {
        closed = true
        pending = nil
    }

    private func run() async {
        while true {
            guard let item = takePending() else {
                running = false
                return
            }
            await item.run()
        }
    }

    private func takePending() -> Pending? {
        if closed {
            return nil
        }
        let item = pending
        pending = nil
        return item
    }

    private func isCurrent(_ itemGeneration: Int) -> Bool {
        itemGeneration == generation && !closed
    }
}
