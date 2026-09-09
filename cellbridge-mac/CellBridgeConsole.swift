// CellBridge Console — macOS 原生前端（AppKit）
// 数据源：~/.cellbridge/data/cellbridge.sqlite、~/.cellbridge/run/{config.yaml,logs/*}、进程状态
// 编译：swiftc -O CellBridgeConsole.swift -o CellBridgeConsole

import AppKit
import SQLite3

// MARK: - 常量

let kDBPath = NSHomeDirectory() + "/.cellbridge/data/cellbridge.sqlite"
let kRunDir = NSHomeDirectory() + "/.cellbridge/run"
let kLogDir = kRunDir + "/logs"
let kConfigPath = kRunDir + "/config.yaml"
let kGatewayBin = NSHomeDirectory() + "/WorkBuddy/2026-09-04-18-44-50/mac-4g-modem/cellbridge-mac/cellbridge-gateway"
let kAudioBin = NSHomeDirectory() + "/WorkBuddy/2026-09-04-18-44-50/mac-4g-modem/cellbridge-mac/voice-audio-bridge"
let kAdbPath = NSHomeDirectory() + "/Applications/platform-tools/adb"
let kRefreshInterval: TimeInterval = 2.0

// MARK: - 数据采集层

final class DataStore {
    static let shared = DataStore()

    // 进程检测
    func pgrep(_ pattern: String) -> [Int32] {
        let out = runShell("pgrep -f \(pattern) 2>/dev/null")
        return out.split(separator: "\n").compactMap { Int32($0.trimmingCharacters(in: .whitespaces)) }
    }

    var gatewayRunning: Bool { !pgrep("cellbridge-gateway -config").isEmpty }
    var audioBridgeRunning: Bool { !pgrep("voice-audio-bridge --fifo-rx").isEmpty }
    var ptyBridgeRunning: Bool { !pgrep("at_pty_bridge.py").isEmpty }

    func sipListening() -> Bool {
        let out = runShell("lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -c ':5060'")
        return (Int(out.trimmingCharacters(in: .whitespacesAndNewlines)) ?? 0) > 0
    }

    // 配置解析
    func loadConfig() -> [(String, String)] {
        guard let s = try? String(contentsOfFile: kConfigPath, encoding: .utf8) else { return [] }
        var result: [(String, String)] = []
        for line in s.split(separator: "\n") {
            let l = line.trimmingCharacters(in: .whitespaces)
            if l.isEmpty || l.hasPrefix("#") { continue }
            let parts = l.split(separator: ":", maxSplits: 1)
            if parts.count == 2 {
                result.append((parts[0].trimmingCharacters(in: .whitespaces),
                               parts[1].trimmingCharacters(in: .whitespaces)))
            }
        }
        return result
    }

    // 音频统计（audio-bridge.log 最后一条 stats 行）
    struct AudioStats {
        var rxRate = 0        // cellular->fifo fr/s
        var txRate = 0        // fifo->cellular fr/s
        var dropped = 0
        var renderFailures = 0
        var raw = ""
        var timestamp = Date.distantPast
    }

    func audioStats() -> AudioStats {
        var st = AudioStats()
        guard let s = try? String(contentsOfFile: kLogDir + "/audio-bridge.log", encoding: .utf8) else { return st }
        let lines = s.split(separator: "\n")
        // 最近一条 [stats] 行
        for line in lines.reversed() {
            let l = String(line)
            if l.contains("[stats]"), l.contains("cellular->fifo=") {
                st.raw = l
                // cellular->fifo=39936 fr (7987 fr/s) fifo->cellular=39936 fr (7987 fr/s) dropped=80896 B
                let nums = l.components(separatedBy: CharacterSet.decimalDigits.inverted).filter { !$0.isEmpty }
                if nums.count >= 6 {
                    st.rxRate = Int(nums[2]) ?? 0
                    st.txRate = Int(nums[5]) ?? 0
                    if nums.count >= 7 { st.dropped = Int(nums[6]) ?? 0 }
                }
                break
            }
        }
        // 渲染失败只统计最近 80 行（旧的失败不代表当前）
        st.renderFailures = lines.suffix(80).filter { $0.contains("AudioUnitRender 失败") }.count
        return st
    }

    // mixer 路由状态
    func mixerRoutes() -> (csRx: String?, csTx: String?) {
        guard FileManager.default.fileExists(atPath: kAdbPath) else { return (nil, nil) }
        let out = runShell("\(kAdbPath) shell \"/data/mini_tinymix get 'AFE_PCM_RX_Voice Mixer CSVoice'; /data/mini_tinymix get 'Voice_Tx Mixer AFE_PCM_TX_Voice'\" 2>/dev/null", timeout: 6)
        let lines = out.split(separator: "\n").map { String($0).trimmingCharacters(in: .whitespaces) }
        func lastVal(_ lines: [String]) -> String? {
            guard let l = lines.last else { return nil }
            return l.components(separatedBy: .whitespaces).last
        }
        let half = lines.count / 2
        let csRx = half > 0 ? lastVal(Array(lines[0..<max(half,1)])) : nil
        let csTx = half > 0 ? lastVal(Array(lines[half...])) : nil
        return (csRx, csTx)
    }

