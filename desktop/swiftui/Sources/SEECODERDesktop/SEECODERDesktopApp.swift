import SwiftUI
import AppKit

@MainActor
final class DesktopAppDelegate: NSObject, NSApplicationDelegate {
  func applicationDidFinishLaunching(_ notification: Notification) {
    NSApp.setActivationPolicy(.regular)
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { self.activateWindow() }
  }

  func activateWindow() {
    NSApp.activate(ignoringOtherApps: true)
    NSApp.windows.first(where: { $0.isVisible })?.makeKeyAndOrderFront(nil)
  }
}

@main
struct SEECODERDesktopApp: App {
  @StateObject private var store = DesktopStore()
  @NSApplicationDelegateAdaptor(DesktopAppDelegate.self) private var appDelegate

  init() {
    if let url = Bundle.module.url(forResource: "seecoder-logo", withExtension: "png"), let image = NSImage(contentsOf: url) {
      NSApplication.shared.applicationIconImage = image
    }
  }

  var body: some Scene {
    WindowGroup("SEECODER") { DesktopRoot().environmentObject(store).frame(minWidth: 1000, minHeight: 640).onAppear { appDelegate.activateWindow() } }
      .windowStyle(.hiddenTitleBar)
      .defaultSize(width: 1360, height: 860)
      .windowResizability(.contentMinSize)
      .commands { CommandGroup(after: .newItem) { Button("新对话") { store.newSession() }.keyboardShortcut("n", modifiers: .command) } }
  }
}

struct ChatMessage: Identifiable, Codable, Hashable {
  enum Role: String, Codable { case user, agent, system }
  let id: UUID
  let role: Role
  let content: String
  let createdAt: Date
  init(_ role: Role, _ content: String) { id = UUID(); self.role = role; self.content = content; createdAt = .now }
}

struct SessionModel: Identifiable, Codable, Hashable {
  var id = UUID(); var title = "新对话"; var workspace = ""; var messages: [ChatMessage] = []; var updatedAt = Date.now
}

struct DiffLine: Identifiable, Hashable { let id = UUID(); let kind: Kind; let text: String; enum Kind { case meta, file, hunk, add, remove, context } }
enum RenameKind: String, Identifiable { case session, workspace; var id: String { rawValue } }
struct TimelineEvent: Identifiable, Hashable {
  enum Tone: String, Hashable { case running, success, warning, failure, info }
  let id = UUID()
  let title: String
  let detail: String
  let tone: Tone
}

@MainActor
final class DesktopStore: ObservableObject {
  @Published var sessions: [SessionModel] = []
  @Published var selectedID: UUID?
  @Published var draft = ""
  @Published var mode = "ask"
  @Published var isRunning = false
  @Published var activity = ["桌面端已就绪 · 原生 SwiftUI"]
  @Published var timeline: [TimelineEvent] = []
  @Published var reviewFile: String?
  @Published var diffLines: [DiffLine] = []
  @Published var showCreateWorkspace = false
  @Published var workspaceParent = ""
  @Published var workspaceName = ""
  @Published var workspaceError = ""
  @Published var renameKind: RenameKind?
  @Published var renameText = ""
  @Published var renameError = ""
  private var process: Process?
  private var inputPipe: Pipe?
  private var outputBuffer = ""
  private let persistenceKey = "seecoder.swiftui.sessions.v1"

  init() { load(); if sessions.isEmpty { newSession() }; selectedID = sessions.first?.id }
  var currentIndex: Int? { sessions.firstIndex { $0.id == selectedID } }
  var current: SessionModel? { currentIndex.map { sessions[$0] } }
  var hasWorkspace: Bool { !(current?.workspace ?? "").isEmpty }

