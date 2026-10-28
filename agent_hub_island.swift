// Agent Hub island: a Dynamic Island–style strip around the MacBook notch
// (or the top centre of the menu bar) showing conversations that need you.
//
// agent_hub.py compiles and starts this with its port and PID; it reads the
// hub's /api/sessions every few seconds and quits when the hub does.
// Hover to see the list, click a row to jump into that conversation.

import AppKit
import SwiftUI

let args = CommandLine.arguments
let port = args.count > 1 ? args[1] : "47321"
let hubPID = args.count > 2 ? Int32(args[2]) ?? 0 : 0
let base = "http://127.0.0.1:\(port)"
let zh = (Locale.preferredLanguages.first ?? "").hasPrefix("zh")
func L(_ zhText: String, _ en: String) -> String { zh ? zhText : en }

let ear: CGFloat = 6        // the little outward flare where the island meets the screen edge
let wing: CGFloat = 44      // width added on each side of the notch when compact
let rowH: CGFloat = 40
let maxRows = 6
let spring = Animation.spring(response: 0.38, dampingFraction: 0.78)

let waitColor = Color(hex: "#e8a43a"), unseenColor = Color(hex: "#4cc184"), runColor = Color(hex: "#5b9cf0")

struct Session: Decodable, Equatable {
    let app: String
    let id: String
    let title: String
    let status: String
    let preview: String?
    let projectName: String?
    let updatedAt: Double
    var key: String { app + ":" + id }
}

struct AppMeta: Decodable { let name: String; let letter: String; let color: String }
struct Payload: Decodable { let sessions: [Session]; let apps: [String: AppMeta] }

struct Geo: Equatable {
    var notchW: CGFloat = 0
    var notchH: CGFloat = 32
    var hasNotch = false
}

enum Mode { case hidden, compact, expanded }

final class Hub: ObservableObject {
    @Published var waiting: [Session] = []
    @Published var unseen: [Session] = []
    @Published var running: [Session] = []
    @Published var apps: [String: AppMeta] = [:]
    @Published var icons: [String: NSImage] = [:]
    @Published var banner: Session?
    @Published var hovering = false
    @Published var geo = Geo()

    private var known: Set<String>?   // waiting/unseen keys from the last poll; nil until the first one
    private var iconTried: Set<String> = []
    private var misses = 0
    private var bannerTimer: Timer?

    var listed: [Session] { Array((waiting + unseen + running).prefix(maxRows)) }
    var total: Int { waiting.count + unseen.count + running.count }

    var mode: Mode {
        if total == 0 { return .hidden }
        return hovering || banner != nil ? .expanded : .compact
    }

    var size: CGSize {
        let compactW = geo.notchW + 2 * wing + 2 * ear
        switch mode {
        case .hidden: return CGSize(width: geo.notchW + 2 * ear, height: geo.notchH)
        case .compact: return CGSize(width: compactW, height: geo.notchH)
        case .expanded:
            let content = hovering ? CGFloat(listed.count) * (rowH + 2) + 30 + 12 : 56 + 12
            return CGSize(width: max(compactW, 400), height: geo.notchH + content)
        }
    }