    // 通话记录
    struct CallRow {
        var id: String
        var direction: String
        var peer: String
        var state: String
        var startedAt: Int
        var endedAt: Int?
        var endReason: String
    }

    func calls(limit: Int = 100) -> [CallRow] {
        var rows: [CallRow] = []
        guard let db = openDB() else { return rows }
        defer { sqlite3_close(db) }
        let sql = "SELECT id, direction, peer, state, started_at, ended_at, end_reason FROM calls ORDER BY started_at DESC LIMIT \(limit)"
        var stmt: OpaquePointer?
        if sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK {
            while sqlite3_step(stmt) == SQLITE_ROW {
                let ended = sqlite3_column_type(stmt, 5) == SQLITE_NULL ? nil : Int(sqlite3_column_int64(stmt, 5))
                rows.append(CallRow(
                    id: String(cString: sqlite3_column_text(stmt, 0)),
                    direction: String(cString: sqlite3_column_text(stmt, 1)),
                    peer: sqlite3_column_type(stmt, 2) == SQLITE_NULL ? "—" : String(cString: sqlite3_column_text(stmt, 2)),
                    state: String(cString: sqlite3_column_text(stmt, 3)),
                    startedAt: Int(sqlite3_column_int64(stmt, 4)),
                    endedAt: ended,
                    endReason: sqlite3_column_type(stmt, 6) == SQLITE_NULL ? "" : String(cString: sqlite3_column_text(stmt, 6))))
            }
        }
        sqlite3_finalize(stmt)
        return rows
    }

    // 短信记录
    struct MsgRow {
        var direction: String
        var peer: String
        var body: String
        var status: String
        var createdAt: Int
    }

    func messages(limit: Int = 200) -> [MsgRow] {
        var rows: [MsgRow] = []
        guard let db = openDB() else { return rows }
        defer { sqlite3_close(db) }
        let sql = "SELECT direction, peer, body, status, created_at FROM messages ORDER BY created_at DESC LIMIT \(limit)"
        var stmt: OpaquePointer?
        if sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK {
            while sqlite3_step(stmt) == SQLITE_ROW {
                rows.append(MsgRow(
                    direction: String(cString: sqlite3_column_text(stmt, 0)),
                    peer: String(cString: sqlite3_column_text(stmt, 1)),
                    body: String(cString: sqlite3_column_text(stmt, 2)),
                    status: String(cString: sqlite3_column_text(stmt, 3)),
                    createdAt: Int(sqlite3_column_int64(stmt, 4))))
            }
        }
        sqlite3_finalize(stmt)
        return rows
    }

    func messageCount() -> Int {
        guard let db = openDB() else { return 0 }
        defer { sqlite3_close(db) }
        var stmt: OpaquePointer?
        var n = 0
        if sqlite3_prepare_v2(db, "SELECT COUNT(*) FROM messages", -1, &stmt, nil) == SQLITE_OK {
            if sqlite3_step(stmt) == SQLITE_ROW { n = Int(sqlite3_column_int64(stmt, 0)) }
        }
        sqlite3_finalize(stmt)
        return n
    }

    func callCount() -> Int {
        guard let db = openDB() else { return 0 }
        defer { sqlite3_close(db) }
        var stmt: OpaquePointer?
        var n = 0
        if sqlite3_prepare_v2(db, "SELECT COUNT(*) FROM calls", -1, &stmt, nil) == SQLITE_OK {
            if sqlite3_step(stmt) == SQLITE_ROW { n = Int(sqlite3_column_int64(stmt, 0)) }
        }
        sqlite3_finalize(stmt)
        return n
    }

