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
      .commands { CommandGroup(after: .newItem) { Button("新对话") { store.openNewConversation() }.keyboardShortcut("n", modifiers: .command) } }
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

struct LocalChange: Codable, Hashable {
  var path: String
  var added: Int
  var deleted: Int
}

struct WorkItemModel: Codable, Hashable {
  var id: String
  var description: String
  var tool: String
  var status: String
  var evidence: String
}
struct TaskPlanModel: Codable, Hashable {
  var id: String
  var task: String
  var status: String
  var items: [WorkItemModel]
}

struct SessionModel: Identifiable, Codable, Hashable {
  var id = UUID(); var title = "新对话"; var workspace = ""; var messages: [ChatMessage] = []; var updatedAt = Date.now; var localChanges: [LocalChange] = []; var taskPlan: TaskPlanModel?
  init(workspace: String = "") { self.workspace = workspace }
  enum CodingKeys: String, CodingKey { case id, title, workspace, messages, updatedAt, localChanges, taskPlan }
  init(from decoder: Decoder) throws {
    let values = try decoder.container(keyedBy: CodingKeys.self)
    id = try values.decodeIfPresent(UUID.self, forKey: .id) ?? UUID()
    title = try values.decodeIfPresent(String.self, forKey: .title) ?? "新对话"
    workspace = try values.decodeIfPresent(String.self, forKey: .workspace) ?? ""
    messages = try values.decodeIfPresent([ChatMessage].self, forKey: .messages) ?? []
    updatedAt = try values.decodeIfPresent(Date.self, forKey: .updatedAt) ?? .now
    localChanges = try values.decodeIfPresent([LocalChange].self, forKey: .localChanges) ?? []
    taskPlan = try values.decodeIfPresent(TaskPlanModel.self, forKey: .taskPlan)
  }
}
struct DesktopPersistence: Codable {
  let sessions: [SessionModel]
  let selectedID: UUID?
}

struct ProjectGroup: Identifiable {
  let id: String
  let name: String
  let workspace: String
  let sessions: [SessionModel]
  let isPinned: Bool
  let isArchived: Bool
  var isUnassigned: Bool { workspace.isEmpty }
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
struct WorkUpdate: Identifiable, Hashable {
  let id = UUID()
  let title: String
  let detail: String
  let note: String
  var isComplete = false
}
struct PendingApproval: Identifiable, Hashable {
  enum Kind: Hashable { case tool, plan }
  let id = UUID()
  let kind: Kind
  let title: String
  let detail: String
}

@MainActor
final class DesktopStore: ObservableObject {
  @Published var sessions: [SessionModel] = []
  @Published var selectedID: UUID?
  @Published var draft = ""
  @Published var mode = "auto"
  @Published var isRunning = false
  @Published var activity = ["桌面端已就绪 · 原生 SwiftUI"]
  @Published var timeline: [TimelineEvent] = []
  // Main-chat progress is deliberately slimmer than the audit timeline. It is
  // transient, grouped by model tool batch, and never stores provider-only
  // reasoning deltas in local session history.
  @Published var workUpdates: [WorkUpdate] = []
  @Published var pendingApproval: PendingApproval?
  @Published var reviewFile: String?
  @Published var diffLines: [DiffLine] = []
  @Published var showNewConversation = false
  @Published var showCreateWorkspace = false
  @Published var showProjectSettings = false
  @Published var projectSettingsWorkspace = ""
  @Published var pinnedProjects: Set<String> = []
  @Published var archivedProjects: Set<String> = []
  @Published var archivedSessions: Set<UUID> = []
  @Published var workspaceParent = ""
  @Published var workspaceName = ""
  @Published var workspaceError = ""
  @Published var renameKind: RenameKind?
  @Published var renameText = ""
  @Published var renameError = ""
  private var process: Process?
  private var inputPipe: Pipe?
  private var outputBuffer = ""
  private var stopRequested = false
  private var pendingVisibleCommentary = ""
  private var activeWorkUpdateID: UUID?
  private var outstandingToolResults = 0
  private var lastSubmittedTask: String?
  private var lastSubmittedAt: Date?
  private let legacyPersistenceKey = "seecoder.swiftui.sessions.v1"
  private let pinnedProjectsKey = "seecoder.swiftui.pinned-projects.v1"
  private let archivedProjectsKey = "seecoder.swiftui.archived-projects.v1"
  private let archivedSessionsKey = "seecoder.swiftui.archived-sessions.v1"
  private let modeKey = "seecoder.swiftui.mode.v1"

  init() {
    load()
    loadProjectFlags()
    loadSessionFlags()
    if let storedMode = UserDefaults.standard.string(forKey: modeKey), ["ask", "plan", "auto"].contains(storedMode) { mode = storedMode }
    cleanupEmptyPlaceholderSessions()
    if selectedID == nil || !sessions.contains(where: { $0.id == selectedID }) { selectedID = sessions.first?.id }
    save()
  }
  var currentIndex: Int? { sessions.firstIndex { $0.id == selectedID } }
  var current: SessionModel? { currentIndex.map { sessions[$0] } }
  var hasWorkspace: Bool { !(current?.workspace ?? "").isEmpty }
  var projectGroups: [ProjectGroup] {
    let grouped = Dictionary(grouping: sessions) { $0.workspace }
    return grouped.keys.sorted { lhs, rhs in
      if lhs.isEmpty != rhs.isEmpty { return !lhs.isEmpty }
      let lhsArchived = archivedProjects.contains(lhs), rhsArchived = archivedProjects.contains(rhs)
      if lhsArchived != rhsArchived { return !lhsArchived }
      let lhsPinned = pinnedProjects.contains(lhs), rhsPinned = pinnedProjects.contains(rhs)
      if lhsPinned != rhsPinned { return lhsPinned }
      return (lhs.isEmpty ? "未选择项目" : shortPath(lhs)).localizedStandardCompare(lhs.isEmpty ? "未选择项目" : shortPath(rhs)) == .orderedAscending
    }.compactMap { workspace in
      guard let groupedSessions = grouped[workspace] else { return nil }
      let name = workspace.isEmpty ? "未选择项目" : shortPath(workspace)
      return ProjectGroup(id: workspace.isEmpty ? "unassigned" : workspace, name: name, workspace: workspace, sessions: groupedSessions.sorted { lhs, rhs in
        let lhsArchived = archivedSessions.contains(lhs.id), rhsArchived = archivedSessions.contains(rhs.id)
        if lhsArchived != rhsArchived { return !lhsArchived }
        return lhs.updatedAt > rhs.updatedAt
      }, isPinned: pinnedProjects.contains(workspace), isArchived: archivedProjects.contains(workspace))
    }
  }