  func newSession() { sessions.insert(SessionModel(), at: 0); selectedID = sessions[0].id; reviewFile = nil; diffLines = []; save() }
  func select(_ session: SessionModel) { guard !isRunning else { return }; selectedID = session.id; reviewFile = nil; diffLines = [] }
  func chooseWorkspace() {
    let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false; panel.prompt = "选择开发区域"
    if panel.runModal() == .OK, let url = panel.url { applyWorkspace(url.path, activityText: "已选择本地工作区") }
  }
  func chooseWorkspaceParent() {
    let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false; panel.prompt = "选择父目录"
    if panel.runModal() == .OK, let url = panel.url { workspaceParent = url.path; workspaceError = "" }
  }
  func createWorkspace() {
    let name = workspaceName.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !workspaceParent.isEmpty, !name.isEmpty, name.count <= 80, !name.contains("/"), !name.contains("\\"), name != ".", name != ".." else { workspaceError = "请选择父目录，并输入有效的单层文件夹名称。"; return }
    let target = URL(fileURLWithPath: workspaceParent).appendingPathComponent(name)
    do { try FileManager.default.createDirectory(at: target, withIntermediateDirectories: false); showCreateWorkspace = false; applyWorkspace(target.path, activityText: "已创建本地工作区") }
    catch { workspaceError = "无法创建该文件夹。请确认名称未被占用且目录可写。" }
  }
  func applyWorkspace(_ path: String, activityText: String) { guard let index = currentIndex else { return }; sessions[index].workspace = path; sessions[index].updatedAt = .now; activity.insert(activityText + " · " + shortPath(path), at: 0); reviewFile = nil; diffLines = []; save() }
  func openCreateWorkspace() { workspaceParent = ""; workspaceName = ""; workspaceError = ""; showCreateWorkspace = true }
  func openRenameSession(_ session: SessionModel) { guard !isRunning else { return }; selectedID = session.id; renameText = session.title; renameError = ""; renameKind = .session }
  func openRenameWorkspace(_ session: SessionModel) { guard !isRunning, !session.workspace.isEmpty else { return }; selectedID = session.id; renameText = URL(fileURLWithPath: session.workspace).lastPathComponent; renameError = ""; renameKind = .workspace }
  func renameCurrent(_ kind: RenameKind) {
    let name = renameText.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !name.isEmpty else { renameError = "名称不能为空。"; return }
    guard let index = currentIndex else { return }
    if kind == .session { sessions[index].title = name; sessions[index].updatedAt = .now; renameKind = nil; save(); return }
    guard !name.contains("/"), !name.contains("\\"), name != ".", name != "..", name.count <= 80 else { renameError = "请输入有效的单层文件夹名称。"; return }
    let oldPath = sessions[index].workspace; guard !oldPath.isEmpty else { return }
    let oldURL = URL(fileURLWithPath: oldPath); let target = oldURL.deletingLastPathComponent().appendingPathComponent(name)
    guard target.path != oldPath else { renameKind = nil; return }
    guard !FileManager.default.fileExists(atPath: target.path) else { renameError = "同一目录下已存在同名文件夹。"; return }
    do { try FileManager.default.moveItem(at: oldURL, to: target); for i in sessions.indices where sessions[i].workspace == oldPath { sessions[i].workspace = target.path; sessions[i].updatedAt = .now }; activity.insert("已重命名本地工作区 · \(name)", at: 0); renameKind = nil; save() }
    catch { renameError = "无法重命名该文件夹。请确认其未被占用且父目录可写。" }
  }
  func send() {
    let task = draft.trimmingCharacters(in: .whitespacesAndNewlines); guard !task.isEmpty, hasWorkspace, !isRunning, let index = currentIndex else { return }
    sessions[index].messages.append(ChatMessage(.user, task)); sessions[index].title = sessions[index].title == "新对话" ? String(task.prefix(24)) : sessions[index].title; sessions[index].updatedAt = .now; draft = ""; isRunning = true; timeline = []; addTimeline("任务已提交", detail: "正在连接本地 AgentRunner", tone: .running); activity.insert("正在启动本地 AgentRunner", at: 0); save()
    if let process, process.isRunning, let inputPipe {
      do { try inputPipe.fileHandleForWriting.write(contentsOf: Data((task + "\n").utf8)); return }
      catch { self.process = nil; self.inputPipe = nil }
    }
    let sessionID = sessions[index].id.uuidString; let workspace = sessions[index].workspace; let storage = sessionStorageURL(id: sessionID).path
    let p = Process(); p.executableURL = URL(fileURLWithPath: "/usr/bin/env"); p.arguments = ["uv", "run", "seecoder", "chat", "--workspace", workspace, "--event-json", "--save", storage, "--mode", mode]
    let input = Pipe(); let output = Pipe(); let error = Pipe(); p.standardInput = input; p.standardOutput = output; p.standardError = error; process = p; inputPipe = input; outputBuffer = ""
    output.fileHandleForReading.readabilityHandler = { [weak self] handle in let data = handle.availableData; guard !data.isEmpty else { return }; Task { @MainActor in self?.consume(String(decoding: data, as: UTF8.self)) } }
    error.fileHandleForReading.readabilityHandler = { [weak self] handle in let data = handle.availableData; guard !data.isEmpty else { return }; Task { @MainActor in self?.recordCLIError(String(decoding: data, as: UTF8.self)) } }
    p.terminationHandler = { [weak self] _ in Task { @MainActor in guard let self else { return }; self.isRunning = false; self.process = nil; self.inputPipe = nil; self.addTimeline("本地 AgentRunner 已结束", detail: "可继续提交下一轮任务", tone: .info); self.save() } }
    do { try p.run(); try input.fileHandleForWriting.write(contentsOf: Data((task + "\n").utf8)) } catch { isRunning = false; append(.system, "无法启动本地 AgentRunner：\(error.localizedDescription)") }
  }
  func stop() { process?.terminate(); addTimeline("正在停止任务", detail: "已向本地 AgentRunner 发送终止请求", tone: .warning); activity.insert("已请求停止本地任务", at: 0) }
  func inspectDiff(_ file: String) {
    guard let workspace = current?.workspace, !workspace.isEmpty else { return }; reviewFile = file
    let p = Process(); p.executableURL = URL(fileURLWithPath: "/usr/bin/env"); p.arguments = ["git", "-C", workspace, "diff", "--no-ext-diff", "--unified=3", "--", file]
    let pipe = Pipe(); p.standardOutput = pipe
    do { try p.run(); p.waitUntilExit(); let text = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self); diffLines = text.split(separator: "\n", omittingEmptySubsequences: false).map { classifyDiff(String($0)) } }
    catch { diffLines = [DiffLine(kind: .context, text: "无法读取本地 Git 差异。")] }
  }
  func changedFiles() -> [(String, Int, Int)] {
    guard let workspace = current?.workspace, !workspace.isEmpty else { return [] }; let result = run("git", ["-C", workspace, "diff", "--numstat"])
    return result.split(separator: "\n").compactMap { row in let pieces = row.split(separator: "\t", maxSplits: 2); guard pieces.count == 3 else { return nil }; return (String(pieces[2]), Int(pieces[0]) ?? 0, Int(pieces[1]) ?? 0) }
  }
  private func consume(_ chunk: String) {
    outputBuffer += chunk
    while let newline = outputBuffer.range(of: "\n") {
      let line = String(outputBuffer[..<newline.lowerBound]); outputBuffer.removeSubrange(..<newline.upperBound)
      guard let data = line.data(using: .utf8), let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let event = object["event"] as? String, let value = object["data"] as? [String: Any] else { continue }
      handleEvent(event, data: value)
    }
  }
  private func handleEvent(_ event: String, data: [String: Any]) {
    switch event {
    case "token": if let text = data["text"] as? String { appendStreaming(text) }
    case "run_started": addTimeline("本地 AgentRunner 已启动", detail: "模式：\(data["mode"] as? String ?? mode)，最大步数：\(data["max_steps"] ?? "-")", tone: .running)
    case "model_request": addTimeline("正在请求模型", detail: "第 \(data["step"] ?? "-") 步：根据当前上下文规划下一步", tone: .running)
    case "tool_dispatch":
      let calls = data["calls"] as? [[String: Any]] ?? []
      if calls.isEmpty { addTimeline("准备调用本地工具", detail: "共 \(data["count"] ?? "-") 个工具调用", tone: .running) }
      for call in calls { addTimeline("调用工具：\(call["name"] as? String ?? "unknown")", detail: call["purpose"] as? String ?? "执行本地受限操作", tone: .running) }
    case "tool_result":
      let name = data["name"] as? String ?? "unknown"; let ok = data["ok"] as? Bool ?? false
      let purpose = data["purpose"] as? String ?? "本地工具执行完成"
      addTimeline(ok ? "工具成功：\(name)" : "工具失败：\(name)", detail: ok ? purpose : "\(purpose) · \(data["error"] as? String ?? "未知错误")", tone: ok ? .success : .failure)
    case "plan_proposal": addTimeline("已提出计划步骤", detail: data["description"] as? String ?? (data["name"] as? String ?? "本地操作"), tone: .warning)
    case "approval_request": addTimeline("等待批准", detail: "\(data["name"] as? String ?? "本地操作") 需要用户确认", tone: .warning)
    case "context_compacted": addTimeline("上下文已压缩", detail: "为控制上下文预算，已整理较早的对话内容", tone: .info)
    case "usage": addTimeline("用量更新", detail: "累计 tokens：\(data["total_tokens"] ?? "-")", tone: .info)
    case "run_finished": addTimeline("运行结束", detail: "状态：\(data["state"] ?? "-")，共 \(data["steps"] ?? "-") 步", tone: .success)
    case "configuration_error": let message = data["message"] as? String ?? "配置错误"; addTimeline("无法启动任务", detail: message, tone: .failure); append(.system, message); isRunning = false
    case "turn_outcome":
      if let final = data["final_text"] as? String, !final.isEmpty, current?.messages.last?.content != final { append(.agent, final) }
      isRunning = false; addTimeline("任务完成", detail: "状态：\(data["state"] ?? "-")", tone: .success); activity.insert("任务完成", at: 0)
    default: break
    }
  }
  private func addTimeline(_ title: String, detail: String, tone: TimelineEvent.Tone) { timeline.append(TimelineEvent(title: title, detail: detail, tone: tone)); if timeline.count > 60 { timeline.removeFirst(timeline.count - 60) } }
  private func recordCLIError(_ text: String) { let cleaned = text.trimmingCharacters(in: .whitespacesAndNewlines); guard !cleaned.isEmpty else { return }; activity.insert("CLI: " + cleaned, at: 0); addTimeline("本地进程提示", detail: cleaned, tone: .info) }
  private func appendStreaming(_ text: String) { guard let index = currentIndex else { return }; if sessions[index].messages.last?.role == .agent { sessions[index].messages[sessions[index].messages.count - 1] = ChatMessage(.agent, sessions[index].messages.last!.content + text) } else { sessions[index].messages.append(ChatMessage(.agent, text)) } }
  private func append(_ role: ChatMessage.Role, _ text: String) { guard let index = currentIndex else { return }; sessions[index].messages.append(ChatMessage(role, text)); save() }
  private func run(_ executable: String, _ arguments: [String]) -> String { let p = Process(); p.executableURL = URL(fileURLWithPath: "/usr/bin/env"); p.arguments = [executable] + arguments; let pipe = Pipe(); p.standardOutput = pipe; do { try p.run(); p.waitUntilExit(); return String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self) } catch { return "" } }
  private func classifyDiff(_ text: String) -> DiffLine { let kind: DiffLine.Kind = text.hasPrefix("@@") ? .hunk : text.hasPrefix("+++ ") || text.hasPrefix("--- ") ? .file : text.hasPrefix("+") ? .add : text.hasPrefix("-") ? .remove : text.hasPrefix("diff ") || text.hasPrefix("index ") ? .meta : .context; return DiffLine(kind: kind, text: text) }
  private func shortPath(_ path: String) -> String { URL(fileURLWithPath: path).lastPathComponent }
  private func sessionStorageURL(id: String) -> URL { let root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("SEECODER/sessions", isDirectory: true); try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true); return root.appendingPathComponent(id + ".json") }
  private func save() { if let data = try? JSONEncoder().encode(sessions) { UserDefaults.standard.set(data, forKey: persistenceKey) } }
  private func load() { if let data = UserDefaults.standard.data(forKey: persistenceKey), let stored = try? JSONDecoder().decode([SessionModel].self, from: data) { sessions = stored } }
}