    private func openDB() -> OpaquePointer? {
        var db: OpaquePointer?
        guard sqlite3_open_v2(kDBPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else { return nil }
        return db
    }

    // 日志尾部
    func tailLog(_ name: String, maxBytes: Int = 64_000) -> String {
        let path = kLogDir + "/" + name
        guard let fh = FileHandle(forReadingAtPath: path) else { return "（日志不存在：\(path)）" }
        defer { try? fh.close() }
        let size = (try? fh.seekToEnd()) ?? 0
        let start = max(0, Int(size) - maxBytes)
        try? fh.seek(toOffset: UInt64(start))
        let data = fh.readDataToEndOfFile()
        return String(data: data, encoding: .utf8) ?? "（无法解码）"
    }

    // SIP 注册状态（gateway.log 最近 register 事件）
    func sipRegisterInfo() -> (registered: Bool, detail: String) {
        let log = tailLog("gateway.log", maxBytes: 32_000)
        var registered = false
        var detail = "未见注册记录"
        for line in log.split(separator: "\n").reversed() {
            let l = String(line)
            if l.contains("sip register") {
                let expired = l.contains("expires=0")
                let ipPart = l.components(separatedBy: "contact=").last ?? ""
                if !expired {
                    registered = true
                    detail = ipPart
                } else if !registered {
                    detail = "最近一次为注销"
                }
                break
            }
        }
        return (registered, detail)
    }

    @discardableResult
    func runShell(_ cmd: String, timeout: Int = 8) -> String {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/sh")
        task.arguments = ["-c", cmd]
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = Pipe()
        do { try task.run() } catch { return "" }
        let deadline = Date().addingTimeInterval(TimeInterval(timeout))
        while task.isRunning && Date() < deadline { usleep(50_000) }
        if task.isRunning { task.terminate() }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8) ?? ""
    }
}

// MARK: - 工具

func fmtTime(_ ts: Int?) -> String {
    guard let ts = ts, ts > 0 else { return "—" }
    let df = DateFormatter()
    df.dateFormat = "MM-dd HH:mm:ss"
    return df.string(from: Date(timeIntervalSince1970: TimeInterval(ts)))
}

func fmtDuration(_ s: Int?) -> String {
    guard let s = s, s > 0 else { return "—" }
    if s < 60 { return "\(s) 秒" }
    return "\(s / 60) 分 \(s % 60) 秒"
}

// MARK: - 状态卡片视图

final class StatusCard: NSView {
    private let titleLabel = NSTextField(labelWithString: "")
    private let valueLabel = NSTextField(labelWithString: "")
    private let detailLabel = NSTextField(wrappingLabelWithString: "")
    private let dot = NSView(frame: NSRect(x: 0, y: 0, width: 9, height: 9))

    init(title: String) {
        super.init(frame: .zero)
        wantsLayer = true
        layer?.cornerRadius = 10
        titleLabel.stringValue = title
        titleLabel.font = NSFont.systemFont(ofSize: 12, weight: .medium)
        titleLabel.textColor = .secondaryLabelColor
        valueLabel.font = NSFont.systemFont(ofSize: 17, weight: .semibold)
        valueLabel.textColor = .labelColor
        valueLabel.lineBreakMode = .byTruncatingMiddle
        detailLabel.font = NSFont.systemFont(ofSize: 11)
        detailLabel.textColor = .tertiaryLabelColor
        dot.wantsLayer = true
        dot.layer?.cornerRadius = 4.5
        for v in [titleLabel, valueLabel, detailLabel, dot] { v.translatesAutoresizingMaskIntoConstraints = false; addSubview(v) }
        NSLayoutConstraint.activate([
            dot.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            dot.centerYAnchor.constraint(equalTo: titleLabel.centerYAnchor),
            dot.widthAnchor.constraint(equalToConstant: 9),
            dot.heightAnchor.constraint(equalToConstant: 9),
            titleLabel.leadingAnchor.constraint(equalTo: dot.trailingAnchor, constant: 7),
            titleLabel.topAnchor.constraint(equalTo: topAnchor, constant: 12),
            valueLabel.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            valueLabel.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 6),
            valueLabel.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -14),
            detailLabel.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            detailLabel.topAnchor.constraint(equalTo: valueLabel.bottomAnchor, constant: 4),
            detailLabel.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -14),
            detailLabel.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -12),
        ])
    }
    required init?(coder: NSCoder) { fatalError() }

    override func viewDidChangeEffectiveAppearance() { applyTheme() }
    override func layout() { super.layout(); applyTheme() }

    func applyTheme() {
        layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor
        layer?.borderWidth = 1
        layer?.borderColor = NSColor.separatorColor.cgColor
    }

    func set(value: String, detail: String, ok: Bool?) {
        valueLabel.stringValue = value
        detailLabel.stringValue = detail
        switch ok {
        case .some(true): dot.layer?.backgroundColor = NSColor.systemGreen.cgColor
        case .some(false): dot.layer?.backgroundColor = NSColor.systemRed.cgColor
        case .none: dot.layer?.backgroundColor = NSColor.systemOrange.cgColor
        }
    }
}

// MARK: - 表格封装

final class SimpleTable: NSObject, NSTableViewDataSource, NSTableViewDelegate {
    let tableView: NSTableView
    let scroll: NSScrollView
    private var rowsData: [[String]] = []
    private var headers: [String]