  func newSession() { openNewConversation() }
  func persistMode() { guard ["ask", "plan", "auto"].contains(mode) else { mode = "auto"; return }; UserDefaults.standard.set(mode, forKey: modeKey) }
  func openNewConversation() { guard !isRunning else { return }; showNewConversation = true }
  func startSession(in workspace: String) {
    let session = SessionModel(workspace: workspace)
    sessions.insert(session, at: 0); selectedID = session.id; showNewConversation = false; reviewFile = nil; diffLines = []; resetWorkUpdates(); activity.insert("已在项目中创建新对话 · \(shortPath(workspace))", at: 0); save()
  }
  func select(_ session: SessionModel) { guard !isRunning else { return }; selectedID = session.id; reviewFile = nil; diffLines = []; resetWorkUpdates(); save() }
  func chooseWorkspace() {
    let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false; panel.prompt = "选择开发区域"
    if panel.runModal() == .OK, let url = panel.url {
      if currentIndex == nil { startSession(in: url.path) } else { applyWorkspace(url.path, activityText: "已选择本地工作区") }
    }
  }
  func openProject() {
    let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false; panel.prompt = "打开项目"
    guard panel.runModal() == .OK, let url = panel.url else { return }
    if let session = sessions.first(where: { !$0.workspace.isEmpty && URL(fileURLWithPath: $0.workspace).standardizedFileURL.path == url.standardizedFileURL.path }) { select(session) }
    else { startSession(in: url.path) }
  }
  func chooseWorkspaceForNewConversation() {
    let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false; panel.prompt = "选择项目文件夹"
    if panel.runModal() == .OK, let url = panel.url { startSession(in: url.path) }
  }
  func openNewProject() {
    showNewConversation = false
    // Wait for the first sheet to dismiss before presenting the project sheet.
    // This avoids a transient "already presenting" warning on macOS.
    DispatchQueue.main.async { self.openCreateWorkspace() }
  }
  func togglePinnedProject(_ project: ProjectGroup) {
    guard !project.isUnassigned else { return }
    if pinnedProjects.contains(project.workspace) { pinnedProjects.remove(project.workspace) } else { pinnedProjects.insert(project.workspace) }
    saveProjectFlags()
  }
  func toggleArchivedProject(_ project: ProjectGroup) {
    guard !project.isUnassigned else { return }
    if archivedProjects.contains(project.workspace) { archivedProjects.remove(project.workspace) } else { archivedProjects.insert(project.workspace) }
    saveProjectFlags()
  }
  func isSessionArchived(_ session: SessionModel) -> Bool { archivedSessions.contains(session.id) }
  func toggleArchivedSession(_ session: SessionModel) {
    guard !isRunning else { return }
    if archivedSessions.contains(session.id) {
      archivedSessions.remove(session.id)
      activity.insert("已恢复会话 · \(session.title)", at: 0)
    } else {
      archivedSessions.insert(session.id)
      activity.insert("已归档会话 · \(session.title)", at: 0)
    }
    save()
  }
  func showProjectInFinder(_ project: ProjectGroup) {
    guard !project.isUnassigned else { return }
    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: project.workspace)])
  }
  func openProjectSettings(_ project: ProjectGroup) {
    guard !project.isUnassigned else { return }
    projectSettingsWorkspace = project.workspace
    showProjectSettings = true
  }
  func detachProject(_ project: ProjectGroup) {
    guard !project.isUnassigned, !isRunning else { return }
    for index in sessions.indices where sessions[index].workspace == project.workspace {
      sessions[index].workspace = ""
      sessions[index].updatedAt = .now
    }
    pinnedProjects.remove(project.workspace)
    archivedProjects.remove(project.workspace)
    activity.insert("已从项目列表移除 · 本地文件未删除", at: 0)
    save()
  }
  func chooseWorkspaceParent() {
    let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false; panel.prompt = "选择父目录"
    if panel.runModal() == .OK, let url = panel.url { workspaceParent = url.path; workspaceError = "" }
  }
  func createWorkspace() {
    let name = workspaceName.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !workspaceParent.isEmpty, !name.isEmpty, name.count <= 80, !name.contains("/"), !name.contains("\\"), name != ".", name != ".." else { workspaceError = "请选择父目录，并输入有效的单层文件夹名称。"; return }
    let target = URL(fileURLWithPath: workspaceParent).appendingPathComponent(name)
    do { try FileManager.default.createDirectory(at: target, withIntermediateDirectories: false); showCreateWorkspace = false; startSession(in: target.path); activity.insert("已创建新项目 · \(name)", at: 0) }
    catch { workspaceError = "无法创建该文件夹。请确认名称未被占用且目录可写。" }
  }
  func applyWorkspace(_ path: String, activityText: String) { guard let index = currentIndex else { return }; if sessions[index].workspace != path { sessions[index].localChanges = [] }; sessions[index].workspace = path; sessions[index].updatedAt = .now; activity.insert(activityText + " · " + shortPath(path), at: 0); reviewFile = nil; diffLines = []; save() }
  func openCreateWorkspace() { workspaceParent = ""; workspaceName = "新项目"; workspaceError = ""; showCreateWorkspace = true }
  func openRenameSession(_ session: SessionModel) { guard !isRunning else { return }; selectedID = session.id; renameText = session.title; renameError = ""; renameKind = .session }
  func openRenameCurrentWorkspace() { guard let session = current else { return }; openRenameWorkspace(session) }
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
    // Protect the long-lived stdin protocol from a double click or a
    // keyboard shortcut delivered immediately after the click. A later,
    // intentional retry remains available once this short window expires.
    if lastSubmittedTask == task, let lastSubmittedAt, Date.now.timeIntervalSince(lastSubmittedAt) < 2 { return }
    lastSubmittedTask = task; lastSubmittedAt = .now
    sessions[index].messages.append(ChatMessage(.user, task)); sessions[index].title = sessions[index].title == "新对话" ? String(task.prefix(24)) : sessions[index].title; sessions[index].updatedAt = .now; draft = ""; isRunning = true; timeline = []; resetWorkUpdates(); pendingApproval = nil; addTimeline("任务已提交", detail: "正在连接本地 AgentRunner", tone: .running); activity.insert("正在启动本地 AgentRunner", at: 0); save()
    if let process, process.isRunning, let inputPipe {
      do { try inputPipe.fileHandleForWriting.write(contentsOf: Data((task + "\n").utf8)); return }
      catch { self.process = nil; self.inputPipe = nil }
    }
    let sessionID = sessions[index].id.uuidString; let workspace = sessions[index].workspace; let storage = sessionStorageURL(id: sessionID).path
    let root = projectRootURL()
    let envFile = root.appendingPathComponent(".env")
    guard let invocation = agentInvocation(root: root, workspace: workspace, storage: storage, sessionID: sessionID) else {
      lastSubmittedTask = nil; lastSubmittedAt = nil
      isRunning = false
      append(.system, "无法启动本地 AgentRunner：找不到 uv 或项目虚拟环境中的 Python。请先在项目根目录运行 uv sync。")
      addTimeline("无法启动本地 AgentRunner", detail: "未找到可执行的 uv 或 .venv/bin/python", tone: .failure)
      return
    }
    let p = Process(); p.executableURL = invocation.executable; p.currentDirectoryURL = root
    p.arguments = invocation.arguments + ["--mode", mode]
    p.environment = launchEnvironment(root: root)
    if FileManager.default.fileExists(atPath: envFile.path) { p.arguments = (p.arguments ?? []) + ["--env-file", envFile.path] }
    let input = Pipe(); let output = Pipe(); let error = Pipe(); p.standardInput = input; p.standardOutput = output; p.standardError = error; process = p; inputPipe = input; outputBuffer = ""; stopRequested = false
    output.fileHandleForReading.readabilityHandler = { [weak self] handle in let data = handle.availableData; guard !data.isEmpty else { return }; Task { @MainActor in self?.consume(String(decoding: data, as: UTF8.self)) } }
    error.fileHandleForReading.readabilityHandler = { [weak self] handle in let data = handle.availableData; guard !data.isEmpty else { return }; Task { @MainActor in self?.recordCLIError(String(decoding: data, as: UTF8.self)) } }
    p.terminationHandler = { [weak self] process in
      let status = process.terminationStatus
      let reason = process.terminationReason
      Task { @MainActor in
        guard let self else { return }
        self.isRunning = false
        self.pendingApproval = nil
        self.process = nil
        self.inputPipe = nil
        let wasStopped = self.stopRequested
        self.stopRequested = false
        if status == 0 || wasStopped {
          self.addTimeline(wasStopped ? "任务已停止" : "本地 AgentRunner 已结束", detail: "可继续提交下一轮任务", tone: wasStopped ? .warning : .info)
        } else {
          let reasonText = reason == .uncaughtSignal ? "收到信号 " + String(status) : "退出码 " + String(status)
          self.addTimeline("本地 AgentRunner 异常退出", detail: reasonText + " · 请检查上方轨迹和配置", tone: .failure)
          self.activity.insert("本地任务异常退出 · " + reasonText, at: 0)
        }
        self.save()
      }
    }
    do { try p.run(); try input.fileHandleForWriting.write(contentsOf: Data((task + "\n").utf8)) } catch { lastSubmittedTask = nil; lastSubmittedAt = nil; isRunning = false; append(.system, "无法启动本地 AgentRunner：\(error.localizedDescription)"); addTimeline("无法启动本地 AgentRunner", detail: invocation.executable.path, tone: .failure) }
  }
  func stop() { stopRequested = true; process?.terminate(); pendingApproval = nil; addTimeline("正在停止任务", detail: "已向本地 AgentRunner 发送终止请求", tone: .warning); activity.insert("已请求停止本地任务", at: 0) }
  func decideApproval(_ approved: Bool) {
    guard let inputPipe, isRunning else { return }
    do {
      try inputPipe.fileHandleForWriting.write(contentsOf: Data((approved ? "y\n" : "n\n").utf8))
      addTimeline(approved ? "已批准本地操作" : "已拒绝本地操作", detail: pendingApproval?.title ?? "等待 AgentRunner 继续", tone: approved ? .success : .warning)
      pendingApproval = nil
    } catch { addTimeline("无法提交批准决定", detail: "本地 AgentRunner 输入通道不可用", tone: .failure) }
  }
  func inspectDiff(_ file: String) {
    guard let workspace = current?.workspace, !workspace.isEmpty else { return }; reviewFile = file
    let p = Process(); p.executableURL = URL(fileURLWithPath: "/usr/bin/env"); p.arguments = ["git", "-C", workspace, "diff", "HEAD", "--no-ext-diff", "--no-color", "--unified=3", "--", file]
    let pipe = Pipe(); p.standardOutput = pipe
    do { try p.run(); p.waitUntilExit(); var text = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self); if text.isEmpty { let absolute = URL(fileURLWithPath: workspace).appendingPathComponent(file).path; text = run("git", ["-C", workspace, "diff", "--no-index", "--no-ext-diff", "--no-color", "--unified=3", "/dev/null", absolute]) }; diffLines = text.isEmpty ? [DiffLine(kind: .context, text: "未检测到可显示的 Git 差异，或该文件已被删除。")] : text.split(separator: "\n", omittingEmptySubsequences: false).map { classifyDiff(String($0)) } }
    catch { diffLines = [DiffLine(kind: .context, text: "无法读取本地 Git 差异。")] }
  }
  func changedFiles() -> [(String, Int, Int)] {
    guard let workspace = current?.workspace, !workspace.isEmpty else { return [] }
    let tracked = run("git", ["-C", workspace, "diff", "HEAD", "--numstat"])
    var rows = tracked.split(separator: "\n").compactMap { row -> (String, Int, Int)? in
      let pieces = row.split(separator: "\t", maxSplits: 2); guard pieces.count == 3 else { return nil }
      return (String(pieces[2]), Int(pieces[0]) ?? 0, Int(pieces[1]) ?? 0)
    }
    let status = run("git", ["-C", workspace, "status", "--porcelain=v1", "--untracked-files=all"])
    for line in status.split(separator: "\n") where line.hasPrefix("?? ") {
      let path = String(line.dropFirst(3)); let url = URL(fileURLWithPath: workspace).appendingPathComponent(path)
      guard let content = try? String(contentsOf: url, encoding: .utf8) else { continue }
      let additions = content.isEmpty ? 0 : content.split(separator: "\n", omittingEmptySubsequences: false).count - (content.hasSuffix("\n") ? 1 : 0)
      rows.append((path, additions, 0))
    }
    return rows.isEmpty ? (current?.localChanges ?? []).map { ($0.path, $0.added, $0.deleted) } : rows
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
    case "changeset_updated":
      let files = (data["files"] as? [String] ?? []).joined(separator: ", ")
      addTimeline("ChangeSet 已记录", detail: files.isEmpty ? (data["tool"] as? String ?? "目录操作") : files, tone: .success)
    case "changeset_error":
      addTimeline("ChangeSet 记录警告", detail: data["message"] as? String ?? "本次变更无法完整记录", tone: .warning)
    case "checkpoint_created":
      addTimeline("运行检查点已创建", detail: data["changeset_id"] as? String ?? "本轮 ChangeSet 已持久化", tone: .success)
    case "plan_state":
      let items = (data["items"] as? [[String: Any]] ?? []).map { item in
        WorkItemModel(id: item["id"] as? String ?? UUID().uuidString,
                      description: item["description"] as? String ?? item["tool"] as? String ?? "本地操作",
                      tool: item["tool"] as? String ?? "",
                      status: item["status"] as? String ?? "pending",
                      evidence: item["evidence"] as? String ?? "")
      }
      if let index = currentIndex, let planID = data["plan_id"] as? String {
        sessions[index].taskPlan = TaskPlanModel(id: planID, task: data["task"] as? String ?? "", status: data["status"] as? String ?? "proposed", items: items)
        sessions[index].updatedAt = .now; save()
      }
      let completed = items.filter { $0.status == "completed" }.count
      let status = data["status"] as? String ?? "proposed"
      addTimeline("计划状态：\(status)", detail: "\(completed)/\(items.count) 步已完成", tone: ["failed", "cancelled"].contains(status) ? .failure : status == "completed" ? .success : .running)
      if status == "cancelled" { pendingApproval = nil; isRunning = false }
    // Provider text often contains a repetitive scratch narration before a
    // tool call. Keep a bounded, user-visible excerpt for a collapsed note;
    // do not append every delta as a permanent chat message.
    case "token": if let text = data["text"] as? String { captureVisibleCommentary(text) }
    case "reasoning": break
    case "run_started":
      if let startedMode = data["mode"] as? String, ["ask", "plan", "auto"].contains(startedMode) { mode = startedMode }
      addTimeline("本地 AgentRunner 已启动", detail: "模式：\(data["mode"] as? String ?? mode)，最大步数：\(data["max_steps"] ?? "-")", tone: .running)
    case "model_request": break
    case "tool_dispatch":
      let calls = data["calls"] as? [[String: Any]] ?? []
      if calls.isEmpty { addTimeline("准备本地动作", detail: "共 \(data["count"] ?? "-") 个工具调用", tone: .running) }
      for call in calls { let name = call["name"] as? String ?? "unknown"; addTimeline("准备：\(toolLabel(name))", detail: call["purpose"] as? String ?? "执行本地受限操作", tone: .running) }
      addWorkUpdate(for: calls)
    case "tool_result":
      let name = data["name"] as? String ?? "unknown"; let ok = data["ok"] as? Bool ?? false
      let purpose = data["purpose"] as? String ?? "本地工具执行完成"
      let planned = (data["error"] as? String) == "PlanMode"
      addTimeline(planned ? "计划已记录：\(toolLabel(name))" : (ok ? "已完成：\(toolLabel(name))" : "失败：\(toolLabel(name))"), detail: planned ? purpose : (ok ? toolResultDetail(data) : "\(purpose) · \(data["error"] as? String ?? "未知错误")"), tone: planned ? .running : (ok ? .success : .failure))
      if ok, ["write_file", "apply_patch", "delete_file", "copy_file", "move_file"].contains(name), let result = data["data"] as? [String: Any] { recordLocalChange(name: name, result: result) }
      if ok, name == "rename_directory", let result = data["data"] as? [String: Any], result["workspace_renamed"] as? Bool == true,
         let oldPath = result["old_path"] as? String, let newPath = result["workspace_path"] as? String {
        applyAgentWorkspaceRename(oldPath: oldPath, newPath: newPath)
      }
      completeWorkUnitIfNeeded()
    case "plan_proposal": let name = data["name"] as? String ?? "本地操作"; addTimeline("计划动作：\(toolLabel(name))", detail: data["description"] as? String ?? name, tone: .warning)
    case "approval_request":
      let name = data["name"] as? String ?? "本地操作"; let detail = "\(name) 需要用户确认后才会在本地执行"
      pendingApproval = PendingApproval(kind: .tool, title: name, detail: detail); addTimeline("等待批准", detail: detail, tone: .warning)
    case "plan_approval_request":
      let detail = data["message"] as? String ?? "Plan 模式已生成修改计划，等待批准执行"
      pendingApproval = PendingApproval(kind: .plan, title: "执行计划", detail: detail); addTimeline("等待批准", detail: detail, tone: .warning)
    case "context_compacted": addTimeline("上下文已压缩", detail: "为控制上下文预算，已整理较早的对话内容", tone: .info)
    case "usage": addTimeline("用量更新", detail: "累计 tokens：\(data["total_tokens"] ?? "-")", tone: .info)
    case "run_finished":
      let state = data["state"] as? String ?? "unknown"
      if state == "awaiting_approval" {
        let calls = data["pending_calls"] as? [[String: Any]] ?? []
        let name = calls.first?["name"] as? String ?? "本地操作"
        pendingApproval = PendingApproval(kind: .tool, title: name, detail: "该操作正在等待你的批准，并会在重启后保留")
        addTimeline("等待批准", detail: "状态：awaiting_approval · \(name)", tone: .warning)
        return
      }
      let limited = state == "stop_max_steps"
      let tone: TimelineEvent.Tone = state == "final" || state == "plan_proposed" ? .success : (limited ? .warning : .failure)
      addTimeline(limited ? "本轮达到执行上限" : (state == "final" ? "运行结束" : "运行停止"), detail: limited ? "共 \(data["steps"] ?? "-") 步 · 当前变更已保留，可继续" : "状态：\(state)，共 \(data["steps"] ?? "-") 步", tone: tone)
    case "configuration_error": let message = data["message"] as? String ?? "配置错误"; addTimeline("无法启动任务", detail: message, tone: .failure); append(.system, message); isRunning = false
    case "turn_outcome":
      let state = data["state"] as? String ?? "unknown"
      if let final = data["final_text"] as? String, !final.isEmpty, current?.messages.last?.content != final { append(.agent, final) }
      if state == "plan_proposed" {
        addTimeline("计划已生成", detail: "等待批准后执行计划", tone: .warning)
      } else {
        isRunning = false; pendingApproval = nil
        let limited = state == "stop_max_steps"
        let recoverable = data["recoverable"] as? Bool ?? ["failed_model", "failed_protocol", "stop_max_steps", "stop_context_budget", "stop_task_timeout", "cancelled"].contains(state)
        let terminalTone: TimelineEvent.Tone = state == "final" ? .success : (limited ? .warning : .failure)
        let detail = limited ? "共 \(data["steps"] ?? "-") 步 · 当前变更已保留，可继续发送" : (recoverable && state != "final" ? "状态：\(state) · 上一轮已保留，可继续发送" : "状态：\(state)")
        addTimeline(limited ? "本轮达到执行上限" : (state == "final" ? "任务完成" : "任务结束"), detail: detail, tone: terminalTone)
        activity.insert(limited ? "本轮达到执行上限 · 可继续" : (state == "final" ? "任务完成" : "任务结束 · \(state)"), at: 0)
      }
    default: break
    }
  }
  private func toolLabel(_ name: String) -> String { ["read_file": "读取文件", "search_files": "搜索文件", "search_code": "检索代码", "find_files": "查找文件", "project_overview": "分析项目结构", "write_file": "写入文件", "apply_patch": "应用补丁", "delete_file": "删除文件", "create_directory": "创建目录", "copy_file": "复制文件", "move_file": "移动文件", "rename_directory": "重命名目录", "run_command": "运行命令", "git_diff": "检查 Git 差异", "git_status": "检查 Git 状态", "git_log": "读取 Git 历史", "git_show": "读取提交", "web_search": "搜索资料"][name] ?? name }
  private func toolResultDetail(_ data: [String: Any]) -> String {
    let result = data["data"] as? [String: Any] ?? [:]
    var details: [String] = []
    if let path = (result["path"] ?? result["destination"] ?? result["workspace_path"] ?? result["new_path"] ?? result["source"]) as? String { details.append(path) }
    if let bytes = (result["bytes_written"] as? NSNumber)?.intValue { details.append("\(bytes) bytes") }
    if let lines = (result["line_count"] as? NSNumber)?.intValue { details.append("\(lines) 行") }
    if result["added_lines"] != nil || result["deleted_lines"] != nil { details.append("+\((result["added_lines"] as? NSNumber)?.intValue ?? 0) −\((result["deleted_lines"] as? NSNumber)?.intValue ?? 0) 行") }
    if result["created"] as? Bool == true { details.append("已创建") }
    if result["deleted"] as? Bool == true { details.append("已删除") }
    if result["changed"] as? Bool == true { details.append("已变更") }
    return details.isEmpty ? "工具已完成" : details.joined(separator: " · ")
  }
  private func addTimeline(_ title: String, detail: String, tone: TimelineEvent.Tone) { timeline.append(TimelineEvent(title: title, detail: detail, tone: tone)); if timeline.count > 60 { timeline.removeFirst(timeline.count - 60) } }
  private func recordCLIError(_ text: String) { let cleaned = text.trimmingCharacters(in: .whitespacesAndNewlines); guard !cleaned.isEmpty else { return }; activity.insert("CLI: " + cleaned, at: 0); addTimeline("本地进程提示", detail: cleaned, tone: .info) }
  private func resetWorkUpdates() {
    workUpdates = []
    pendingVisibleCommentary = ""
    activeWorkUpdateID = nil
    outstandingToolResults = 0
  }
  private func captureVisibleCommentary(_ text: String) {
    // This is only the ordinary visible assistant channel. Provider reasoning
    // deltas stay discarded in `handleEvent`, rather than being exposed as
    // chain-of-thought or persisted locally.
    guard pendingVisibleCommentary.count < 700 else { return }
    let normalized = text.replacingOccurrences(of: "\r", with: "")
    let remaining = 700 - pendingVisibleCommentary.count
    pendingVisibleCommentary += String(normalized.prefix(remaining))
  }
  private func addWorkUpdate(for calls: [[String: Any]]) {
    guard !calls.isEmpty else { return }
    let labels = Array(Set(calls.map { toolLabel($0["name"] as? String ?? "unknown") })).sorted()
    let title = labels.count == 1 ? "正在处理：\(labels[0])" : "正在处理 \(labels.count) 项工作"
    let purposes = calls.compactMap { ($0["purpose"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
    let detail = purposes.isEmpty ? labels.joined(separator: "、") : Array(purposes.prefix(2)).joined(separator: "；")
    let note = pendingVisibleCommentary.trimmingCharacters(in: .whitespacesAndNewlines)
    let update = WorkUpdate(title: title, detail: detail, note: note)
    workUpdates.append(update)
    if workUpdates.count > 16 { workUpdates.removeFirst(workUpdates.count - 16) }
    activeWorkUpdateID = update.id
    outstandingToolResults = calls.count
    pendingVisibleCommentary = ""
  }
  private func completeWorkUnitIfNeeded() {
    guard outstandingToolResults > 0 else { return }
    outstandingToolResults -= 1
    guard outstandingToolResults == 0, let activeWorkUpdateID,
          let index = workUpdates.firstIndex(where: { $0.id == activeWorkUpdateID }) else { return }
    workUpdates[index].isComplete = true
    self.activeWorkUpdateID = nil
  }
  private func append(_ role: ChatMessage.Role, _ text: String) { guard let index = currentIndex else { return }; sessions[index].messages.append(ChatMessage(role, text)); save() }
  private func recordLocalChange(name: String, result: [String: Any]) {
    guard let index = currentIndex else { return }
    var paths: [String] = []
    if let path = result["path"] as? String, path != "." { paths.append(path) }
    if let destination = result["destination"] as? String { paths.append(destination) }
    guard !paths.isEmpty else { return }
    let added = (result["added_lines"] as? NSNumber)?.intValue ?? 0
    let deleted = (result["deleted_lines"] as? NSNumber)?.intValue ?? 0
    objectWillChange.send()
    for path in paths {
      if let existing = sessions[index].localChanges.firstIndex(where: { $0.path == path }) {
        sessions[index].localChanges[existing].added = max(sessions[index].localChanges[existing].added, added)
        sessions[index].localChanges[existing].deleted = max(sessions[index].localChanges[existing].deleted, deleted)
      } else {
        sessions[index].localChanges.append(LocalChange(path: path, added: added, deleted: deleted))
      }
    }
    sessions[index].updatedAt = .now
    save()
  }
  private func applyAgentWorkspaceRename(oldPath: String, newPath: String) {
    let oldURL = URL(fileURLWithPath: oldPath).standardizedFileURL
    let newURL = URL(fileURLWithPath: newPath).standardizedFileURL
    var changed = false
    for index in sessions.indices {
      let workspaceURL = URL(fileURLWithPath: sessions[index].workspace).standardizedFileURL
      if !sessions[index].workspace.isEmpty && workspaceURL.path == oldURL.path {
        sessions[index].workspace = newURL.path
        sessions[index].updatedAt = .now
        changed = true
      }
    }
    guard changed else { return }
    activity.insert("Agent 已重命名工作区 · \(newURL.lastPathComponent)", at: 0)
    addTimeline("工作区路径已更新", detail: newURL.path, tone: .success)
    save()
  }
  private func run(_ executable: String, _ arguments: [String]) -> String { let p = Process(); p.executableURL = URL(fileURLWithPath: "/usr/bin/env"); p.arguments = [executable] + arguments; let pipe = Pipe(); p.standardOutput = pipe; p.standardError = Pipe(); do { try p.run(); p.waitUntilExit(); return String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self) } catch { return "" } }
  private func agentInvocation(root: URL, workspace: String, storage: String, sessionID: String) -> (executable: URL, arguments: [String])? {
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    let configured = ProcessInfo.processInfo.environment["SEECODER_UV"]
    let inheritedPath = ProcessInfo.processInfo.environment["PATH"]?.split(separator: ":").map { String($0) + "/uv" } ?? []
    let uvCandidates = ([configured, "/opt/homebrew/bin/uv", "/usr/local/bin/uv", "\(home)/.local/bin/uv", "\(home)/.cargo/bin/uv"] + inheritedPath).compactMap { $0 }.map(URL.init(fileURLWithPath:))
    if let uv = uvCandidates.first(where: { FileManager.default.isExecutableFile(atPath: $0.path) }) {
      var arguments = ["run", "seecoder", "chat", "--workspace", workspace, "--event-json", "--save", storage, "--session-id", sessionID]
      if FileManager.default.fileExists(atPath: storage) { arguments.append(contentsOf: ["--resume", storage]) }
      return (uv, arguments)
    }
    let pythonCandidates = [root.appendingPathComponent(".venv/bin/python"), root.appendingPathComponent(".venv/bin/python3"), URL(fileURLWithPath: "/opt/homebrew/opt/python@3.12/bin/python3.12"), URL(fileURLWithPath: "/opt/homebrew/bin/python3"), URL(fileURLWithPath: "/usr/local/bin/python3")]
    if let python = pythonCandidates.first(where: { FileManager.default.isExecutableFile(atPath: $0.path) }) {
      var arguments = ["-m", "seecoder", "chat", "--workspace", workspace, "--event-json", "--save", storage, "--session-id", sessionID]
      if FileManager.default.fileExists(atPath: storage) { arguments.append(contentsOf: ["--resume", storage]) }
      return (python, arguments)
    }
    return nil
  }
  private func launchEnvironment(root: URL) -> [String: String] {
    var environment = ProcessInfo.processInfo.environment
    let extraPath = ["/opt/homebrew/bin", "/usr/local/bin", "\(FileManager.default.homeDirectoryForCurrentUser.path)/.local/bin", "\(FileManager.default.homeDirectoryForCurrentUser.path)/.cargo/bin"]
    let currentPath = environment["PATH"]?.split(separator: ":").map(String.init) ?? []
    var paths: [String] = []
    for path in extraPath + currentPath where !paths.contains(path) { paths.append(path) }
    environment["PATH"] = paths.joined(separator: ":")
    let sourcePath = root.appendingPathComponent("src").path
    environment["PYTHONPATH"] = [sourcePath, environment["PYTHONPATH"]].compactMap { $0 }.joined(separator: ":")
    environment["SEECODER_PROJECT_ROOT"] = root.path
    return environment
  }
  private func projectRootURL() -> URL {
    let configured = ProcessInfo.processInfo.environment["SEECODER_PROJECT_ROOT"].map { URL(fileURLWithPath: $0, isDirectory: true) }
    let candidates = [configured, Bundle.main.bundleURL.deletingLastPathComponent(), URL(fileURLWithPath: #filePath).deletingLastPathComponent(), URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true)].compactMap { $0 }
    for start in candidates {
      var candidate = start.standardizedFileURL
      for _ in 0..<12 {
        if FileManager.default.fileExists(atPath: candidate.appendingPathComponent("pyproject.toml").path) { return candidate }
        let parent = candidate.deletingLastPathComponent(); if parent.path == candidate.path { break }; candidate = parent
      }
    }
    return URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true)
  }
  private func classifyDiff(_ text: String) -> DiffLine { let kind: DiffLine.Kind = text.hasPrefix("@@") ? .hunk : text.hasPrefix("+++ ") || text.hasPrefix("--- ") ? .file : text.hasPrefix("+") ? .add : text.hasPrefix("-") ? .remove : text.hasPrefix("diff ") || text.hasPrefix("index ") ? .meta : .context; return DiffLine(kind: kind, text: text) }
  private func shortPath(_ path: String) -> String { URL(fileURLWithPath: path).lastPathComponent }
  private func sessionStorageURL(id: String) -> URL { let root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("SEECODER/sessions", isDirectory: true); try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true); return root.appendingPathComponent(id + ".json") }
  private var persistenceURL: URL {
    let root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("SEECODER", isDirectory: true)
    try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    return root.appendingPathComponent("desktop-sessions.json")
  }
  private func save() {
    let snapshot = DesktopPersistence(sessions: sessions, selectedID: selectedID)
    if let data = try? JSONEncoder().encode(snapshot) { try? data.write(to: persistenceURL, options: .atomic) }
    saveProjectFlags()
    saveSessionFlags()
    UserDefaults.standard.set(mode, forKey: modeKey)
  }
  private func loadProjectFlags() {
    if let paths = UserDefaults.standard.array(forKey: pinnedProjectsKey) as? [String] { pinnedProjects = Set(paths) }
    if let paths = UserDefaults.standard.array(forKey: archivedProjectsKey) as? [String] { archivedProjects = Set(paths) }
  }
  private func saveProjectFlags() {
    UserDefaults.standard.set(Array(pinnedProjects), forKey: pinnedProjectsKey)
    UserDefaults.standard.set(Array(archivedProjects), forKey: archivedProjectsKey)
  }
  private func loadSessionFlags() {
    let values = UserDefaults.standard.array(forKey: archivedSessionsKey) as? [String] ?? []
    archivedSessions = Set(values.compactMap(UUID.init(uuidString:)))
  }
  private func saveSessionFlags() {
    UserDefaults.standard.set(archivedSessions.map(\.uuidString), forKey: archivedSessionsKey)
  }
  private func cleanupEmptyPlaceholderSessions() {
    let removed = sessions.filter { $0.workspace.isEmpty && $0.messages.isEmpty && $0.title == "新对话" }.map(\.id)
    guard !removed.isEmpty else { return }
    sessions.removeAll { removed.contains($0.id) }
    archivedSessions.subtract(removed)
    if let selectedID, removed.contains(selectedID) { self.selectedID = sessions.first?.id }
    activity.insert("已清理 \(removed.count) 个空白占位会话", at: 0)
  }
  private func load() {
    if let data = try? Data(contentsOf: persistenceURL), let stored = try? JSONDecoder().decode(DesktopPersistence.self, from: data) { sessions = stored.sessions; selectedID = stored.selectedID; return }
    if let data = UserDefaults.standard.data(forKey: legacyPersistenceKey), let stored = try? JSONDecoder().decode([SessionModel].self, from: data) { sessions = stored }
  }
}

struct DesktopRoot: View {
  @EnvironmentObject var store: DesktopStore
  var body: some View {
    HSplitView {
      // HSplitView owns the horizontal proposal. Avoid passing an unbounded
      // proposal back into the split children, which creates a recursive
      // sizeThatFits graph on recent macOS releases.
      Sidebar().frame(minWidth: 208, idealWidth: 238, maxWidth: 292)
      Conversation().frame(minWidth: 440, idealWidth: 720)
      Inspector().frame(minWidth: 250, idealWidth: 284, maxWidth: 390)
    }
    .frame(minWidth: 1000, minHeight: 640)
    .background(Color.canvas)
    .environment(\.colorScheme, .light)
    .preferredColorScheme(.light)
    .tint(Color.brandBlue)
    .sheet(isPresented: $store.showNewConversation) { NewConversationSheet() }
    .sheet(isPresented: $store.showCreateWorkspace) { CreateWorkspaceSheet() }
    .sheet(isPresented: $store.showProjectSettings) { ProjectSettingsSheet() }
    .sheet(item: $store.renameKind) { kind in RenameSheet(kind: kind) }
  }
}

struct BrandMark: View {
  private let logo: NSImage?

  init() {
    if let url = Bundle.module.url(forResource: "seecoder-logo", withExtension: "png") {
      logo = NSImage(contentsOf: url)
    } else {
      logo = nil
    }
  }

  var body: some View {
    ZStack(alignment: .top) {
      RoundedRectangle(cornerRadius: 9)
        .fill(Color.white.opacity(0.78))
      if let logo {
        Image(nsImage: logo)
          .resizable()
          .renderingMode(.original)
          .scaledToFit()
          .frame(width: 29, height: 29)
          .padding(2)
      } else {
        Image(systemName: "chevron.left.forwardslash.chevron.right")
          .foregroundStyle(Color.brandBlue)
      }
    }
    .frame(width: 34, height: 34)
    .clipShape(RoundedRectangle(cornerRadius: 9))
    .shadow(color: Color.brandBlue.opacity(0.16), radius: 5, y: 2)
    .accessibilityLabel("SEECODER")
  }
}

struct Sidebar: View {
  @EnvironmentObject var store: DesktopStore
  var body: some View {
    VStack(alignment: .leading, spacing: 10) {
      HStack { BrandMark(); Spacer(); Button { store.openNewConversation() } label: { Image(systemName: "plus") }.buttonStyle(.plain).foregroundStyle(Color.muted) }.padding(.bottom, 12)
      Button(action: store.openNewConversation) { Label("新对话", systemImage: "square.and.pencil") }.buttonStyle(SidebarAction())
      Button(action: store.openProject) { Label("打开项目", systemImage: "folder") }.buttonStyle(SidebarAction())
      Button(action: store.openCreateWorkspace) { Label("新建项目", systemImage: "folder.badge.plus") }.buttonStyle(SidebarAction())
      HStack { Text("项目").font(.caption.weight(.semibold)).foregroundStyle(Color.muted); Spacer(); Button { store.openCreateWorkspace() } label: { Image(systemName: "plus") }.buttonStyle(.plain).foregroundStyle(Color.muted) }.padding(.top, 16)
      ScrollView {
        LazyVStack(alignment: .leading, spacing: 6) {
          ForEach(store.projectGroups) { project in
            ProjectSection(project: project)
          }
        }
      }
      .scrollIndicators(.hidden)
      Spacer(minLength: 12)
      Divider()
      Label("本地优先", systemImage: "checkmark.circle.fill").font(.caption.weight(.semibold)).foregroundStyle(Color.brandGreen)
      Text("项目、会话和消息仅保存在此设备\n不会保存 API key").font(.caption2).foregroundStyle(Color.muted)
    }.padding(16).background(Color.sidebar)
  }
}

private struct ProjectSection: View {
  @EnvironmentObject var store: DesktopStore
  let project: ProjectGroup
  @State private var isHovered = false

  var body: some View {
    VStack(alignment: .leading, spacing: 3) {
      HStack(spacing: 7) {
        Button {
          if let session = project.sessions.first { store.select(session) }
        } label: {
          HStack(spacing: 7) {
            Image(systemName: project.isUnassigned ? "tray" : project.isArchived ? "archivebox" : "folder")
            Text(project.name).lineLimit(1)
          }
          .font(.system(size: 14, weight: .semibold))
          .foregroundStyle(project.isArchived ? Color.muted : Color.ink)
          .frame(maxWidth: .infinity, alignment: .leading)
        }
        .buttonStyle(.plain)
        Spacer()
        if !project.isUnassigned {
          Button { store.startSession(in: project.workspace) } label: { Image(systemName: "plus") }.buttonStyle(.plain).foregroundStyle(Color.muted)
          Menu {
            Button("新对话") { store.startSession(in: project.workspace) }
            Divider()
            Button(project.isPinned ? "取消置顶项目" : "置顶项目") { store.togglePinnedProject(project) }
            Button("在 Finder 中显示") { store.showProjectInFinder(project) }
            Button("项目设置") { store.openProjectSettings(project) }
            Divider()
            Button(project.isArchived ? "取消归档项目" : "归档项目") { store.toggleArchivedProject(project) }
            Button("从项目列表移除") { store.detachProject(project) }
          } label: {
            Image(systemName: "ellipsis").frame(width: 20, height: 26)
          }
          .menuStyle(.borderlessButton)
          .foregroundStyle(Color.muted)
        }
      }
      .padding(.horizontal, 7)
      .padding(.vertical, 5)
      .background(isHovered ? Color.brandBlue.opacity(0.06) : .clear, in: RoundedRectangle(cornerRadius: 7))
      .onHover { isHovered = $0 }
      ForEach(project.sessions) { session in
        HStack(spacing: 3) {
          Button { store.select(session) } label: {
            HStack(spacing: 7) {
              Circle().strokeBorder(store.selectedID == session.id ? Color.brandBlue : Color.muted.opacity(0.55), lineWidth: 1).frame(width: 7, height: 7)
              Text(session.title).lineLimit(1).font(.system(size: 13, weight: store.selectedID == session.id ? .semibold : .regular)).foregroundStyle(store.isSessionArchived(session) ? Color.muted : Color.ink)
              if store.isSessionArchived(session) { Image(systemName: "archivebox").font(.caption2).foregroundStyle(Color.muted) }
            }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 7).padding(.horizontal, 10).background(store.selectedID == session.id ? Color.brandBlue.opacity(0.12) : .clear, in: RoundedRectangle(cornerRadius: 7))
          }.buttonStyle(.plain)
          Menu {
            Button("重命名会话") { store.openRenameSession(session) }
            Button(store.isSessionArchived(session) ? "取消归档会话" : "归档会话") { store.toggleArchivedSession(session) }
          } label: {
            Image(systemName: "ellipsis").frame(width: 18, height: 26)
          }
          .menuStyle(.borderlessButton)
          .foregroundStyle(Color.muted)
        }.padding(.leading, 10)
      }
    }
  }
}