struct DesktopRoot: View {
  @EnvironmentObject var store: DesktopStore
  var body: some View {
    HSplitView {
      Sidebar().frame(minWidth: 208, idealWidth: 238, maxWidth: 292, maxHeight: .infinity)
      Conversation().frame(minWidth: 440, maxWidth: .infinity, maxHeight: .infinity)
      Inspector().frame(minWidth: 250, idealWidth: 284, maxWidth: 390, maxHeight: .infinity)
    }
    .background(Color.canvas)
    .environment(\.colorScheme, .light)
    .preferredColorScheme(.light)
    .tint(Color.brandBlue)
    .sheet(isPresented: $store.showCreateWorkspace) { CreateWorkspaceSheet() }
    .sheet(item: $store.renameKind) { kind in RenameSheet(kind: kind) }
  }
}

struct Sidebar: View {
  @EnvironmentObject var store: DesktopStore
  var body: some View { VStack(alignment: .leading, spacing: 10) { HStack { Image("seecoder-logo", bundle: .module).resizable().frame(width: 25, height: 25).clipShape(RoundedRectangle(cornerRadius: 7)); Text("SEECODER").font(.system(size: 15, weight: .bold)).foregroundStyle(Color.ink); Spacer() }.padding(.bottom, 12); Button(action: store.newSession) { Label("新对话", systemImage: "square.and.pencil") }.buttonStyle(SidebarAction()); Button(action: store.chooseWorkspace) { Label("打开工作区", systemImage: "folder") }.buttonStyle(SidebarAction()); Button(action: store.openCreateWorkspace) { Label("新建工作区", systemImage: "folder.badge.plus") }.buttonStyle(SidebarAction()); Text("项目会话").font(.caption.weight(.semibold)).foregroundStyle(Color.muted).padding(.top, 16); ScrollView { LazyVStack(spacing: 3) { ForEach(store.sessions) { session in HStack(spacing: 4) { Button { store.select(session) } label: { VStack(alignment: .leading, spacing: 3) { Text(session.title).lineLimit(1).font(.system(size: 13, weight: .semibold)).foregroundStyle(Color.ink); Text(session.workspace.isEmpty ? "未选择工作区" : URL(fileURLWithPath: session.workspace).lastPathComponent).lineLimit(1).font(.caption).foregroundStyle(Color.muted) }.frame(maxWidth: .infinity, alignment: .leading).padding(8).background(store.selectedID == session.id ? Color.brandBlue.opacity(0.12) : .clear, in: RoundedRectangle(cornerRadius: 8)) }.buttonStyle(.plain); Menu { Button("重命名会话") { store.openRenameSession(session) }; if !session.workspace.isEmpty { Button("重命名工作区") { store.openRenameWorkspace(session) } } } label: { Image(systemName: "ellipsis").frame(width: 20, height: 28) }.menuStyle(.borderlessButton) } } } }; Spacer(minLength: 12); Divider(); Label("本地优先", systemImage: "checkmark.circle.fill").font(.caption.weight(.semibold)).foregroundStyle(Color.brandGreen); Text("会话仅保存在此设备\n不会保存 API key").font(.caption2).foregroundStyle(Color.muted) }.padding(16).background(Color.sidebar) }
}

