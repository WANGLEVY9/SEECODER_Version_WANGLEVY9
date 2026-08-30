#!/usr/bin/env python3
"""Original, local-only Tk desktop interface for the SEECODER CLI.

Run it through the bundled launcher after installing Homebrew Tk:
    ./desktop/run_desktop.sh

The desktop process never handles a model key. It launches the project's
Python-3.12 CLI in event-JSON mode, which continues to own the agent loop,
tool dispatch, workspace boundary, and all model communication.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext


APP_NAME = "SEECODER Desktop"
STATE_VERSION = 1
# Shared SEECODER brand palette: sky blue, deep blue, green, orange and navy.
# Keeping the fallback Tk client on the same tokens makes the project coherent
# even when Electron is unavailable on a demo machine.
BACKGROUND = "#f5fbfe"
SIDEBAR = "#eef7fb"
PANEL = "#ffffff"
PANEL_ACTIVE = "#e4f4fb"
TEXT = "#19344d"
MUTED = "#6c8291"
ACCENT = "#2e83d3"
SUCCESS = "#3eb779"
WARNING = "#ffad2e"
ERROR = "#bf5360"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_state_path() -> Path:
    return Path.home() / ".seecoder-desktop" / "sessions.json"


class SessionStore:
    """Small local-only session index. It intentionally stores no credentials."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
            return []
        sessions = payload.get("sessions")
        return sessions if isinstance(sessions, list) else []

    def save(self, sessions: List[Dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        payload = {"version": STATE_VERSION, "sessions": sessions}
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def create(self, workspace: Path) -> Dict[str, Any]:
        now = utc_now()
        return {
            "id": uuid.uuid4().hex,
            "title": "新对话",
            "workspace": str(workspace),
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }


def find_uv() -> str:
    configured = os.environ.get("SEECODER_UV")
    if configured:
        return configured
    discovered = shutil.which("uv")
    if not discovered:
        raise RuntimeError("未找到 uv。请安装 uv，或设置 SEECODER_UV 指向其可执行文件。")
    return discovered


def build_backend_command(uv: str, task: str, workspace: Path) -> List[str]:
    """Build the only backend invocation permitted from the desktop process."""

    if not task.strip():
        raise ValueError("任务不能为空")
    if not workspace.is_dir():
        raise ValueError("工作区必须是已存在的目录")
    return [
        uv,
        "run",
        "seecoder",
        "run",
        task.strip(),
        "--workspace",
        str(workspace.resolve()),
        "--event-json",
    ]


def parse_event_line(line: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    event = payload.get("event")
    data = payload.get("data")
    if not isinstance(event, str) or not isinstance(data, dict):
        return None
    return event, data


class DesktopApp:
    def __init__(self, root: tk.Tk, project_root: Path, state_path: Path) -> None:
        self.root = root
        self.project_root = project_root.resolve()
        self.store = SessionStore(state_path)
        self.sessions = self.store.load()
        self.current_id: Optional[str] = None
        self.process: Optional[subprocess.Popen[str]] = None
        self.stream_events: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        self.closed_streams: set[str] = set()
        self.finished_normally = False

        self.root.title(APP_NAME)
        self.root.geometry("1440x860")
        self.root.minsize(1100, 680)
        self.root.configure(background=BACKGROUND)
        self._build_layout()
        self._ensure_session()
        self._render_session_list()
        self._render_current_session()
        self._append_activity("已准备就绪：等待本地任务")
        self.root.after(60, self._poll_process_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Command-Return>", self._send_task)
        self.root.bind("<Control-Return>", self._send_task)

    def _build_layout(self) -> None:
        outer = tk.Frame(self.root, background=BACKGROUND)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        sidebar = tk.Frame(outer, background=SIDEBAR, width=260)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        self._build_sidebar(sidebar)

        main = tk.Frame(outer, background=BACKGROUND)
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        self._build_main(main)

        activity = tk.Frame(outer, background=PANEL, width=285)
        activity.grid(row=0, column=2, sticky="nsew")
        activity.grid_propagate(False)
        self._build_activity(activity)

    @staticmethod
    def _button(parent: tk.Misc, text: str, command: Any, *, accent: bool = False, state: str = "normal") -> tk.Button:
        background = ACCENT if accent else PANEL
        foreground = "#ffffff" if accent else TEXT
        return tk.Button(
            parent,
            text=text,
            command=command,
            state=state,
            background=background,
            foreground=foreground,
            activebackground="#514a7b" if accent else PANEL_ACTIVE,
            activeforeground=foreground,
            disabledforeground=MUTED,
            borderwidth=0,
            highlightthickness=0,
            relief="flat",
            padx=12,
            pady=8,
            font=("Helvetica Neue", 12),
        )

    def _build_sidebar(self, parent: tk.Frame) -> None:
        logo_path = self.project_root / "assets" / "seecoder-logo.png"
        try:
            self.logo_photo = tk.PhotoImage(file=str(logo_path))
            # Keep the complete mark visible without stretching the sidebar.
            scale = max(1, self.logo_photo.width() // 42)
            if scale > 1:
                self.logo_photo = self.logo_photo.subsample(scale, scale)
            tk.Label(parent, image=self.logo_photo, background=SIDEBAR).pack(anchor="w", padx=18, pady=(14, 4))
        except (tk.TclError, OSError):
            self.logo_photo = None
            tk.Label(parent, text="SEECODER", background=SIDEBAR, foreground=TEXT, font=("Helvetica Neue", 18, "bold")).pack(
                anchor="w", padx=18, pady=(20, 4)
            )
        tk.Label(parent, text="本地编程智能体 · Local Agent", background=SIDEBAR, foreground=MUTED, font=("Helvetica Neue", 11)).pack(
            anchor="w", padx=18, pady=(0, 20)
        )
        self._button(parent, "＋ 新对话", self._new_session).pack(
            fill="x", padx=14, pady=4
        )
        self._button(parent, "⌂ 选择工作区", self._choose_workspace).pack(
            fill="x", padx=14, pady=4
        )
        tk.Label(parent, text="最近会话", background=SIDEBAR, foreground=MUTED, font=("Helvetica Neue", 11)).pack(
            anchor="w", padx=18, pady=(25, 7)
        )
        self.session_list = tk.Listbox(
            parent,
            background=SIDEBAR,
            foreground=TEXT,
            selectbackground=PANEL_ACTIVE,
            selectforeground=TEXT,
            highlightthickness=0,
            borderwidth=0,
            activestyle="none",
            font=("Helvetica Neue", 12),
        )
        self.session_list.pack(fill="both", expand=True, padx=10, pady=(0, 12))
        self.session_list.bind("<<ListboxSelect>>", self._select_session)
        tk.Label(
            parent,
            text="仅本地保存\n不保存 API key",
            background=SIDEBAR,
            foreground=MUTED,
            font=("Helvetica Neue", 10),
            justify="left",
        ).pack(anchor="w", padx=18, pady=(0, 18))

    def _build_main(self, parent: tk.Frame) -> None:
        header = tk.Frame(parent, background=BACKGROUND)
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(18, 10))
        header.columnconfigure(0, weight=1)
        self.title_label = tk.Label(header, text="新对话", background=BACKGROUND, foreground=TEXT, font=("Helvetica Neue", 16, "bold"))
        self.title_label.grid(row=0, column=0, sticky="w")
        self.workspace_label = tk.Label(header, text="", background=BACKGROUND, foreground=MUTED, font=("Helvetica Neue", 10))
        self.workspace_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self._button(header, "打开工作区", self._choose_workspace).grid(
            row=0, column=1, rowspan=2, sticky="e"
        )

        self.transcript = scrolledtext.ScrolledText(
            parent,
            background=BACKGROUND,
            foreground=TEXT,
            insertbackground=TEXT,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=28,
            pady=14,
            wrap="word",
            font=("Helvetica Neue", 13),
            state="disabled",
        )
        self.transcript.frame.configure(background=BACKGROUND, borderwidth=0, relief="flat")
        self.transcript.vbar.configure(width=8, borderwidth=0, highlightthickness=0, relief="flat")
        self.transcript.grid(row=1, column=0, sticky="nsew", padx=6)
        self.transcript.tag_configure("user_label", foreground=ACCENT, font=("Helvetica Neue", 11, "bold"))
        self.transcript.tag_configure("agent_label", foreground=SUCCESS, font=("Helvetica Neue", 11, "bold"))
        self.transcript.tag_configure("system_label", foreground=WARNING, font=("Helvetica Neue", 11, "bold"))
        self.transcript.tag_configure("body", foreground=TEXT, spacing3=18)
        self.transcript.tag_configure("empty_title", foreground=TEXT, font=("Helvetica Neue", 22, "bold"), justify="center", spacing1=80)
        self.transcript.tag_configure("empty_body", foreground=MUTED, font=("Helvetica Neue", 13), justify="center", spacing3=16)
        self.transcript.tag_configure("empty_hint", foreground=ACCENT, font=("Helvetica Neue", 12, "bold"), justify="center")

        composer = tk.Frame(parent, background=PANEL)
        composer.grid(row=2, column=0, sticky="ew", padx=24, pady=(8, 22))
        composer.columnconfigure(0, weight=1)
        self.composer = tk.Text(
            composer,
            height=4,
            background=PANEL,
            foreground=TEXT,
            insertbackground=TEXT,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            wrap="word",
            font=("Helvetica Neue", 13),
            padx=14,
            pady=12,
        )
        self.composer.grid(row=0, column=0, columnspan=2, sticky="ew", padx=2, pady=2)
        self.status_label = tk.Label(composer, text="本地 · 受限执行模式", background=PANEL, foreground=MUTED, font=("Helvetica Neue", 10))
        self.status_label.grid(row=1, column=0, sticky="w", padx=14, pady=(0, 10))
        self.send_button = self._button(composer, "发送", self._send_task, accent=True)
        self.send_button.grid(row=1, column=1, sticky="e", padx=10, pady=(0, 10))
        self.stop_button = self._button(composer, "停止", self._stop_run, state="disabled")
        self.stop_button.grid(row=1, column=2, sticky="e", padx=(0, 10), pady=(0, 10))

    def _build_activity(self, parent: tk.Frame) -> None:
        tk.Label(parent, text="运行状态", background=PANEL, foreground=TEXT, font=("Helvetica Neue", 14, "bold")).pack(
            anchor="w", padx=18, pady=(20, 5)
        )
        tk.Label(parent, text="模型仅提出工具意图。\n文件、命令与 Git 均在本地执行。", background=PANEL, foreground=MUTED, font=("Helvetica Neue", 10), justify="left").pack(
            anchor="w", padx=18, pady=(0, 16)
        )
        self.activity_list = tk.Listbox(
            parent,
            background=PANEL,
            foreground=TEXT,
            highlightthickness=0,
            borderwidth=0,
            activestyle="none",
            font=("Helvetica Neue", 11),
        )
        self.activity_list.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._button(parent, "设计与安全说明", self._show_about).pack(
            fill="x", padx=14, pady=(0, 18)
        )

    def _ensure_session(self) -> None:
        if self.sessions:
            self.current_id = self.sessions[0].get("id")
            return
        session = self.store.create(self.project_root / "demo_workspace")
        self.sessions = [session]
        self.current_id = session["id"]
        self._save()

    def _current(self) -> Dict[str, Any]:
        for session in self.sessions:
            if session.get("id") == self.current_id:
                return session
        raise RuntimeError("当前会话不存在")

    def _save(self) -> None:
        self.store.save(self.sessions)

    def _render_session_list(self) -> None:
        self.session_list.delete(0, tk.END)
        selected = 0
        for index, session in enumerate(self.sessions):
            self.session_list.insert(tk.END, session.get("title", "新对话"))
            if session.get("id") == self.current_id:
                selected = index
        self.session_list.selection_set(selected)

    def _render_current_session(self) -> None:
        session = self._current()
        self.title_label.configure(text=session.get("title", "新对话"))
        self.workspace_label.configure(text="工作区  ·  " + session.get("workspace", "未选择"))
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", tk.END)
        messages = session.get("messages", [])
        if not messages:
            self._render_empty_state()
        for message in messages:
            self._append_transcript(message.get("role", "system"), message.get("content", ""), persist=False)
        self.transcript.configure(state="disabled")

    def _render_empty_state(self) -> None:
        self.transcript.insert(tk.END, "SEECODER\n", "empty_title")
        self.transcript.insert(
            tk.END,
            "一个由本地工具驱动的编程智能体。\n选择工作区后，描述你希望完成的真实编码任务。\n\n",
            "empty_body",
        )
        self.transcript.insert(tk.END, "⌘ ↵ 发送任务", "empty_hint")

    def _append_transcript(self, role: str, content: str, persist: bool = True) -> None:
        labels = {"user": ("你", "user_label"), "assistant": ("SEECODER", "agent_label"), "system": ("本地状态", "system_label")}
        label, tag = labels.get(role, labels["system"])
        was_disabled = str(self.transcript.cget("state")) == "disabled"
        if was_disabled:
            self.transcript.configure(state="normal")
        self.transcript.insert(tk.END, label + "\n", tag)
        self.transcript.insert(tk.END, content.strip() + "\n\n", "body")
        self.transcript.see(tk.END)
        if was_disabled:
            self.transcript.configure(state="disabled")
        if persist:
            session = self._current()
            messages = session.setdefault("messages", [])
            messages.append({"role": role, "content": content, "at": utc_now()})
            session["updated_at"] = utc_now()
            if role == "user" and session.get("title") == "新对话":
                session["title"] = content.strip().replace("\n", " ")[:22] or "新对话"
                self._render_session_list()
                self.title_label.configure(text=session["title"])
            self._save()

    def _append_activity(self, content: str) -> None:
        self.activity_list.insert(tk.END, "• " + content)
        self.activity_list.yview_moveto(1.0)

    def _new_session(self) -> None:
        if self.process:
            messagebox.showwarning(APP_NAME, "当前任务仍在运行。请先停止或等待完成。")
            return
        session = self.store.create(Path(self._current().get("workspace", str(self.project_root / "demo_workspace"))))
        self.sessions.insert(0, session)
        self.current_id = session["id"]
        self._save()
        self._render_session_list()
        self._render_current_session()
        self.activity_list.delete(0, tk.END)

    def _select_session(self, _event: tk.Event[Any]) -> None:
        selection = self.session_list.curselection()
        if not selection or self.process:
            return
        self.current_id = self.sessions[selection[0]]["id"]
        self._render_current_session()
        self.activity_list.delete(0, tk.END)

    def _choose_workspace(self) -> None:
        if self.process:
            messagebox.showwarning(APP_NAME, "当前任务仍在运行，暂不能切换工作区。")
            return
        chosen = filedialog.askdirectory(initialdir=self._current().get("workspace", str(self.project_root)))
        if not chosen:
            return
        session = self._current()
        session["workspace"] = str(Path(chosen).resolve())
        session["updated_at"] = utc_now()
        self._save()
        self._render_current_session()

    def _send_task(self, _event: Optional[tk.Event[Any]] = None) -> str:
        if self.process:
            return "break"
        task = self.composer.get("1.0", tk.END).strip()
        workspace = Path(self._current().get("workspace", ""))
        try:
            command = build_backend_command(find_uv(), task, workspace)
        except (RuntimeError, ValueError) as error:
            messagebox.showerror(APP_NAME, str(error))
            return "break"
        if not self._current().get("messages"):
            self.transcript.configure(state="normal")
            self.transcript.delete("1.0", tk.END)
            self.transcript.configure(state="disabled")
        self._append_transcript("user", task)
        self.composer.delete("1.0", tk.END)
        self.activity_list.delete(0, tk.END)
        self._append_activity("已启动本地 AgentRunner（受限执行模式）")
        self.status_label.configure(text="运行中：等待模型响应…", foreground=ACCENT)
        self.send_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.closed_streams.clear()
        self.finished_normally = False
        try:
            self.process = subprocess.Popen(
                command,
                cwd=self.project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=os.name != "nt",
            )
        except OSError as error:
            self._append_transcript("system", "无法启动本地后端：" + str(error))
            self._set_ready()
            return "break"
        assert self.process.stdout is not None and self.process.stderr is not None
        threading.Thread(target=self._read_stream, args=("stdout", self.process.stdout), daemon=True).start()
        threading.Thread(target=self._read_stream, args=("stderr", self.process.stderr), daemon=True).start()
        return "break"

    def _read_stream(self, name: str, stream: Any) -> None:
        try:
            for line in stream:
                self.stream_events.put((name, line.rstrip("\n")))
        finally:
            stream.close()
            self.stream_events.put(("closed", name))

    def _poll_process_events(self) -> None:
        try:
            while True:
                source, line = self.stream_events.get_nowait()
                if source == "closed":
                    self.closed_streams.add(line)
                elif source == "stdout":
                    self._handle_backend_line(line)
                elif line.strip():
                    self._append_activity("后端诊断：" + line.strip()[:240])
        except queue.Empty:
            pass
        if self.process and {"stdout", "stderr"}.issubset(self.closed_streams):
            exit_code = self.process.poll()
            self.process = None
            if not self.finished_normally:
                self._append_transcript("system", "本地运行结束，退出码：{}".format(exit_code))
            self._set_ready()
        self.root.after(60, self._poll_process_events)

    def _handle_backend_line(self, line: str) -> None:
        parsed = parse_event_line(line)
        if parsed is None:
            if line.strip():
                self._append_activity(line.strip()[:240])
            return
        event, data = parsed
        if event == "model_request":
            self._append_activity("正在请求模型（第 {} 步）".format(data.get("step", "?")))
        elif event == "tool_dispatch":
            self._append_activity("模型请求调用 {} 个本地工具".format(data.get("count", "?")))
        elif event == "tool_result":
            status = "完成" if data.get("ok") else "失败：" + str(data.get("error", "unknown"))
            self._append_activity("{}：{}".format(data.get("name", "tool"), status))
        elif event == "configuration_error":
            self._append_transcript("system", "配置错误：" + str(data.get("message", "未知错误")))
        elif event == "run_outcome":
            self.finished_normally = True
            self._append_transcript("assistant", str(data.get("final_text", "模型未提供最终文本。")))
            self._append_activity("结束状态：{}（{} 步）".format(data.get("state", "unknown"), data.get("steps", "?")))
            trace_path = data.get("trace_path")
            if trace_path:
                self._append_activity("已写入本地脱敏 trace")

    def _stop_run(self) -> None:
        if not self.process:
            return
        self.status_label.configure(text="正在请求停止本地进程…", foreground=WARNING)
        self._append_activity("用户请求停止本地运行")
        try:
            if os.name != "nt":
                os.killpg(self.process.pid, signal.SIGTERM)
            else:
                self.process.terminate()
        except ProcessLookupError:
            pass

    def _set_ready(self) -> None:
        self.status_label.configure(text="本地 · 受限执行模式", foreground=MUTED)
        self.send_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

    def _show_about(self) -> None:
        messagebox.showinfo(
            "SEECODER Desktop 设计边界",
            "此界面只负责本地交互和会话索引。\n\n"
            "它通过 JSONL 启动本仓库的 CLI；Agent 循环、上下文、模型输出解析、工具定义、"
            "文件/命令执行、错误处理和停止条件仍由自研 Python 后端完成。\n\n"
            "默认仅启用受限 argv 命令模式。不会嵌入 Codex、现成 Agent、Agent 框架，"
            "或云端文件/代码执行服务。",
        )

    def _on_close(self) -> None:
        if self.process and not messagebox.askyesno(APP_NAME, "本地任务仍在运行，是否停止并关闭？"):
            return
        if self.process:
            self._stop_run()
        self.root.destroy()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Original local Tk desktop UI for SEECODER")
    parser.add_argument("--project-root", type=Path, default=default_project_root())
    parser.add_argument("--state-file", type=Path, default=default_state_path())
    arguments = parser.parse_args(argv)
    if not arguments.project_root.is_dir():
        print("Project root does not exist: {}".format(arguments.project_root), file=sys.stderr)
        return 2
    root = tk.Tk()
    DesktopApp(root, arguments.project_root, arguments.state_file)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        # Developer-mode terminal interruption should close the local window
        # cleanly rather than looking like an application failure.
        root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