struct Conversation: View {
  @EnvironmentObject var store: DesktopStore
  var body: some View { VStack(spacing: 0) { HStack { VStack(alignment: .leading, spacing: 3) { Text(store.current?.title ?? "新对话").font(.headline).foregroundStyle(Color.ink); Text(store.current?.workspace.isEmpty == false ? store.current!.workspace : "尚未选择本地开发区域").font(.caption).foregroundStyle(Color.muted).lineLimit(1) }; Spacer(); Menu { Button("选择其他工作区", action: store.chooseWorkspace); Button("重命名当前工作区", action: store.openRenameCurrentWorkspace).disabled(!store.hasWorkspace || store.isRunning) } label: { Label("工作区", systemImage: "folder") }.menuStyle(.borderlessButton); Button("选择工作区", action: store.chooseWorkspace).buttonStyle(.bordered) }.padding(.horizontal, 22).frame(height: 60); Divider(); ScrollViewReader { proxy in ScrollView { Group { if store.current?.messages.isEmpty != false { Onboarding() } else { LazyVStack(alignment: .leading, spacing: 18) { ForEach(store.current?.messages ?? []) { message in MessageBubble(message: message) }; ForEach(store.workUpdates) { update in WorkUpdateCard(update: update) }; if let plan = store.current?.taskPlan { PlanSummary(plan: plan) }; if store.pendingApproval != nil { ApprovalCard() }; ChangeSummary() } } }.frame(maxWidth: 720, alignment: .leading).padding(.horizontal, 28).padding(.vertical, 26).frame(maxWidth: .infinity, alignment: .center) }.onChange(of: store.pendingApproval?.id) { _, id in if id != nil { proxy.scrollTo("approval-card", anchor: .bottom) } }.onChange(of: store.current?.messages.count) { _, _ in if let id = store.current?.messages.last?.id { proxy.scrollTo(id, anchor: .bottom) } }.onChange(of: store.workUpdates.count) { _, _ in if let id = store.workUpdates.last?.id { proxy.scrollTo(id, anchor: .bottom) } } }; Composer() }.background(Color.canvas) }
}