struct Conversation: View {
  @EnvironmentObject var store: DesktopStore
  var body: some View { VStack(spacing: 0) { HStack { VStack(alignment: .leading, spacing: 3) { Text(store.current?.title ?? "新对话").font(.headline).foregroundStyle(Color.ink); Text(store.current?.workspace.isEmpty == false ? store.current!.workspace : "尚未选择本地开发区域").font(.caption).foregroundStyle(Color.muted).lineLimit(1) }; Spacer(); Button("选择工作区", action: store.chooseWorkspace).buttonStyle(.bordered) }.padding(.horizontal, 22).frame(height: 60); Divider(); ScrollViewReader { proxy in ScrollView { Group { if store.current?.messages.isEmpty != false { Onboarding() } else { LazyVStack(alignment: .leading, spacing: 18) { ForEach(store.current?.messages ?? []) { message in MessageBubble(message: message) }; if !store.timeline.isEmpty { ExecutionTimeline(compact: false) }; ChangeSummary() } } }.frame(maxWidth: 720, alignment: .leading).padding(.horizontal, 28).padding(.vertical, 26).frame(maxWidth: .infinity, alignment: .center) }.onChange(of: store.timeline.count) { _, _ in proxy.scrollTo("execution-timeline", anchor: .bottom) }.onChange(of: store.current?.messages.count) { _, _ in if let id = store.current?.messages.last?.id { proxy.scrollTo(id, anchor: .bottom) } } }; Composer() }.frame(maxWidth: .infinity, maxHeight: .infinity).background(Color.canvas) }
}

