# P2：原创本地桌面端计划

## 目标与边界

实现一个受 Codex 类工作流启发的原创桌面端：本地会话栏、工作区选择、对话区、运行活动区、任务输入与停止控制。它不是 Codex 的界面封装、不会使用 Codex 服务，也不复刻其品牌、图标或专有功能。

考核允许普通模型厂商客户端和原生 tool calling，但禁止封装现成 Agent、Agent 框架/SDK、托管代码执行和 Files API。故 UI 只启动现有的自研 CLI；不会接入 LangChain、LlamaIndex、OpenAI Agents SDK、Claude Agent SDK、AutoGen、CrewAI、Electron/Tauri/Web 服务或远端代码/文件服务。

## 架构

```text
Tk 标准库桌面界面（系统 Python）
  ├─ 本地 JSON 会话索引（不存密钥）
  ├─ 子进程：uv run seecoder run ... --event-json
  │    └─ 自研 CLI → AgentRunner → 本地 ToolRegistry → DeepSeek 原生 tool calling
  └─ JSONL 事件渲染、停止本机子进程组
```

macOS Command Line Tools 附带的旧 Tk 在深色系统外观下会错误绘制普通文本，不能用于交付。桌面端因此要求一次性的本机运行时安装：`brew install python-tk@3.12`。启动脚本固定使用 `/opt/homebrew/opt/python@3.12/bin/python3.12` 与 Tk 9；该运行时不进入仓库、不加入 `pyproject.toml`，也不是 Agent/UI 框架。

GUI 始终经 `uv` 启动项目锁定的 CLI 后端。两者以本地 stdout JSONL 通信，不共享凭据。高对比浅色原创主题在当前 macOS 上经实际窗口截图验证可读。

## 验收项

- 不新增第三方运行依赖；`pyproject.toml` 仍仅包含普通模型厂商客户端。
- 默认不会传递 `--host-shell`；现有 CLI 默认保持 `restricted` argv 执行模式。
- GUI 不处理、展示或持久化 API key；后端仍从项目根目录的被忽略 `.env` 读取。
- GUI 可显示模型请求和本地工具状态；停止仅终止本机进程。
- 用 Homebrew Tk 9 Python 进行无窗口边界测试，用项目 Python 进行后端全量测试。
- 已完成真实 Tk 9 窗口启动及截图检查：三栏、标题、会话、输入区、状态区与按钮均可见；该检查未提交任务，因此没有发起模型请求或读取凭据。

## 运行

```bash
brew install python-tk@3.12  # 首次运行一次
./desktop/run_desktop.sh
```

首次启动默认使用 `demo_workspace`。选择其它工作区后，桌面端将其传递给现有 CLI 的 `--workspace` 参数，文件工具仍执行路径边界检查。

本机验证命令：

```bash
cd desktop && PYTHONPYCACHEPREFIX=/private/tmp/seecoder-desktop-pyc /opt/homebrew/opt/python@3.12/bin/python3.12 -m unittest test_desktop.py -v
cd .. && PYTHONPATH=src python3.12 -m unittest discover -s tests -v
```