struct Onboarding: View { @EnvironmentObject var store: DesktopStore; var body: some View { VStack(spacing: 12) { Spacer(minLength: 48); Image("seecoder-logo", bundle: .module).resizable().frame(width: 50, height: 50).clipShape(RoundedRectangle(cornerRadius: 15)); Text("开始一个新项目").font(.system(size: 27, weight: .bold)).foregroundStyle(Color.ink); Text("选择一个本地文件夹作为项目，或创建名为“新项目”的文件夹。\n项目下可以继续创建多个独立会话。").font(.system(size: 14)).multilineTextAlignment(.center).foregroundStyle(Color.muted); HStack { Button("选择本地项目", action: store.chooseWorkspace).buttonStyle(.borderedProminent); Button("新建项目", action: store.openCreateWorkspace).buttonStyle(.bordered) }; Spacer(minLength: 28) }.frame(maxWidth: .infinity, minHeight: 260) } }
struct MessageBubble: View { let message: ChatMessage; private var shouldCollapse: Bool { message.role == .agent && message.content.count > 1_600 }; var body: some View { VStack(alignment: .leading, spacing: 7) { Label(message.role == .user ? "你" : message.role == .agent ? "SEECODER" : "本地状态", systemImage: message.role == .user ? "person.fill" : "sparkle").font(.caption.weight(.semibold)).foregroundStyle(message.role == .user ? Color.brandBlue : Color.brandGreen); Group { if message.role == .agent && shouldCollapse { DisclosureGroup("详细说明（已折叠）") { MarkdownMessage(source: message.content).padding(.top, 8) }.font(.system(size: 14)).foregroundStyle(Color.ink) } else if message.role == .agent { MarkdownMessage(source: message.content) } else { Text(message.content).textSelection(.enabled).font(.system(size: 14)).lineSpacing(5).foregroundStyle(Color.ink) } }.padding(15).frame(maxWidth: 760, alignment: .leading).background(message.role == .user ? Color.brandCyan.opacity(0.14) : Color.white, in: RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.line, lineWidth: 1)) }.id(message.id) } }
struct WorkUpdateCard: View {
  let update: WorkUpdate
  var body: some View {
    VStack(alignment: .leading, spacing: 6) {
      HStack(spacing: 8) {
        Image(systemName: update.isComplete ? "checkmark.circle.fill" : "arrow.triangle.2.circlepath")
          .foregroundStyle(update.isComplete ? Color.brandGreen : Color.brandBlue)
        Text(update.isComplete ? (update.title.hasPrefix("正在处理：") ? update.title.replacingOccurrences(of: "正在处理：", with: "已完成：") : "已完成本批工作") : update.title)
          .font(.system(size: 14, weight: .semibold)).foregroundStyle(Color.ink)
      }
      if !update.detail.isEmpty { Text(update.detail).font(.system(size: 13)).foregroundStyle(Color.muted).lineLimit(2) }
      if !update.note.isEmpty {
        DisclosureGroup("补充说明（已折叠）") {
          Text(update.note).font(.system(size: 12)).lineSpacing(3).foregroundStyle(Color.muted).textSelection(.enabled).padding(.top, 5)
        }
        .font(.system(size: 12, weight: .medium)).foregroundStyle(Color.muted)
      }
    }
    .padding(13).frame(maxWidth: 760, alignment: .leading)
    .background(Color.white, in: RoundedRectangle(cornerRadius: 12))
    .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.line, lineWidth: 1))
    .id(update.id)
  }
}
struct MarkdownMessage: View {
  private enum Block { case prose([String]), code(language: String, body: String) }
  let source: String
  private var blocks: [Block] {
    var result: [Block] = []; var prose: [String] = []; var code: [String] = []; var language = ""; var inCode = false
    func flushProse() { if !prose.isEmpty { result.append(.prose(prose)); prose = [] } }
    for raw in source.split(separator: "\n", omittingEmptySubsequences: false) {
      let line = String(raw)
      if line.hasPrefix("```") {
        if inCode { result.append(.code(language: language, body: code.joined(separator: "\n"))); code = []; language = ""; inCode = false }
        else { flushProse(); language = String(line.dropFirst(3)).trimmingCharacters(in: .whitespaces); inCode = true }
      } else if inCode { code.append(line) } else { prose.append(line) }
    }
    if inCode { result.append(.code(language: language, body: code.joined(separator: "\n"))) } else { flushProse() }
    return result
  }
  var body: some View {
    VStack(alignment: .leading, spacing: 10) {
      ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
        switch block {
        case .prose(let lines): VStack(alignment: .leading, spacing: 5) { ForEach(Array(lines.enumerated()), id: \.offset) { _, line in markdownLine(line) } }
        case .code(let language, let body): VStack(alignment: .leading, spacing: 7) { if !language.isEmpty { Text(language.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(Color.muted) }; ScrollView(.horizontal, showsIndicators: false) { Text(body.isEmpty ? " " : body).textSelection(.enabled).font(.system(size: 12, design: .monospaced)).foregroundStyle(Color.ink).frame(maxWidth: .infinity, alignment: .leading) } }.padding(11).background(Color.codeSurface, in: RoundedRectangle(cornerRadius: 9))
        }
      }
    }
  }
  @ViewBuilder private func markdownLine(_ line: String) -> some View {
    let trimmed = line.trimmingCharacters(in: .whitespaces)
    if trimmed.isEmpty { Spacer().frame(height: 4) }
    else if trimmed.hasPrefix("### ") { Text(attributed(String(trimmed.dropFirst(4)))).font(.headline).foregroundStyle(Color.ink) }
    else if trimmed.hasPrefix("## ") { Text(attributed(String(trimmed.dropFirst(3)))).font(.title3.weight(.bold)).foregroundStyle(Color.ink) }
    else if trimmed.hasPrefix("# ") { Text(attributed(String(trimmed.dropFirst(2)))).font(.title2.weight(.bold)).foregroundStyle(Color.ink) }
    else if let numbered = orderedListParts(trimmed) {
      HStack(alignment: .firstTextBaseline, spacing: 7) { Text("\(numbered.0).").foregroundStyle(Color.brandBlue).fontWeight(.semibold); Text(attributed(numbered.1)).foregroundStyle(Color.ink) }
    }
    else if trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") || trimmed.hasPrefix("+ ") { HStack(alignment: .firstTextBaseline, spacing: 7) { Text("•").foregroundStyle(Color.brandBlue); Text(attributed(String(trimmed.dropFirst(2)))).foregroundStyle(Color.ink) } }
    else { Text(attributed(line)).foregroundStyle(Color.ink) }
  }
  private func orderedListParts(_ line: String) -> (Int, String)? {
    guard let dot = line.firstIndex(of: "."), dot > line.startIndex,
          let number = Int(line[..<dot]) else { return nil }
    let contentStart = line.index(after: dot)
    guard contentStart < line.endIndex, line[contentStart] == " " else { return nil }
    let content = line[line.index(after: contentStart)...].trimmingCharacters(in: .whitespaces)
    return content.isEmpty ? nil : (number, String(content))
  }
  private func attributed(_ text: String) -> AttributedString { (try? AttributedString(markdown: text)) ?? AttributedString(text) }
}
struct ApprovalCard: View {
  @EnvironmentObject var store: DesktopStore
  var body: some View {
    if let approval = store.pendingApproval {
      VStack(alignment: .leading, spacing: 10) {
        Label(approval.kind == .plan ? "批准执行计划" : "批准本地操作", systemImage: "hand.raised.fill").font(.headline).foregroundStyle(Color.ink)
        Text(approval.detail).font(.subheadline).foregroundStyle(Color.muted)
        HStack { Button("拒绝") { store.decideApproval(false) }.buttonStyle(.bordered); Button(approval.kind == .plan ? "批准并执行" : "批准执行") { store.decideApproval(true) }.buttonStyle(.borderedProminent); Spacer() }
      }
      .padding(14)
      .background(Color.brandAmber.opacity(0.10), in: RoundedRectangle(cornerRadius: 12))
      .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.brandAmber.opacity(0.55), lineWidth: 1))
      .id("approval-card")
    }
  }
}
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
struct ChangeSummary: View { @EnvironmentObject var store: DesktopStore; var body: some View { let files = store.changedFiles(); if !files.isEmpty { let added = files.reduce(0) { $0 + $1.1 }; let deleted = files.reduce(0) { $0 + $1.2 }; VStack(alignment: .leading, spacing: 8) { HStack { Text("已编辑 \(files.count) 个文件").font(.headline); Spacer(); Text("+\(added) −\(deleted)").font(.caption).foregroundStyle(.secondary) }; if (store.current?.localChanges.count ?? 0) > 0 { Text("本轮 Agent 编辑记录（工作区未检测到 Git 基线时仍可用）").font(.caption2).foregroundStyle(Color.muted) }; ForEach(files, id: \.0) { file in Button { store.inspectDiff(file.0) } label: { HStack { Text(file.0).font(.system(.caption, design: .monospaced)); Spacer(); Text("+\(file.1) −\(file.2)").font(.caption).foregroundStyle(.secondary) }.padding(.vertical, 5) }.buttonStyle(.plain) } }.padding(15).frame(maxWidth: 760).background(Color.white, in: RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.line, lineWidth: 1)) } } }
struct PlanSummary: View { let plan: TaskPlanModel; var body: some View { VStack(alignment: .leading, spacing: 9) { HStack { Label("任务计划", systemImage: "list.bullet.rectangle").font(.headline); Spacer(); Text(plan.status).font(.caption).foregroundStyle(.secondary) }; ForEach(plan.items, id: \.id) { item in HStack(alignment: .top, spacing: 8) { Image(systemName: item.status == "completed" ? "checkmark.circle.fill" : item.status == "failed" ? "xmark.octagon.fill" : "circle").foregroundStyle(item.status == "completed" ? Color.brandGreen : item.status == "failed" ? Color.red : Color.brandBlue); VStack(alignment: .leading, spacing: 2) { Text(item.description).font(.system(size: 13, weight: .semibold)); Text(item.tool + (item.evidence.isEmpty ? "" : " · " + item.evidence)).font(.caption).foregroundStyle(Color.muted) } } } }.padding(15).frame(maxWidth: 760).background(Color.white, in: RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.line, lineWidth: 1)) } }
struct Composer: View {
    @EnvironmentObject var store: DesktopStore
    @FocusState private var isFocused: Bool