    init(headers: [String], widths: [CGFloat]) {
        self.headers = headers
        tableView = NSTableView()
        tableView.usesAlternatingRowBackgroundColors = true
        tableView.headerView = NSTableHeaderView()
        for (i, h) in headers.enumerated() {
            let col = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("c\(i)"))
            col.title = h
            col.width = widths[i]
            tableView.addTableColumn(col)
        }
        scroll = NSScrollView()
        scroll.documentView = tableView
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        super.init()
        tableView.dataSource = self
        tableView.delegate = self
    }

    func update(_ rows: [[String]]) {
        rowsData = rows
        tableView.reloadData()
    }

    func numberOfRows(in _: NSTableView) -> Int { rowsData.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let colIndex = tableColumn.flatMap { tableView.tableColumns.firstIndex(of: $0) } ?? 0
        let text = cellText(row: row, col: colIndex)
        let cell = tableView.makeView(withIdentifier: NSUserInterfaceItemIdentifier("cell"), owner: nil) as? NSTextField
            ?? {
                let t = NSTextField(labelWithString: "")
                t.identifier = NSUserInterfaceItemIdentifier("cell")
                t.lineBreakMode = .byTruncatingTail
                t.font = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .regular)
                return t
            }()
        cell.stringValue = text
        return cell
    }

    func cellText(row: Int, col: Int) -> String {
        guard row < rowsData.count, col < rowsData[row].count else { return "" }
        return rowsData[row][col]
    }
}

// MARK: - 各页面

protocol Page: AnyObject {
    var view: NSView { get }
    func refresh()
}

// 概览页
final class OverviewPage: NSObject, Page {
    let stack = NSStackView()
    private let cards: [String: StatusCard]
    private let eventView: NSTextView
    private let eventScroll: NSScrollView

    var view: NSView {
        let wrapper = NSView()
        wrapper.translatesAutoresizingMaskIntoConstraints = false
        stack.translatesAutoresizingMaskIntoConstraints = false
        wrapper.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: wrapper.topAnchor, constant: 20),
            stack.leadingAnchor.constraint(equalTo: wrapper.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: wrapper.trailingAnchor, constant: -24),
        ])
        return wrapper
    }

    override init() {
        stack.orientation = .vertical
        stack.spacing = 12
        stack.alignment = .leading

        func make(_ title: String) -> StatusCard { let c = StatusCard(title: title); c.translatesAutoresizingMaskIntoConstraints = false; c.heightAnchor.constraint(equalToConstant: 92).isActive = true; c.widthAnchor.constraint(equalToConstant: 320).isActive = true; return c }

        let gw = make("SIP 网关")
        let pty = make("AT PTY 桥")
        let audio = make("音频桥（蜂窝 ↔ Mac）")
        let route = make("模块语音路由（CS → AFE_PCM）")
        let db = make("数据存储")
        let push = make("推送（APNs）")
        cards = ["gw": gw, "pty": pty, "audio": audio, "route": route, "db": db, "push": push]

        let row1 = NSStackView(views: [gw, pty])
        let row2 = NSStackView(views: [audio, route])
        let row3 = NSStackView(views: [db, push])
        [row1, row2, row3].forEach { $0.spacing = 12; $0.alignment = .top }

        let eventTitle = NSTextField(labelWithString: "最近事件（gateway.log）")
        eventTitle.font = NSFont.systemFont(ofSize: 13, weight: .semibold)

        eventView = NSTextView()
        eventView.isEditable = false
        eventView.font = NSFont.monospacedSystemFont(ofSize: 10.5, weight: .regular)
        eventView.autoresizingMask = [.width]
        eventView.isVerticallyResizable = true
        eventView.textContainer?.widthTracksTextView = true
        eventScroll = NSScrollView()
        eventScroll.documentView = eventView
        eventScroll.hasVerticalScroller = true
        eventScroll.borderType = .bezelBorder

        super.init()
        [row1, row2, row3, eventTitle, eventScroll].forEach { stack.addArrangedSubview($0) }
        eventScroll.translatesAutoresizingMaskIntoConstraints = false
        eventScroll.widthAnchor.constraint(equalToConstant: 652).isActive = true
        eventScroll.heightAnchor.constraint(equalToConstant: 240).isActive = true
    }

    func refresh() {
        let ds = DataStore.shared
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let gwOn = ds.gatewayRunning
            let ptyOn = ds.ptyBridgeRunning
            let audioOn = ds.audioBridgeRunning
            let aStats = ds.audioStats()
            let mixer = ds.mixerRoutes()
            let reg = ds.sipRegisterInfo()
            let callN = ds.callCount()
            let msgN = ds.messageCount()
            let log = ds.tailLog("gateway.log", maxBytes: 4000)
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.cards["gw"]?.set(
                    value: gwOn ? "运行中 · SIP :5060" : "未运行",
                    detail: reg.registered ? "YakPhone 已注册 \(reg.detail)" : "客户端未注册",
                    ok: gwOn ? (reg.registered ? true : nil) : false)
                self.cards["pty"]?.set(
                    value: ptyOn ? "运行中" : "未运行",
                    detail: ptyOn ? "USB AT ↔ /dev/ttys* 已桥接" : "USB AT 口未被桥接",
                    ok: ptyOn)
                self.cards["audio"]?.set(
                    value: audioOn ? "下行 \(aStats.rxRate) fr/s · 上行 \(aStats.txRate) fr/s" : "未运行",
                    detail: audioOn ? (aStats.renderFailures > 0 ? "⚠️ 最近有 \(aStats.renderFailures) 次渲染失败" : "s16le 8kHz mono · 丢弃 \(aStats.dropped) B") : "——",
                    ok: audioOn ? (aStats.rxRate > 0 && aStats.txRate > 0) : false)
                let rx = mixer.csRx ?? "未知"
                let tx = mixer.csTx ?? "未知"
                let routeOK = (rx == "1") && (tx == "1")
                self.cards["route"]?.set(
                    value: routeOK ? "已路由（CS ↔ AFE_PCM）" : (mixer.csRx == nil ? "状态未知" : "未路由"),
                    detail: "CSVoice→AFE_PCM_RX = \(rx) · AFE_PCM_TX→Voice = \(tx)",
                    ok: mixer.csRx == nil ? nil : routeOK)
                self.cards["db"]?.set(
                    value: "通话 \(callN) · 短信 \(msgN)",
                    detail: kDBPath,
                    ok: callN > 0 || msgN > 0 ? true : nil)
                self.cards["push"]?.set(
                    value: "未配置",
                    detail: "APNs provider 未配置（本地使用无需推送）",
                    ok: nil)
                self.eventView.string = log
                self.eventView.scrollToEndOfDocument(nil)
            }
        }
    }
}