struct Onboarding: View { @EnvironmentObject var store: DesktopStore; var body: some View { VStack(spacing: 12) { Spacer(minLength: 48); Image("seecoder-logo", bundle: .module).resizable().frame(width: 50, height: 50).clipShape(RoundedRectangle(cornerRadius: 15)); Text("选择一个开发区域").font(.system(size: 27, weight: .bold)).foregroundStyle(Color.ink); Text("选择已有本地文件夹，或创建一个新的会话工作区。\n所有本地操作都会限制在你选定的目录中。").font(.system(size: 14)).multilineTextAlignment(.center).foregroundStyle(Color.muted); HStack { Button("选择本地文件夹", action: store.chooseWorkspace).buttonStyle(.borderedProminent); Button("新建会话工作区", action: store.openCreateWorkspace).buttonStyle(.bordered) }; Spacer(minLength: 28) }.frame(maxWidth: .infinity, minHeight: 260) } }
struct MessageBubble: View { let message: ChatMessage; var body: some View { VStack(alignment: .leading, spacing: 7) { Label(message.role == .user ? "你" : message.role == .agent ? "SEECODER" : "本地状态", systemImage: message.role == .user ? "person.fill" : "sparkle").font(.caption.weight(.semibold)).foregroundStyle(message.role == .user ? Color.brandBlue : Color.brandGreen); Text(message.content).textSelection(.enabled).font(.system(size: 14)).lineSpacing(5).padding(15).frame(maxWidth: 760, alignment: .leading).background(message.role == .user ? Color.brandCyan.opacity(0.14) : Color.white, in: RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.line, lineWidth: 1)) }.id(message.id) } }
struct ExecutionTimeline: View {
  @EnvironmentObject var store: DesktopStore
  let compact: Bool
  var body: some View {
    VStack(alignment: .leading, spacing: compact ? 8 : 10) {
      HStack { Label(store.isRunning ? "正在执行" : "执行轨迹", systemImage: store.isRunning ? "waveform.path.ecg" : "checkmark.circle").font(.headline).foregroundStyle(Color.ink); Spacer(); Text("\(store.timeline.count) 个事件").font(.caption).foregroundStyle(Color.muted) }
      ForEach(store.timeline) { item in HStack(alignment: .top, spacing: 9) { Image(systemName: icon(item.tone)).font(.caption.weight(.bold)).foregroundStyle(color(item.tone)).frame(width: 15); VStack(alignment: .leading, spacing: 2) { Text(item.title).font(.system(size: compact ? 12 : 13, weight: .semibold)).foregroundStyle(Color.ink); Text(item.detail).font(.system(size: compact ? 11 : 12)).foregroundStyle(Color.muted).lineLimit(compact ? 2 : nil) } }.padding(.vertical, compact ? 2 : 4) }
    }
    .padding(compact ? 10 : 14)
    .background(Color.white, in: RoundedRectangle(cornerRadius: 12))
    .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.line, lineWidth: 1))
    .id("execution-timeline")
  }
  private func icon(_ tone: TimelineEvent.Tone) -> String { switch tone { case .running: "arrow.triangle.2.circlepath"; case .success: "checkmark.circle.fill"; case .warning: "exclamationmark.circle.fill"; case .failure: "xmark.octagon.fill"; case .info: "info.circle.fill" } }
  private func color(_ tone: TimelineEvent.Tone) -> Color { switch tone { case .running: .brandBlue; case .success: .brandGreen; case .warning: .brandAmber; case .failure: .red; case .info: .muted } }
}
struct ChangeSummary: View { @EnvironmentObject var store: DesktopStore; var body: some View { let files = store.changedFiles(); if !files.isEmpty { VStack(alignment: .leading, spacing: 8) { Text("已编辑 \(files.count) 个文件").font(.headline); ForEach(files, id: \.0) { file in Button { store.inspectDiff(file.0) } label: { HStack { Text(file.0).font(.system(.caption, design: .monospaced)); Spacer(); Text("+\(file.1) −\(file.2)").font(.caption).foregroundStyle(.secondary) }.padding(.vertical, 5) }.buttonStyle(.plain) } }.padding(15).frame(maxWidth: 760).background(Color.white, in: RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.line, lineWidth: 1)) } } }
struct Composer: View { @EnvironmentObject var store: DesktopStore; var body: some View { VStack(spacing: 8) { HStack(alignment: .bottom, spacing: 10) { TextEditor(text: $store.draft).font(.system(size: 14)).foregroundStyle(Color.ink).scrollContentBackground(.hidden).background(Color.white).frame(height: 68).disabled(store.isRunning).overlay(alignment: .topLeading) { if store.draft.isEmpty { Text(store.hasWorkspace ? "描述一个真实的编程任务…" : "可先描述任务，选择工作区后即可发送…").foregroundStyle(Color.muted).padding(.top, 8).padding(.leading, 5).allowsHitTesting(false) } }; Button(action: store.send) { Image(systemName: "arrow.up") }.buttonStyle(.borderedProminent).disabled(!store.hasWorkspace || store.isRunning) }; HStack { Picker("模式", selection: $store.mode) { Text("询问").tag("ask"); Text("计划").tag("plan"); Text("自动").tag("auto") }.labelsHidden().frame(width: 80); Text(store.hasWorkspace ? "本地 · 受限执行" : "选择工作区后发送").font(.caption).foregroundStyle(Color.muted); Spacer(); if store.isRunning { Button("停止", action: store.stop).buttonStyle(.bordered) } } }.padding(11).frame(maxWidth: 720).background(.white, in: RoundedRectangle(cornerRadius: 13)).overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.line, lineWidth: 1)).frame(maxWidth: .infinity).padding(.horizontal, 24).padding(.bottom, 12) } }
struct Inspector: View { @EnvironmentObject var store: DesktopStore; var body: some View { VStack(alignment: .leading, spacing: 14) { Text(store.reviewFile == nil ? "运行状态" : "审阅变更").font(.title3.bold()); if let path = store.current?.workspace, !path.isEmpty { Label(URL(fileURLWithPath: path).lastPathComponent, systemImage: "folder").font(.caption).foregroundStyle(Color.muted) }; if let file = store.reviewFile { Text(file).font(.caption.monospaced()).foregroundStyle(Color.muted); ScrollView { LazyVStack(alignment: .leading, spacing: 0) { ForEach(store.diffLines) { line in Text(line.text.isEmpty ? " " : line.text).font(.system(.caption, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 8).padding(.vertical, 2).background(diffColor(line.kind)) } } }.background(.white, in: RoundedRectangle(cornerRadius: 10)).overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.line, lineWidth: 1)) } else { Text(store.isRunning ? "AgentRunner 正在本地执行。每一步都会记录在下方。" : "提交任务后，这里会显示模型请求、工具调用、执行结果与终止状态。").font(.caption).foregroundStyle(Color.muted).lineSpacing(4); if store.timeline.isEmpty { Spacer() } else { ScrollView { ExecutionTimeline(compact: true) } } } }.padding(20).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading).background(Color.inspector) }
  private func diffColor(_ kind: DiffLine.Kind) -> Color { switch kind { case .add: .green.opacity(0.12); case .remove: .red.opacity(0.10); case .file, .hunk: .blue.opacity(0.09); default: .clear } }
}
struct CreateWorkspaceSheet: View {
  @EnvironmentObject var store: DesktopStore
  @FocusState private var nameFieldFocused: Bool