    var body: some View {
        VStack(spacing: 8) {
            HStack(alignment: .bottom, spacing: 10) {
                ZStack(alignment: .topLeading) {
                    TextEditor(text: $store.draft)
                        .font(.system(size: 14))
                        .foregroundStyle(Color.ink)
                        .scrollContentBackground(.hidden)
                        .background(Color.white)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 6)
                        .focused($isFocused)
                        .disabled(store.isRunning)

                    if store.draft.isEmpty && !isFocused {
                        Text(store.hasWorkspace ? "描述一个真实的编程任务…" : "可先描述任务，选择工作区后即可发送…")
                            .font(.system(size: 14))
                            .foregroundStyle(Color.muted)
                            .padding(.top, 14)
                            .padding(.leading, 12)
                            .allowsHitTesting(false)
                    }
                }
                .frame(height: 68)

                Button(action: store.send) { Image(systemName: "arrow.up") }
                    .buttonStyle(.borderedProminent)
                    .disabled(!store.hasWorkspace || store.isRunning)
            }

            HStack {
                Picker("模式", selection: $store.mode) {
                    Text("Ask").tag("ask")
                    Text("Plan").tag("plan")
                    Text("Auto Mode").tag("auto")
                }
                .labelsHidden()
                .frame(width: 112)
                .onChange(of: store.mode) { _, _ in store.persistMode() }
                Text(store.hasWorkspace ? "本地 · 受限执行" : "选择工作区后发送")
                    .font(.caption)
                    .foregroundStyle(Color.muted)
                Spacer()
                if store.isRunning { Button("停止", action: store.stop).buttonStyle(.bordered) }
            }
        }
        .padding(11)
        .frame(maxWidth: 720)
        .background(.white, in: RoundedRectangle(cornerRadius: 13))
        .overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.line, lineWidth: 1))
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 24)
        .padding(.bottom, 12)
        .onAppear { if store.hasWorkspace && !store.isRunning { isFocused = true } }
        .onChange(of: store.isRunning) { _, running in if !running && store.hasWorkspace { isFocused = true } }
    }
}