// 通话页
final class CallsPage: NSObject, Page {
    let table: SimpleTable
    let countLabel = NSTextField(labelWithString: "")
    var view: NSView {
        let v = NSView()
        countLabel.font = NSFont.systemFont(ofSize: 11)
        countLabel.textColor = .secondaryLabelColor
        countLabel.translatesAutoresizingMaskIntoConstraints = false
        table.scroll.translatesAutoresizingMaskIntoConstraints = false
        v.addSubview(countLabel)
        v.addSubview(table.scroll)
        NSLayoutConstraint.activate([
            countLabel.topAnchor.constraint(equalTo: v.topAnchor, constant: 14),
            countLabel.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 24),
            table.scroll.topAnchor.constraint(equalTo: countLabel.bottomAnchor, constant: 8),
            table.scroll.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 24),
            table.scroll.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -24),
            table.scroll.bottomAnchor.constraint(equalTo: v.bottomAnchor, constant: -16),
        ])
        return v
    }
    override init() {
        table = SimpleTable(headers: ["方向", "对方号码", "状态", "开始时间", "时长", "结束原因"], widths: [56, 130, 90, 150, 90, 160])
    }
    func refresh() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let rows = DataStore.shared.calls().map { c -> [String] in
                let dur = (c.endedAt ?? 0) - c.startedAt
                return [c.direction == "outbound" ? "↗ 拨出" : "↙ 来电",
                        c.peer, c.state, fmtTime(c.startedAt), fmtDuration(dur), c.endReason.isEmpty ? "—" : c.endReason]
            }
            DispatchQueue.main.async {
                self?.table.update(rows)
                self?.countLabel.stringValue = "共 \(rows.count) 条通话记录（最新在前）"
            }
        }
    }
}

// 短信页
final class MessagesPage: NSObject, Page {
    let table: SimpleTable
    let countLabel = NSTextField(labelWithString: "")
    var view: NSView {
        let v = NSView()
        countLabel.font = NSFont.systemFont(ofSize: 11)
        countLabel.textColor = .secondaryLabelColor
        countLabel.translatesAutoresizingMaskIntoConstraints = false
        table.scroll.translatesAutoresizingMaskIntoConstraints = false
        v.addSubview(countLabel)
        v.addSubview(table.scroll)
        NSLayoutConstraint.activate([
            countLabel.topAnchor.constraint(equalTo: v.topAnchor, constant: 14),
            countLabel.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 24),
            table.scroll.topAnchor.constraint(equalTo: countLabel.bottomAnchor, constant: 8),
            table.scroll.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 24),
            table.scroll.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -24),
            table.scroll.bottomAnchor.constraint(equalTo: v.bottomAnchor, constant: -16),
        ])
        return v
    }
    override init() {
        table = SimpleTable(headers: ["方向", "号码", "状态", "时间", "内容"], widths: [56, 120, 90, 150, 420])
    }
    func refresh() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let rows = DataStore.shared.messages().map { m -> [String] in
                [m.direction == "outbound" ? "↗ 发出" : "↙ 收到",
                 m.peer, m.status, fmtTime(m.createdAt),
                 m.body.replacingOccurrences(of: "\n", with: " ")]
            }
            DispatchQueue.main.async {
                self?.table.update(rows)
                self?.countLabel.stringValue = "共 \(rows.count) 条短信记录（最新在前，dry-run=false 即真实收发）"
            }
        }
    }
}

