# SEECODER

SEECODER 是面向南京大学软件工程专业推免项目的本地 Coding Agent。它使用普通模型厂商客户端和模型原生 tool calling，但 Agent 主循环、上下文、工具 schema、参数解析、本地执行、审批、错误处理、停止条件和 trace 均由本仓库自行实现。

## 当前能力

- 单任务 `run` 与交互式多轮 `chat`，支持保存、恢复和继续会话。
- 三种工作模式：`auto` 自动执行；`plan` 只读检查并提出变更计划；`ask` 在修改文件或执行命令前请求批准。
- 13 个本地工具：`list_files`、`read_file`、`search_files`、`write_file`、`apply_patch`、`git_diff`、`git_status`、`git_log`、`run_command`、`search_code`、`spawn_agent`、`web_search`、`list_skills`。
- 文件工具具备工作区边界、真实路径解析、符号链接逃逸检查、大小限制和原子写入。
- `run_command` 默认使用受限 argv 白名单，不拼接 shell；`--host-shell` 是显式兼容选项，并且不等同于操作系统沙箱。
- 同一回合内的只读工具可并行执行；写入和命令操作保持串行以保护因果关系。
- `search_code` 提供语言无关的确定性符号索引；`spawn_agent` 是有界、禁止递归的本地子 Agent；`web_search` 是可失败降级的本地 urllib 工具。
- `git_status` 与 `git_log` 使用固定 Git 参数向量，只读展示本地分支、工作树和有限提交历史。可选项目 Skills 位于 `.seecoder/skills/<skill-name>/SKILL.md`，按数量、大小和路径边界加载为项目指引，不能扩大任何工具权限或覆盖安全策略；可用 `list_skills` 查看。
- 支持 DeepSeek thinking 模式的 `reasoning_content` 保留、流式 token、usage 统计、上下文压缩，以及项目级 `SEECODER.md` / `AGENTS.md` 记忆注入。
- 每次运行生成脱敏 JSONL trace；模型失败、协议错误、工具错误、上下文超限、步数超限和 Ctrl+C 都有明确结果状态。

## 考核边界

本项目不封装 Codex、Claude Code 或其他现成 Agent 产品，不使用 LangChain、LlamaIndex、OpenAI Agents SDK、Claude Agent SDK、AutoGen、CrewAI 等 Agent 框架/SDK，也不调用 Code Interpreter、Files API 等托管代码或文件执行服务。Electron 仅是原创展示层；它通过安全的本地 IPC 启动自研 Python CLI，Agent 逻辑仍在本仓库内。

## 安装与配置

Python 3.12+ 与 `uv`：

```bash
uv sync
cp .env.example .env
```

在 `.env` 中设置 `SEECODER_API_KEY`。示例默认配置为 DeepSeek V4 Flash：`https://api.deepseek.com`、`deepseek-v4-flash`。也可以通过 `SEECODER_BASE_URL` 和 `SEECODER_MODEL` 使用其他 OpenAI-compatible 网关。

密钥只从进程环境或未入库的 dotenv 文件读取。`.env`、运行 trace、录屏和本地虚拟环境均已加入 `.gitignore`；程序不会打印 API key 或请求头，也拒绝把凭据文件和 trace 目录放入可编辑工作区。

## CLI 使用

从仓库根目录运行，工具只能访问指定工作区：

```bash
uv run seecoder run "检查并修复标签规范化问题，然后运行测试并总结修改" \
  --workspace demo_workspace
```

交互式多轮会话：

```bash
uv run seecoder chat --workspace demo_workspace --mode ask \
  --save /private/tmp/seecoder-session.json
```

使用 `uv run seecoder --help`、`uv run seecoder run --help` 或 `uv run seecoder chat --help` 查看完整选项。`--event-json` 会输出供桌面端使用的本地 JSONL 事件协议；`--trace-dir` 应位于工作区之外。

## 原生 macOS 桌面端

推荐使用 SwiftUI 原生 macOS 桌面端，位于 `desktop/swiftui/`，不依赖 Electron、Node 或第三方 UI 组件库。首次新建会话时可选择已有本地文件夹，或选择父目录并创建一个新的空会话工作区；文件夹名会经过单目录组件校验，桌面端不会向其他位置写入文件。每个项目会话连接一个本地 `seecoder chat` 进程，后续任务会延续同一模型上下文、工具回合与审批状态；消息记录可独立滚动，输入框固定在底部。界面采用会话、工作区、审阅三栏：包含项目会话列表、工作区选择、模式切换、实时执行轨迹与只读本地 Git 差异面板。界面不读取、不显示、不持久化 API key。

首次运行需要 macOS 14+ 与 Swift 6：

```bash
./desktop/run_desktop_native.sh
```

SwiftUI 端不加载网页或远程 UI 服务；它将工作区与任务作为 `Process` 的字面参数调用本地 CLI。现有 Electron 端保留在 `desktop/electron/` 作为兼容实现，但不再是推荐入口；无 SwiftUI 环境时仍可使用 Tk 兼容端：`./desktop/run_desktop.sh`。

原生桌面端更新后请退出并重新运行启动脚本。Electron 兼容端的主进程不支持热更新，若仍使用它，更新 `desktop/electron/main.cjs`、`preload.cjs` 或本地 IPC 后也需要完全退出并重启。

## 验证

所有离线测试不需要真实 API key：

```bash
PYTHONPATH=src python3.12 -m unittest discover -s tests -v
cd desktop/electron && npm test
cd ../.. && PYTHONPATH=src python3.12 -m unittest discover -s desktop -v
```

当前回归基线为 Python 后端 65/65、SwiftUI 原生端编译通过、Electron 兼容端边界测试 8/8、Tk 兼容端 3/3；另有 JavaScript 语法、Python 编译和启动脚本检查。P0–P5 的设计、边界分析和受控模型验证记录见 [docs/](docs/)。

`demo_workspace` 初始故意包含失败的 `normalize_tag` 测试，用于演示 Agent 搜索代码、提出或执行补丁、运行受限测试、查看 Git diff 并总结结果。

## 安全声明

SEECODER 是本地 coding agent，不是 OS 级安全沙箱。即使在默认 `restricted` 模式下，命令仍运行在当前主机权限中；演示应使用不含凭据的隔离工作区。`--host-shell` 和 `--allow-dangerous-commands` 只应在明确需要且理解风险时使用。