private enum InspectorPage: String, CaseIterable, Identifiable {
    case status, tools, skills
    var id: String { rawValue }
    var label: String {
        switch self {
        case .status: "运行"
        case .tools: "工具 / MCP"
        case .skills: "Skills"
        }
    }
}

struct Inspector: View {
    @EnvironmentObject var store: DesktopStore
    @State private var page: InspectorPage = .status

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text(store.reviewFile == nil ? page.label : "审阅变更")
                    .font(.title3.bold())
                Spacer()
                if store.reviewFile != nil {
                    Button { store.reviewFile = nil } label: { Image(systemName: "xmark") }
                        .buttonStyle(.plain)
                        .foregroundStyle(Color.muted)
                }
            }

            if let path = store.current?.workspace, !path.isEmpty {
                Label(URL(fileURLWithPath: path).lastPathComponent, systemImage: "folder")
                    .font(.caption)
                    .foregroundStyle(Color.muted)
            }

            if let file = store.reviewFile {
                Text(file).font(.caption.monospaced()).foregroundStyle(Color.muted)
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(store.diffLines) { line in
                            Text(line.text.isEmpty ? " " : line.text)
                                .font(.system(.caption, design: .monospaced))
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.horizontal, 8)
                                .padding(.vertical, 2)
                                .background(diffColor(line.kind))
                        }
                    }
                }
                .background(.white, in: RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.line, lineWidth: 1))
            } else {
                Picker("检查器页面", selection: $page) {
                    ForEach(InspectorPage.allCases) { item in Text(item.label).tag(item) }
                }
                .pickerStyle(.segmented)

                switch page {
                case .status: StatusInspector()
                case .tools: ToolManagerPanel()
                case .skills: SkillsManagerPanel()
                }
            }
        }
        .padding(20)
        .frame(alignment: .topLeading)
        .background(Color.inspector)
    }
}