  var body: some View {
    VStack(alignment: .leading, spacing: 14) {
      Text("新建会话工作区").font(.title2.bold()).foregroundStyle(Color.ink)
      Text("选择父目录并输入名称。SEECODER 仅创建这一个空文件夹。").font(.subheadline).foregroundStyle(Color.muted)
      HStack {
        Text(store.workspaceParent.isEmpty ? "尚未选择父目录" : store.workspaceParent).font(.caption.monospaced()).lineLimit(1).foregroundStyle(Color.ink)
        Spacer()
        Button("选择位置", action: store.chooseWorkspaceParent)
      }
      .padding(10)
      .background(Color.canvas, in: RoundedRectangle(cornerRadius: 8))
      TextField("例如 my-feature", text: $store.workspaceName)
        .textFieldStyle(.roundedBorder)
        .foregroundStyle(Color.ink)
        .tint(Color.brandBlue)
        .focused($nameFieldFocused)
      if !store.workspaceError.isEmpty { Text(store.workspaceError).font(.caption).foregroundStyle(.red) }
      HStack { Spacer(); Button("取消") { store.showCreateWorkspace = false }; Button("创建并开始", action: store.createWorkspace).buttonStyle(.borderedProminent) }
    }
    .padding(26)
    .frame(width: 460)
    .onAppear { DispatchQueue.main.async { nameFieldFocused = true } }
  }
}
struct RenameSheet: View {
  @EnvironmentObject var store: DesktopStore
  let kind: RenameKind
  @FocusState private var nameFieldFocused: Bool