    func start() {
        poll()
        Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in self?.poll() }
    }

    func poll() {
        if hubPID > 0 && kill(hubPID, 0) != 0 { NSApp.terminate(nil) }
        let url = URL(string: "\(base)/api/sessions?lang=\(zh ? "zh" : "en")")!
        URLSession.shared.dataTask(with: url) { data, _, _ in
            DispatchQueue.main.async { self.update(data) }
        }.resume()
    }

    private func update(_ data: Data?) {
        guard let data = data, let p = try? JSONDecoder().decode(Payload.self, from: data) else {
            misses += 1
            if misses >= 10 { NSApp.terminate(nil) }  // the hub has been gone for half a minute
            return
        }
        misses = 0
        let recent = p.sessions.sorted { $0.updatedAt > $1.updatedAt }
        let w = recent.filter { $0.status == "waiting" }
        let u = recent.filter { $0.status == "unseen" }
        let r = recent.filter { $0.status == "running" }
        apps = p.apps
        for app in p.apps.keys where !iconTried.contains(app) { loadIcon(app) }

        // Pop the island open for anything that newly needs you.
        let now = Set((w + u).map { $0.key + ":" + $0.status })
        if let known = known, let fresh = (w + u).first(where: { !known.contains($0.key + ":" + $0.status) }) {
            showBanner(fresh)
        }
        known = now
        if let b = banner, !(w + u).contains(where: { $0.key == b.key }) { banner = nil }
        withAnimation(spring) { waiting = w; unseen = u; running = r }
    }

    private func showBanner(_ s: Session) {
        withAnimation(spring) { banner = s }
        bannerTimer?.invalidate()
        bannerTimer = Timer.scheduledTimer(withTimeInterval: 6, repeats: false) { [weak self] _ in
            withAnimation(spring) { self?.banner = nil }
        }
    }

    private func loadIcon(_ app: String) {
        iconTried.insert(app)
        URLSession.shared.dataTask(with: URL(string: "\(base)/icon/\(app).png")!) { data, resp, _ in
            guard (resp as? HTTPURLResponse)?.statusCode == 200, let data = data, let img = NSImage(data: data) else { return }
            DispatchQueue.main.async { self.icons[app] = img }
        }.resume()
    }

    func open(_ s: Session) {
        var req = URLRequest(url: URL(string: "\(base)/api/open")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["app": s.app, "id": s.id])
        URLSession.shared.dataTask(with: req) { _, _, _ in
            DispatchQueue.main.async { self.poll() }
        }.resume()
        withAnimation(spring) { banner = nil }
    }

    func openBoard() {
        NSWorkspace.shared.open(URL(string: base + "/")!)
    }
}

// ---------------------------------------------------------------- views

extension Color {
    init(hex: String) {
        let v = UInt32(hex.dropFirst(), radix: 16) ?? 0
        self.init(red: Double(v >> 16 & 0xff) / 255, green: Double(v >> 8 & 0xff) / 255, blue: Double(v & 0xff) / 255)
    }
}

/// Flat top that flares into the screen edge, rounded bottom corners.
struct IslandShape: Shape {
    var radius: CGFloat
    var animatableData: CGFloat {
        get { radius }
        set { radius = newValue }
    }

    func path(in r: CGRect) -> Path {
        var p = Path()
        p.move(to: CGPoint(x: r.minX, y: r.minY))
        p.addQuadCurve(to: CGPoint(x: r.minX + ear, y: r.minY + ear), control: CGPoint(x: r.minX + ear, y: r.minY))
        p.addLine(to: CGPoint(x: r.minX + ear, y: r.maxY - radius))
        p.addQuadCurve(to: CGPoint(x: r.minX + ear + radius, y: r.maxY), control: CGPoint(x: r.minX + ear, y: r.maxY))
        p.addLine(to: CGPoint(x: r.maxX - ear - radius, y: r.maxY))
        p.addQuadCurve(to: CGPoint(x: r.maxX - ear, y: r.maxY - radius), control: CGPoint(x: r.maxX - ear, y: r.maxY))
        p.addLine(to: CGPoint(x: r.maxX - ear, y: r.minY + ear))
        p.addQuadCurve(to: CGPoint(x: r.maxX, y: r.minY), control: CGPoint(x: r.maxX - ear, y: r.minY))
        p.closeSubpath()
        return p
    }
}

struct AppIcon: View {
    let app: String
    @ObservedObject var hub: Hub
    var size: CGFloat

    var body: some View {
        if let img = hub.icons[app] {
            Image(nsImage: img).resizable().interpolation(.high).frame(width: size, height: size)
        } else {
            let meta = hub.apps[app]
            RoundedRectangle(cornerRadius: size * 0.25)
                .fill(Color(hex: meta?.color ?? "#555555"))
                .overlay(Text(meta?.letter ?? "?").font(.system(size: size * 0.5, weight: .bold)).foregroundColor(.white))
                .frame(width: size, height: size)
        }
    }
}