private struct StatusInspector: View {
    @EnvironmentObject var store: DesktopStore

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(store.isRunning ? "AgentRunner 正在本地执行。每一步都会记录在下方。" : "提交任务后，这里会显示模型请求、工具调用、执行结果与终止状态。")
                .font(.caption)
                .foregroundStyle(Color.muted)
                .lineSpacing(4)
            if store.pendingApproval != nil { ApprovalCard() }
            if store.timeline.isEmpty { Spacer() }
            else { ScrollView { ExecutionTimeline(compact: true) } }
        }
    }
}

private struct ToolManagerPanel: View {
    private let localTools = [
        ("list_files", "浏览工作区目录", "只读"),
        ("read_file", "按行读取本地文件", "只读"),
        ("search_files", "搜索文件内容", "只读"),
        ("find_files", "按 glob 查找文件", "只读"),
        ("project_overview", "概览项目结构", "只读"),
        ("search_code", "符号定义检索", "只读"),
        ("git_diff", "读取本地 Git 差异", "只读"),
        ("git_status", "查看分支与工作树", "只读"),
        ("git_log", "查看有限本地提交历史", "只读"),
        ("git_show", "查看单次提交摘要", "只读"),
        ("list_skills", "查看本地 Skill 包", "只读"),
        ("delete_file", "安全删除单个临时文件", "受策略控制"),
        ("create_directory", "创建工作区目录", "受策略控制"),
        ("copy_file", "复制工作区文件", "受策略控制"),
        ("move_file", "移动或重命名文件", "受策略控制"),
        ("rename_directory", "重命名工作区内代码目录", "受策略控制"),
        ("write_file", "在工作区内原子写入", "受策略控制"),
        ("apply_patch", "精确修改工作区文件", "受策略控制"),
        ("run_command", "受限 argv 命令执行", "受策略控制")
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                InspectorSection(title: "本地 ToolRegistry", caption: "所有工具由 SEECODER 本地定义、解析与执行。") {
                    ForEach(localTools, id: \.0) { tool in
                        ManagementRow(icon: "wrench.and.screwdriver", title: tool.0, detail: tool.1, badge: tool.2)
                    }
                }
                InspectorSection(title: "MCP 服务", caption: "当前未连接外部 MCP 服务。") {
                    ManagementRow(icon: "network", title: "MCP 未启用", detail: "考核版本不依赖托管工具或外部执行服务。", badge: "0 已连接")
                }
            }
        }
    }
}

