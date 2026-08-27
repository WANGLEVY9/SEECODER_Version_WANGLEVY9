# P2：原创本地桌面端计划

## 目标与考核边界

实现一个受现代 coding workspace 工作流启发、但不复制外部产品品牌或专有服务的原创桌面端：项目会话、工作区选择、对话、执行轨迹、任务输入和本地停止控制。

原始考核题只禁止封装现成 Agent 产品、使用 Agent 框架/SDK，以及调用托管代码执行或文件 API；它明确允许编程语言不限。因此 Electron 与原生 HTML/CSS/JavaScript 可以作为**展示层**使用。它们不拥有模型循环、工具或文件执行能力，也不连接任何外部执行服务。SEECODER 仍不会使用 LangChain、LlamaIndex、OpenAI Agents SDK、Claude Agent SDK、AutoGen 或 CrewAI。

## 架构

```text
Electron 主进程 + 原生 HTML/CSS/JS 渲染层
  ├─ 本地浏览器存储：会话索引（无密钥）
  ├─ context-isolated preload：仅暴露选择目录、开始/停止任务、事件订阅
  ├─ shell=false 的本机子进程：uv run seecoder run ... --event-json
  │    └─ 自研 CLI → AgentRunner → 本地 ToolRegistry → 模型原生 tool calling
  └─ JSONL 事件渲染、停止本机进程组
```

Electron 主进程仅将用户任务与已选目录作为参数数组交给 CLI。它不生成 tool schema、不解析模型 tool call、不执行模型建议的文件或命令，也不读取 `.env`。这些关键逻辑仍由 Python 后端自行实现，故 UI 升级不会改变考核要求的责任归属。

## 安全与可演示性

- BrowserWindow 使用 `contextIsolation: true`、`nodeIntegration: false` 与 `sandbox: true`；渲染页没有 Node 或文件系统权限。
- preload 只开放固定 IPC 方法；主进程用 `spawn(command, args, {shell:false})`，不拼接 shell 字符串。
- 默认仍不传递 `--host-shell`；CLI 默认保持 `restricted` argv 执行模式。
- UI 不处理、展示或持久化 API key；后端只从项目根目录下被忽略的 `.env` 读取。
- 会话仅在 Electron app 的本地浏览器存储中保存；运行 trace 仍由后端按既有规则写入被忽略目录。

## 运行与验证

```bash
node --version  # Node.js 22.12 或更高版本
cd desktop/electron && npm install
cd ../..
./desktop/run_desktop_electron.sh

cd desktop/electron && npm test
PYTHONPATH=src python3.12 -m unittest discover -s tests -v
```

旧 Tk 端保留在 `desktop/run_desktop.sh`，只作为无 Node 环境下的兼容入口；录制视频时应使用 Electron 桌面端。
