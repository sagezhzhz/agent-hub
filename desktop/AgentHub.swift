import AppKit
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView?
    private var backend: Process?
    private var island: Process?
    private var outputPipe: Pipe?
    private var startupTimer: Timer?
    private var endpoint: URL?
    private var generation = UUID()
    private var quitting = false
    private var reportingFailure = false
    private var islandMenu: NSMenuItem!
    private var logHandle: FileHandle?
    private let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/Agent Hub/desktop.log")

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let id = Bundle.main.bundleIdentifier,
           let other = NSRunningApplication.runningApplications(withBundleIdentifier: id)
            .first(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }) {
            other.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
            NSApp.terminate(nil)
            return
        }
        UserDefaults.standard.register(defaults: ["showIsland": true])
        setupLog()
        setupMenu()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 780),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Agent Hub"
        window.minSize = NSSize(width: 390, height: 420)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.center()
        window.setFrameAutosaveName("AgentHubMainWindow")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        startBackend()
    }

    private func setupLog() {
        do {
            try FileManager.default.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            if let size = try? logURL.resourceValues(forKeys: [.fileSizeKey]).fileSize, size > 1_000_000 {
                let old = logURL.appendingPathExtension("previous")
                try? FileManager.default.removeItem(at: old)
                try FileManager.default.moveItem(at: logURL, to: old)
            }
            if !FileManager.default.fileExists(atPath: logURL.path) {
                FileManager.default.createFile(atPath: logURL.path, contents: nil)
            }
            logHandle = try FileHandle(forWritingTo: logURL)
            logHandle?.seekToEndOfFile()
        } catch { NSLog("Could not open application log: %@", error.localizedDescription) }
    }

    private func log(_ message: String) {
        if let bytes = "\(Date()) \(message)\n".data(using: .utf8) { logHandle?.write(bytes) }
    }

    private func menuItem(_ title: String, _ action: Selector, _ key: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        return item
    }

    private func setupMenu() {
        let main = NSMenu()
        let appMenu = NSMenu(title: "Agent Hub")
        appMenu.addItem(menuItem("关于 Agent Hub", #selector(about)))
        appMenu.addItem(.separator())
        islandMenu = menuItem("显示灵动岛", #selector(toggleIsland))
        islandMenu.state = UserDefaults.standard.bool(forKey: "showIsland") ? .on : .off
        appMenu.addItem(islandMenu)
        appMenu.addItem(menuItem("打开日志", #selector(openLog)))
        appMenu.addItem(.separator())
        appMenu.addItem(menuItem("退出 Agent Hub", #selector(quit), "q"))
        let appItem = NSMenuItem(); appItem.submenu = appMenu; main.addItem(appItem)

        let edit = NSMenu(title: "编辑")
        for (title, action, key) in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"),
                                      ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            edit.addItem(NSMenuItem(title: title, action: Selector(action), keyEquivalent: key))
        }
        let editItem = NSMenuItem(title: "编辑", action: nil, keyEquivalent: ""); editItem.submenu = edit; main.addItem(editItem)

        let view = NSMenu(title: "窗口")
        view.addItem(menuItem("刷新", #selector(reload), "r"))
        view.addItem(menuItem("在浏览器中打开", #selector(openBrowser)))
        view.addItem(NSMenuItem(title: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m"))
        view.addItem(NSMenuItem(title: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"))
        let viewItem = NSMenuItem(title: "窗口", action: nil, keyEquivalent: ""); viewItem.submenu = view; main.addItem(viewItem)
        NSApp.mainMenu = main
    }

    private func showLoading() {
        let content = NSView()
        let spinner = NSProgressIndicator()
        spinner.style = .spinning; spinner.startAnimation(nil)
        let label = NSTextField(labelWithString: "正在打开 Agent Hub…")
        label.textColor = .secondaryLabelColor
        let stack = NSStackView(views: [spinner, label])
        stack.orientation = .vertical; stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([stack.centerXAnchor.constraint(equalTo: content.centerXAnchor),
                                     stack.centerYAnchor.constraint(equalTo: content.centerYAnchor)])
        window.contentView = content
    }

    private func startBackend() {
        stopChildren()
        generation = UUID()
        let token = generation
        endpoint = nil; webView = nil
        showLoading()
        guard let resources = Bundle.main.resourceURL else { showFailure("应用文件不完整，请重新安装。"); return }
        let process = Process()
        process.executableURL = resources.appendingPathComponent("backend/agent-hub-server")
        process.arguments = ["--parent-pid", String(ProcessInfo.processInfo.processIdentifier)]
        var environment = ProcessInfo.processInfo.environment
        // Isolate the bundled runtime from a shell's Python configuration.
        for name in ["PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "AGENT_HUB_DATA_DIR", "AGENT_HUB_PORT"] {
            environment.removeValue(forKey: name)
        }
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = logHandle ?? FileHandle.standardError
        outputPipe = pipe; backend = process
        process.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard let self = self, self.generation == token, !self.quitting else { return }
                self.log("Backend exited: \(process.terminationStatus)")
                self.showFailure("本地服务意外退出。可以重新启动，或打开日志查看详情。")
            }
        }
        do { try process.run() }
        catch { showFailure("无法启动本地服务：\(error.localizedDescription)"); return }
        log("Started backend pid=\(process.processIdentifier)")
        startupTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: false) { [weak self] _ in
            guard let self = self, self.generation == token, self.endpoint == nil else { return }
            self.showFailure("启动时间过长，请重试。")
        }
        // Reading the ready message off the main thread keeps the window responsive.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var buffer = Data()
            while true {
                let chunk = pipe.fileHandleForReading.availableData
                if chunk.isEmpty { break }
                buffer.append(chunk)
                if let newline = buffer.firstIndex(of: 10) {
                    let line = buffer.prefix(upTo: newline)
                    guard let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                          object["app"] as? String == "agent-hub-desktop",
                          let port = object["port"] as? Int, (1...65535).contains(port) else { break }
                    DispatchQueue.main.async {
                        guard let self = self, self.generation == token, !self.quitting else { return }
                        self.showDashboard(port: port)
                    }
                    break
                }
                if buffer.count > 65536 { break }
            }
        }
    }

    private func showDashboard(port: Int) {
        startupTimer?.invalidate(); startupTimer = nil
        let url = URL(string: "http://127.0.0.1:\(port)/?lang=zh&desktop=1")!
        endpoint = url
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        let web = WKWebView(frame: window.contentView?.bounds ?? .zero, configuration: configuration)
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self
        webView = web; window.contentView = web
        web.load(URLRequest(url: url))
        log("Dashboard ready at port=\(port)")
        startIsland()
    }

    private func startIsland() {
        guard UserDefaults.standard.bool(forKey: "showIsland"), island == nil,
              let port = endpoint?.port, let resources = Bundle.main.resourceURL else { return }
        let process = Process()
        process.executableURL = resources.appendingPathComponent("agent-hub-island")
        process.arguments = [String(port), String(ProcessInfo.processInfo.processIdentifier), Bundle.main.bundlePath]
        process.standardOutput = logHandle ?? FileHandle.standardOutput
        process.standardError = logHandle ?? FileHandle.standardError
        do { try process.run(); island = process }
        catch { log("Island unavailable: \(error.localizedDescription)") }
    }

    private func stopChildren() {
        generation = UUID()
        startupTimer?.invalidate(); startupTimer = nil
        for process in [island, backend].compactMap({ $0 }) {
            process.terminationHandler = nil
            if process.isRunning { process.terminate() }
        }
        island = nil; backend = nil; outputPipe = nil
    }

    private func showFailure(_ detail: String) {
        guard !quitting, !reportingFailure else { return }
        reportingFailure = true
        log(detail); stopChildren()
        let alert = NSAlert()
        alert.messageText = "Agent Hub 未能正常启动"
        alert.informativeText = detail
        alert.addButton(withTitle: "重试")
        alert.addButton(withTitle: "打开日志")
        alert.addButton(withTitle: "退出")
        let result = alert.runModal()
        reportingFailure = false
        if result == .alertFirstButtonReturn { startBackend() }
        else { if result == .alertSecondButtonReturn { openLog() }; NSApp.terminate(nil) }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "http", url.host == "127.0.0.1", url.port == endpoint?.port {
            decisionHandler(.allow)
        } else {
            if navigationAction.navigationType == .linkActivated, ["http", "https"].contains(url.scheme ?? "") {
                NSWorkspace.shared.open(url)
            }
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { log("Dashboard page loaded") }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { showFailure("无法打开面板：\(error.localizedDescription)") }
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { webView.reload() }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) { quitting = true; stopChildren(); log("Application quit") }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window?.makeKeyAndOrderFront(nil); return true
    }

    @objc private func about() { NSApp.orderFrontStandardAboutPanel(options: [
        .applicationName: "Agent Hub", .applicationVersion: "1.0.0",
        .credits: NSAttributedString(string: "本地 AI 会话看板 · macOS 桌面版\n基于 sagezhzhz/agent-hub，Apache-2.0。")
    ]) }
    @objc private func quit() { NSApp.terminate(nil) }
    @objc private func reload() { webView?.reload() }
    @objc private func openLog() { NSWorkspace.shared.open(logURL) }
    @objc private func openBrowser() { if let endpoint = endpoint { NSWorkspace.shared.open(endpoint) } }
    @objc private func toggleIsland() {
        let enabled = !UserDefaults.standard.bool(forKey: "showIsland")
        UserDefaults.standard.set(enabled, forKey: "showIsland")
        islandMenu.state = enabled ? .on : .off
        if enabled { startIsland() }
        else { if let island = island, island.isRunning { island.terminate() }; island = nil }
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