private struct SkillsManagerPanel: View {
    private let skills = [
        ("上下文管理", "历史裁剪、长度预算与工具回合保留", "内置"),
        ("工具安全", "工作区边界、.env 隔离与命令白名单", "内置"),
        ("代码工作流", "浏览、修改、测试与 Git 差异追踪", "内置"),
        ("本地记忆", "会话持久化；不会保存 API key", "已启用")
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                InspectorSection(title: "本地 Skills", caption: "可在工作区 .seecoder/skills/<名称>/SKILL.md 安装本地 Markdown Skill；数量、大小和路径都会受限。") {
                    ForEach(skills, id: \.0) { skill in
                        ManagementRow(icon: "sparkles", title: skill.0, detail: skill.1, badge: skill.2)
                    }
                }
                InspectorSection(title: "扩展边界", caption: "模型只提出工具调用建议；Skill 不能新增工具权限。执行、审批、循环终止和错误处理始终在本地 AgentRunner。") {
                    ManagementRow(icon: "lock.shield", title: "合规模式", detail: "不使用 LangChain、LlamaIndex、OpenAI Agents SDK 或托管代码执行服务。", badge: "本地")
                }
            }
        }
    }
}

private struct InspectorSection<Content: View>: View {
    let title: String
    let caption: String
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(title).font(.headline)
            Text(caption).font(.caption).foregroundStyle(Color.muted).lineSpacing(3)
            VStack(spacing: 0, content: content)
                .background(.white, in: RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.line, lineWidth: 1))
        }
    }
}

private struct ManagementRow: View {
    let icon: String
    let title: String
    let detail: String
    let badge: String

    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: icon).foregroundStyle(Color.brandBlue).frame(width: 16)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.caption.bold()).lineLimit(1)
                Text(detail).font(.caption2).foregroundStyle(Color.muted).lineLimit(2)
            }
            Spacer(minLength: 4)
            Text(badge).font(.system(size: 10, weight: .semibold)).foregroundStyle(Color.brandGreen)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 9)
        .overlay(alignment: .bottom) { Divider().padding(.leading, 35) }
    }
}
  private func diffColor(_ kind: DiffLine.Kind) -> Color { switch kind { case .add: .green.opacity(0.12); case .remove: .red.opacity(0.10); case .file, .hunk: .blue.opacity(0.09); default: .clear } }
struct NewConversationSheet: View {
  @EnvironmentObject var store: DesktopStore

  var body: some View {
    VStack(alignment: .leading, spacing: 16) {
      HStack(spacing: 10) { Image(systemName: "square.and.pencil").foregroundStyle(Color.brandBlue); Text("新对话").font(.title2.bold()).foregroundStyle(Color.ink) }
      Text("先选择一个本地项目文件夹。新对话会归入该项目，项目下可以继续创建多个会话。")
        .font(.subheadline).foregroundStyle(Color.muted).lineSpacing(3)
      VStack(alignment: .leading, spacing: 8) {
        Button { store.chooseWorkspaceForNewConversation() } label: {
          Label("选择已有项目文件夹", systemImage: "folder").frame(maxWidth: .infinity, alignment: .leading)
        }.buttonStyle(.borderedProminent)
        Button(action: store.openNewProject) {
          Label("新建项目（默认名称：新项目）", systemImage: "folder.badge.plus").frame(maxWidth: .infinity, alignment: .leading)
        }.buttonStyle(.bordered)
      }
      HStack { Spacer(); Button("取消") { store.showNewConversation = false } }
    }
    .padding(26)
    .frame(width: 480)
  }
}

struct ProjectSettingsSheet: View {
  @EnvironmentObject var store: DesktopStore

  private var project: ProjectGroup? {
    store.projectGroups.first { $0.workspace == store.projectSettingsWorkspace }
  }

  var body: some View {
    VStack(alignment: .leading, spacing: 16) {
      HStack(spacing: 10) {
        Image(systemName: "folder").foregroundStyle(Color.brandBlue)
        Text("项目设置").font(.title2.bold()).foregroundStyle(Color.ink)
      }
      if let project {
        VStack(alignment: .leading, spacing: 10) {
          Text(project.name).font(.headline).foregroundStyle(Color.ink)
          Label(project.workspace, systemImage: "externaldrive").font(.caption.monospaced()).foregroundStyle(Color.muted).lineLimit(2)
          Divider()
          Label("\(project.sessions.count) 个会话", systemImage: "bubble.left.and.bubble.right").font(.subheadline).foregroundStyle(Color.muted)
          Label(project.isPinned ? "已置顶" : "未置顶", systemImage: project.isPinned ? "pin.fill" : "pin").font(.subheadline).foregroundStyle(Color.muted)
          Label(project.isArchived ? "已归档" : "正常使用中", systemImage: project.isArchived ? "archivebox" : "checkmark.circle").font(.subheadline).foregroundStyle(project.isArchived ? Color.muted : Color.brandGreen)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.canvas, in: RoundedRectangle(cornerRadius: 10))
        HStack(spacing: 9) {
          Button("在 Finder 中显示") { store.showProjectInFinder(project) }
          Button(project.isPinned ? "取消置顶" : "置顶项目") { store.togglePinnedProject(project) }
          Button("新建会话") { store.showProjectSettings = false; store.startSession(in: project.workspace) }
        }
        Button("从项目列表移除（不删除本地文件）") { store.showProjectSettings = false; store.detachProject(project) }
          .font(.caption)
          .foregroundStyle(Color.muted)
      } else {
        Text("项目已不存在或已从会话列表移除。").foregroundStyle(Color.muted)
      }
      HStack { Spacer(); Button("完成") { store.showProjectSettings = false }.buttonStyle(.borderedProminent) }
    }
    .padding(26)
    .frame(width: 520)
  }
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
extension Color { static let canvas = Color(red: 0.985, green: 0.982, blue: 0.972); static let sidebar = Color(red: 0.955, green: 0.978, blue: 0.984); static let inspector = Color(red: 0.978, green: 0.976, blue: 0.958); static let codeSurface = Color(red: 0.94, green: 0.97, blue: 0.98); static let line = Color(red: 0.83, green: 0.89, blue: 0.91); static let ink = Color(red: 0.10, green: 0.18, blue: 0.23); static let muted = Color(red: 0.35, green: 0.45, blue: 0.51); static let brandBlue = Color(red: 0.10, green: 0.48, blue: 0.84); static let brandCyan = Color(red: 0.30, green: 0.72, blue: 0.92); static let brandGreen = Color(red: 0.14, green: 0.69, blue: 0.43); static let brandAmber = Color(red: 0.98, green: 0.63, blue: 0.12) }