/// A dot that breathes, for "this one is waiting on you".
struct Pulse: View {
    var color: Color
    var body: some View {
        TimelineView(.animation) { t in
            let phase = (sin(t.date.timeIntervalSinceReferenceDate * 3.2) + 1) / 2
            ZStack {
                Circle().fill(color.opacity(0.35 * (1 - phase))).frame(width: 7 + 7 * phase, height: 7 + 7 * phase)
                Circle().fill(color).frame(width: 7, height: 7)
            }
            .frame(width: 14, height: 14)
        }
    }
}

struct Spinner: View {
    var body: some View {
        TimelineView(.animation) { t in
            Circle()
                .trim(from: 0, to: 0.7)
                .stroke(runColor, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                .frame(width: 11, height: 11)
                .rotationEffect(.degrees(t.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 1) * 360))
        }
        .frame(width: 14, height: 14)
    }
}

func statusText(_ s: String) -> String {
    switch s {
    case "waiting": return L("需要你处理", "Needs you")
    case "unseen": return L("刚完成", "Just finished")
    default: return L("运行中", "Running")
    }
}

func statusColor(_ s: String) -> Color {
    s == "waiting" ? waitColor : s == "unseen" ? unseenColor : runColor
}

struct Row: View {
    let s: Session
    @ObservedObject var hub: Hub
    var big = false
    @State private var hover = false

    var subtitle: String {
        let app = hub.apps[s.app]?.name ?? s.app
        let project = s.projectName ?? ""
        if big, s.status == "waiting", let pv = s.preview, !pv.isEmpty { return pv }
        return project.isEmpty ? app : "\(app) · \(project)"
    }

    var body: some View {
        HStack(spacing: 10) {
            AppIcon(app: s.app, hub: hub, size: big ? 30 : 22)
            VStack(alignment: .leading, spacing: 2) {
                Text(s.title.isEmpty ? L("（无标题）", "(untitled)") : s.title)
                    .font(.system(size: 13, weight: .semibold)).foregroundColor(.white).lineLimit(1)
                Text(subtitle.replacingOccurrences(of: "\n", with: " "))
                    .font(.system(size: 11)).foregroundColor(.white.opacity(0.55)).lineLimit(1)
            }
            Spacer(minLength: 6)
            Text(statusText(s.status)).font(.system(size: 11, weight: .medium)).foregroundColor(statusColor(s.status))
        }
        .padding(.horizontal, 10)
        .frame(height: big ? 56 : rowH)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(hover ? 0.12 : 0)))
        .contentShape(Rectangle())
        .onHover { hover = $0 }
        .onTapGesture { hub.open(s) }
    }
}

struct IslandView: View {
    @ObservedObject var hub: Hub

    var lead: Session? { (hub.waiting + hub.unseen + hub.running).first }

    var leftWing: some View {
        Group {
            if let s = lead { AppIcon(app: s.app, hub: hub, size: 18) }
        }
        .frame(width: 22, alignment: .leading)
    }

    var rightWing: some View {
        HStack(spacing: 3) {
            if !hub.waiting.isEmpty {
                Pulse(color: waitColor)
                Text("\(hub.waiting.count)").foregroundColor(waitColor)
            } else if !hub.unseen.isEmpty {
                Circle().fill(unseenColor).frame(width: 7, height: 7).frame(width: 14)
                Text("\(hub.unseen.count)").foregroundColor(unseenColor)
            } else {
                Spinner()
            }
        }
        .font(.system(size: 13, weight: .semibold).monospacedDigit())
        .frame(width: 30, alignment: .trailing)
    }

    var footer: some View {
        let more = hub.total - hub.listed.count
        let counts = [
            hub.waiting.isEmpty ? nil : "\(hub.waiting.count) \(L("需要你", "need you"))",
            hub.unseen.isEmpty ? nil : "\(hub.unseen.count) \(L("刚完成", "finished"))",
            hub.running.isEmpty ? nil : "\(hub.running.count) \(L("运行中", "running"))",
        ].compactMap { $0 }.joined(separator: " · ")
        return HStack {
            Text(more > 0 ? counts + L("（另有 \(more) 个）", " (+\(more) more)") : counts)
                .font(.system(size: 11)).foregroundColor(.white.opacity(0.45)).lineLimit(1)
            Spacer()
            Text(L("打开 Agent Hub", "Open Agent Hub"))
                .font(.system(size: 11, weight: .medium)).foregroundColor(.white.opacity(0.8))
                .contentShape(Rectangle())
                .onTapGesture { hub.openBoard() }
        }
        .padding(.horizontal, 10)
        .frame(height: 30)
    }