// 音频页
final class AudioPage: NSObject, Page {
    private let bigLabel = NSTextField(labelWithString: "")
    private let detail = NSTextField(wrappingLabelWithString: "")
    private let fifoLabel = NSTextField(wrappingLabelWithString: "")
    private var histView: NSTextView!
    private var histScroll: NSScrollView!

    var view: NSView {
        let v = NSView()
        bigLabel.font = NSFont.monospacedDigitSystemFont(ofSize: 28, weight: .semibold)
        bigLabel.textColor = .labelColor
        detail.font = NSFont.systemFont(ofSize: 12)
        detail.textColor = .secondaryLabelColor
        fifoLabel.font = NSFont.monospacedSystemFont(ofSize: 10.5, weight: .regular)
        fifoLabel.textColor = .tertiaryLabelColor
        histView = NSTextView()
        histView.isEditable = false
        histView.font = NSFont.monospacedSystemFont(ofSize: 10.5, weight: .regular)
        histView.autoresizingMask = [.width]
        histView.isVerticallyResizable = true
        histView.textContainer?.widthTracksTextView = true
        histScroll = NSScrollView()
        histScroll.documentView = histView
        histScroll.hasVerticalScroller = true
        histScroll.borderType = .bezelBorder

        let t = NSTextField(labelWithString: "音频桥实时链路")
        t.font = NSFont.systemFont(ofSize: 13, weight: .semibold)

        let title = NSTextField(labelWithString: "audio-bridge.log（最近统计）")
        title.font = NSFont.systemFont(ofSize: 13, weight: .semibold)

        let subs: [NSView] = [t, bigLabel, detail, fifoLabel, title, histScroll]
        for sub in subs {
            sub.translatesAutoresizingMaskIntoConstraints = false
            v.addSubview(sub)
        }
        NSLayoutConstraint.activate([
            t.topAnchor.constraint(equalTo: v.topAnchor, constant: 18),
            t.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 24),
            bigLabel.topAnchor.constraint(equalTo: t.bottomAnchor, constant: 10),
            bigLabel.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 24),
            detail.topAnchor.constraint(equalTo: bigLabel.bottomAnchor, constant: 6),
            detail.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 24),
            detail.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -24),
            fifoLabel.topAnchor.constraint(equalTo: detail.bottomAnchor, constant: 8),
            fifoLabel.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 24),
            fifoLabel.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -24),
            title.topAnchor.constraint(equalTo: fifoLabel.bottomAnchor, constant: 18),
            title.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 24),
            histScroll.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 8),
            histScroll.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 24),
            histScroll.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -24),
            histScroll.bottomAnchor.constraint(equalTo: v.bottomAnchor, constant: -16),
        ])
        return v
    }

    private var first = true
    func refresh() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let ds = DataStore.shared
            let on = ds.audioBridgeRunning
            let st = ds.audioStats()
            let log = ds.tailLog("audio-bridge.log", maxBytes: 5000)
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.bigLabel.stringValue = on
                    ? "↓ \(st.rxRate) fr/s   ↑ \(st.txRate) fr/s"
                    : "未运行"
                self.bigLabel.textColor = on ? .labelColor : .secondaryLabelColor
                let health = on ? (st.rxRate > 0 ? "正常（8kHz s16le 单声道，双向 8000 帧/秒为满速）" : "⚠️ 蜂窝采集方向无数据") : "——"
                self.detail.stringValue = "状态：\(health)" + (st.renderFailures > 0 ? " · 最近 \(st.renderFailures) 次渲染失败" : "")
                self.fifoLabel.stringValue = "rx FIFO（对方声音→Mac）：\(kRunDir)/cellular-rx.fifo\ntx FIFO（Mac→对方）：\(kRunDir)/cellular-tx.fifo\ndropped：\(st.dropped) B（网关空闲期正常增长，通话中应停止）"
                self.histView.string = log
                self.histView.scrollToEndOfDocument(nil)
                self.first = false
            }
        }
    }
}