  var body: some View {
    VStack(alignment: .leading, spacing: 14) {
      Text(kind == .session ? "重命名会话" : "重命名本地工作区").font(.title2.bold()).foregroundStyle(Color.ink)
      Text(kind == .session ? "仅更新本机保存的会话标题。" : "会在同一父目录下重命名该本地文件夹，并同步本机引用它的会话。").font(.subheadline).foregroundStyle(Color.muted)
      TextField(kind == .session ? "会话名称" : "文件夹名称", text: $store.renameText)
        .textFieldStyle(.roundedBorder)
        .foregroundStyle(Color.ink)
        .tint(Color.brandBlue)
        .focused($nameFieldFocused)
      if !store.renameError.isEmpty { Text(store.renameError).font(.caption).foregroundStyle(.red) }
      HStack { Spacer(); Button("取消") { store.renameKind = nil }; Button("重命名") { store.renameCurrent(kind) }.buttonStyle(.borderedProminent) }
    }
    .padding(26)
    .frame(width: 460)
    .onAppear { DispatchQueue.main.async { nameFieldFocused = true } }
  }
}
struct SidebarAction: ButtonStyle { func makeBody(configuration: Configuration) -> some View { configuration.label.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 8).padding(.horizontal, 9).background(configuration.isPressed ? Color.brandBlue.opacity(0.12) : .clear, in: RoundedRectangle(cornerRadius: 7)) } }
extension Color { static let canvas = Color(red: 0.985, green: 0.982, blue: 0.972); static let sidebar = Color(red: 0.955, green: 0.978, blue: 0.984); static let inspector = Color(red: 0.978, green: 0.976, blue: 0.958); static let line = Color(red: 0.83, green: 0.89, blue: 0.91); static let ink = Color(red: 0.10, green: 0.18, blue: 0.23); static let muted = Color(red: 0.35, green: 0.45, blue: 0.51); static let brandBlue = Color(red: 0.10, green: 0.48, blue: 0.84); static let brandCyan = Color(red: 0.30, green: 0.72, blue: 0.92); static let brandGreen = Color(red: 0.14, green: 0.69, blue: 0.43); static let brandAmber = Color(red: 0.98, green: 0.63, blue: 0.12) }