    var body: some View {
        let g = hub.geo, mode = hub.mode, size = hub.size
        VStack(spacing: 0) {
            HStack(spacing: 0) {
                if mode != .hidden { leftWing.transition(.opacity) }
                Spacer(minLength: g.notchW)
                if mode != .hidden { rightWing.transition(.opacity) }
            }
            .padding(.horizontal, ear + 10)
            .frame(height: g.notchH)

            if mode == .expanded {
                VStack(spacing: 2) {
                    if hub.hovering {
                        ForEach(hub.listed, id: \.key) { Row(s: $0, hub: hub) }
                        footer
                    } else if let b = hub.banner {
                        Row(s: b, hub: hub, big: true)
                    }
                }
                .padding(.horizontal, ear + 8)
                .padding(.top, 4)
                .padding(.bottom, 8)
                .transition(.opacity.combined(with: .scale(scale: 0.96, anchor: .top)))
            }
            Spacer(minLength: 0)
        }
        .frame(width: size.width, height: size.height)
        .background(Color.black)
        .clipShape(IslandShape(radius: mode == .expanded ? 20 : 10))
        .opacity(mode == .hidden ? 0 : 1)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .environment(\.colorScheme, .dark)
    }
}

// ---------------------------------------------------------------- window

final class Panel: NSPanel {
    override var canBecomeKey: Bool { false }
    // Borderless windows are normally pushed below the menu bar; the island lives in it.
    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect { frameRect }
}

final class ClickThroughHost<V: View>: NSHostingView<V> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    let hub = Hub()
    var panel: Panel!
    var top: CGFloat = 0
    var centerX: CGFloat = 0

    func applicationDidFinishLaunching(_ note: Notification) {
        panel = Panel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.level = NSWindow.Level(rawValue: NSWindow.Level.mainMenu.rawValue + 3)
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        panel.ignoresMouseEvents = true
        panel.contentView = ClickThroughHost(rootView: IslandView(hub: hub))
        place()
        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            self?.place()
        }
        panel.orderFrontRegardless()
        // The window is larger than the island, so let clicks through except over the island itself.
        Timer.scheduledTimer(withTimeInterval: 1.0 / 30, repeats: true) { [weak self] _ in self?.track() }
        hub.start()
    }

    /// Sit on the notched screen if there is one, otherwise the menu-bar screen.
    func place() {
        guard let screen = NSScreen.screens.first(where: { $0.safeAreaInsets.top > 0 }) ?? NSScreen.screens.first else { return }
        let f = screen.frame
        var geo = Geo(notchW: 0, notchH: max(f.maxY - screen.visibleFrame.maxY, NSStatusBar.system.thickness), hasNotch: false)
        centerX = f.midX
        if screen.safeAreaInsets.top > 0, let l = screen.auxiliaryTopLeftArea, let r = screen.auxiliaryTopRightArea {
            geo = Geo(notchW: f.width - l.width - r.width, notchH: screen.safeAreaInsets.top, hasNotch: true)
            centerX = f.minX + l.width + geo.notchW / 2
        }
        hub.geo = geo
        top = f.maxY
        let w: CGFloat = 520, h = geo.notchH + CGFloat(maxRows) * (rowH + 2) + 60
        panel.setFrame(NSRect(x: centerX - w / 2, y: f.maxY - h, width: w, height: h), display: true)
    }

    func track() {
        let s = hub.size
        let rect = NSRect(x: centerX - s.width / 2, y: top - s.height, width: s.width, height: s.height + 1)
        let inside = hub.mode != .hidden && rect.contains(NSEvent.mouseLocation)
        panel.ignoresMouseEvents = !inside
        if inside != hub.hovering {
            withAnimation(spring) { hub.hovering = inside }
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