// 日志页
final class LogsPage: NSObject, Page {
    private let popup = NSPopUpButton()
    private var textView: NSTextView!
    private var scroll: NSScrollView!
    private let autoCheck = NSButton(checkboxWithTitle: "自动刷新并跟随末尾", target: nil, action: nil)

    static let logFiles = ["gateway.log", "audio-bridge.log", "at-pty.log", "launcher.log"]

    var view: NSView {
        let v = NSView()
        popup.addItems(withTitles: Self.logFiles)
        popup.font = NSFont.systemFont(ofSize: 12)
        popup.target = self
        popup.action = #selector(logChanged)
        autoCheck.font = NSFont.systemFont(ofSize: 12)
        autoCheck.state = .on

        textView = NSTextView()
        textView.isEditable = false
        textView.font = NSFont.monospacedSystemFont(ofSize: 10.5, weight: .regular)
        textView.autoresizingMask = [.width]
        textView.isVerticallyResizable = true
        textView.textContainer?.widthTracksTextView = true
        scroll = NSScrollView()
        scroll.documentView = textView
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder

        let subs: [NSView] = [popup, autoCheck, scroll]
        for sub in subs {
            sub.translatesAutoresizingMaskIntoConstraints = false
            v.addSubview(sub)
        }
        NSLayoutConstraint.activate([
            popup.topAnchor.constraint(equalTo: v.topAnchor, constant: 14),
            popup.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 24),
            popup.widthAnchor.constraint(equalToConstant: 200),
            autoCheck.centerYAnchor.constraint(equalTo: popup.centerYAnchor),
            autoCheck.leadingAnchor.constraint(equalTo: popup.trailingAnchor, constant: 14),
            scroll.topAnchor.constraint(equalTo: popup.bottomAnchor, constant: 10),
            scroll.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 24),
            scroll.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -24),
            scroll.bottomAnchor.constraint(equalTo: v.bottomAnchor, constant: -16),
        ])
        return v
    }

    @objc private func logChanged() { refreshNow() }

    func refreshNow() {
        let name = Self.logFiles[popup.indexOfSelectedItem]
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let content = DataStore.shared.tailLog(name, maxBytes: 120_000)
            DispatchQueue.main.async {
                guard let self = self else { return }
                let wasAtBottom = self.textView.isScrolledToBottom
                self.textView.string = content
                if self.autoCheck.state == .on || wasAtBottom {
                    self.textView.scrollToEndOfDocument(nil)
                }
            }
        }
    }

    func refresh() {
        if autoCheck.state == .on { refreshNow() }
    }
}

extension NSTextView {
    var isScrolledToBottom: Bool {
        guard let sv = enclosingScrollView else { return true }
        return sv.contentView.bounds.maxY >= sv.documentView!.bounds.maxY - 40
    }
}

// 参数页
final class ConfigPage: NSObject, Page {
    private let configTable: SimpleTable
    private let note = NSTextField(wrappingLabelWithString: "")

    var view: NSView {
        let v = NSView()
        note.font = NSFont.systemFont(ofSize: 11)
        note.textColor = .secondaryLabelColor
        configTable.scroll.translatesAutoresizingMaskIntoConstraints = false
        note.translatesAutoresizingMaskIntoConstraints = false
        v.addSubview(note)
        v.addSubview(configTable.scroll)
        NSLayoutConstraint.activate([
            note.topAnchor.constraint(equalTo: v.topAnchor, constant: 14),
            note.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 24),
            note.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -24),
            configTable.scroll.topAnchor.constraint(equalTo: note.bottomAnchor, constant: 8),
            configTable.scroll.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 24),
            configTable.scroll.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -24),
            configTable.scroll.bottomAnchor.constraint(equalTo: v.bottomAnchor, constant: -16),
        ])
        return v
    }

    override init() {
        configTable = SimpleTable(headers: ["配置项", "值"], widths: [240, 500])
        note.stringValue = "来自 ~/.cellbridge/run/config.yaml 与运行环境（只读展示）"
    }

    func refresh() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let ds = DataStore.shared
            var rows: [[String]] = ds.loadConfig().map { ($0.0, $0.1) }.map { [$0.0, $0.1] }
            rows.append(["二进制 · 网关", kGatewayBin])
            rows.append(["二进制 · 音频桥", kAudioBin])
            rows.append(["数据库", kDBPath])
            rows.append(["日志目录", kLogDir])
            rows.append(["adb 工具", kAdbPath + (FileManager.default.fileExists(atPath: kAdbPath) ? "（存在）" : "（不存在，mixer 状态将不可查）")])
            DispatchQueue.main.async { self?.configTable.update(rows) }
        }
    }
}

// MARK: - 主窗口

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    var window: NSWindow!
    var sidebarStack: NSStackView!
    var sidebarButtons: [NSButton] = []
    var contentContainer: NSView!
    var pages: [Page] = []
    var titles: [String] = []
    var currentIndex = 0
    var timer: Timer?

    func applicationDidFinishLaunching(_: Notification) {
        let overview = OverviewPage()
        let calls = CallsPage()
        let messages = MessagesPage()
        let audio = AudioPage()
        let logs = LogsPage()
        let config = ConfigPage()
        pages = [overview, calls, messages, audio, logs, config]
        titles = ["概览", "通话", "短信", "音频", "日志", "参数"]

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 920, height: 640),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "CellBridge Console"
        window.delegate = self
        window.titlebarAppearsTransparent = false
        window.minSize = NSSize(width: 820, height: 560)

        // 布局：侧栏 + 内容
        let sidebar = NSVisualEffectView()
        sidebar.material = .sidebar
        sidebar.blendingMode = .behindWindow
        sidebar.state = .active

        sidebarStack = NSStackView()
        sidebarStack.orientation = .vertical
        sidebarStack.spacing = 4
        sidebarStack.alignment = .leading
        sidebar.translatesAutoresizingMaskIntoConstraints = false
        sidebarStack.translatesAutoresizingMaskIntoConstraints = false
        sidebar.addSubview(sidebarStack)

        contentContainer = NSView()
        contentContainer.translatesAutoresizingMaskIntoConstraints = false

        let title = NSTextField(labelWithString: "CellBridge")
        title.font = NSFont.systemFont(ofSize: 15, weight: .bold)
        title.translatesAutoresizingMaskIntoConstraints = false
        sidebar.addSubview(title)

        window.contentView?.addSubview(sidebar)
        window.contentView?.addSubview(contentContainer)

        NSLayoutConstraint.activate([
            sidebar.topAnchor.constraint(equalTo: window.contentView!.topAnchor),
            sidebar.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor),
            sidebar.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor),
            sidebar.widthAnchor.constraint(equalToConstant: 176),

            title.topAnchor.constraint(equalTo: sidebar.topAnchor, constant: 44),
            title.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor, constant: 18),

            sidebarStack.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 18),
            sidebarStack.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor, constant: 12),
            sidebarStack.trailingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: -12),

            contentContainer.topAnchor.constraint(equalTo: window.contentView!.topAnchor),
            contentContainer.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor),
            contentContainer.leadingAnchor.constraint(equalTo: sidebar.trailingAnchor),
            contentContainer.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor),
        ])

        for (i, t) in titles.enumerated() {
            let b = NSButton(title: "  \(t)", target: self, action: #selector(sidebarTap(_:)))
            b.bezelStyle = .regularSquare
            b.isBordered = false
            b.tag = i
            b.font = NSFont.systemFont(ofSize: 13)
            b.contentTintColor = .labelColor
            b.translatesAutoresizingMaskIntoConstraints = false
            sidebarStack.addArrangedSubview(b)
            b.widthAnchor.constraint(equalTo: sidebarStack.widthAnchor, constant: -8).isActive = true
            b.heightAnchor.constraint(equalToConstant: 30).isActive = true
            sidebarButtons.append(b)
        }

        selectPage(0)
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        timer = Timer.scheduledTimer(withTimeInterval: kRefreshInterval, repeats: true) { [weak self] _ in
            self?.refreshCurrent()
        }
        refreshCurrent()
    }

    @objc private func sidebarTap(_ sender: NSButton) { selectPage(sender.tag) }

    private func selectPage(_ idx: Int) {
        currentIndex = idx
        contentContainer.subviews.forEach { $0.removeFromSuperview() }
        let pv = pages[idx].view
        pv.translatesAutoresizingMaskIntoConstraints = false
        contentContainer.addSubview(pv)
        NSLayoutConstraint.activate([
            pv.topAnchor.constraint(equalTo: contentContainer.topAnchor),
            pv.bottomAnchor.constraint(equalTo: contentContainer.bottomAnchor),
            pv.leadingAnchor.constraint(equalTo: contentContainer.leadingAnchor),
            pv.trailingAnchor.constraint(equalTo: contentContainer.trailingAnchor),
        ])
        for (i, b) in sidebarButtons.enumerated() {
            b.layer?.cornerRadius = 6
            b.wantsLayer = true
            b.layer?.backgroundColor = i == idx
                ? NSColor.quaternaryLabelColor.cgColor
                : NSColor.clear.cgColor
            b.contentTintColor = i == idx ? .labelColor : .secondaryLabelColor
        }
        refreshCurrent()
    }

    private func refreshCurrent() {
        guard currentIndex < pages.count else { return }
        pages[currentIndex].refresh()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_: NSApplication) -> Bool { true }
}

// MARK: - 入口

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
