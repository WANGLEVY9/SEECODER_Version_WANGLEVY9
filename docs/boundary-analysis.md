# SEECODER 开发边界分析（对照推免考核要求）

> 审计日期：2026-08-27。本文档回答三个问题：项目现状是什么、考核边界是什么、当前设计与边界的关系是什么。

## 0. 结论先行

- **合规性**：项目完全落在题目允许范围内——agent 内核全部自研，唯一的运行时依赖是模型厂商客户端 `openai`，只使用模型原生 tool calling，本地执行文件和命令；没有封装现成 agent 产品，没有使用任何 agent 框架/SDK，没有调用托管代码执行或文件 API。
- **提交物进度**：Git 仓库 ✅（已公开、已推送、历史无密钥）；README.txt ⏳ 未写；视频 ⏳ 未录。
- **剩余时间**：约 6 天（截止 2026-09-02 24:00）。核心工作量已基本完成，剩余工作集中在 P1.5：预演、README.txt、视频、打包、提交纪律。

## 1. 项目代码全貌

```
SEECODER（Python 3.12 + uv）
├─ src/seecoder/                  # 自研 agent 内核
│  ├─ cli.py                      # argparse CLI、退出码语义、--event-json 事件协议
│  ├─ config.py                   # 自研 dotenv 子集解析、Settings 校验（进程环境优先于文件）
│  ├─ types.py                    # ChatMessage / ToolCall / ToolResult / RunState 领域类型
│  ├─ model_client.py             # OpenAI 兼容适配（原生 tool calling）+ 有界重试 + reasoning_content 兼容读取
│  ├─ runner.py                   # AgentRunner 状态机主循环（核心）
│  ├─ context.py                  # 确定性字符预算裁剪，保留 system+初始任务+最近完整工具回合
│  ├─ trace.py                    # JSONL 执行轨迹，密钥名+已知密钥值双重脱敏
│  └─ tools/                      # 7 个本地工具 + ToolRegistry + WorkspaceBoundary
│     ├─ base.py                  # 注册表、JSON 参数校验、symlink 解析后工作区边界
│     ├─ files.py                 # list_files / read_file / write_file / search_files / apply_patch
│     ├─ git.py                   # git_diff（只读，固定参数向量）
│     └─ shell.py                 # run_command：restricted(argv 白名单) / host_shell(黑名单) 双模式
├─ desktop/
│  ├─ electron/                   # 原创 Electron 展示层（contextIsolation+sandbox，spawn CLI 子进程）
│  └─ seecoder_desktop.py         # Tk 兼容回退版本
├─ demo_workspace/                # 演示 fixture：normalize_tag 缺陷（按设计失败）
├─ tests/                         # 43 项离线单元测试，不依赖真实 API
├─ docs/                          # P0/P1/P2 计划与验收记录
└─ pyproject.toml / uv.lock       # 依赖树仅 openai + 其传递依赖（httpx/pydantic 等）
```

## 2. 考核要求逐项对照

### 2.1 必须自研的重要逻辑（题目明列 5 项，全部满足）

| 要求 | 实现位置 | 说明 |
| --- | --- | --- |
| 对话历史与上下文管理 | `context.py` ContextManager + `runner.py` messages 列表 | 固定字符预算、保留 system+任务意图+最近完整工具回合、thinking 模式下拒绝破坏性裁剪（命名终止而非丢历史） |
| 工具的定义与本地执行 | `tools/` 全部 | schema 自写、JSON 参数校验自写、dispatch 自写、执行全部为本地文件系统/subprocess |
| 模型输出的解析 | `model_client.py` | 从 provider message 提取 tool_calls（id/name/arguments）、跨 openai 客户端版本读取 DeepSeek reasoning_content |
| 循环终止条件 | `runner.py` + `types.py` RunState | FINAL / STOP_MAX_STEPS / STOP_TOOL_ERROR_LIMIT / STOP_CONTEXT_BUDGET / FAILED_MODEL / FAILED_PROTOCOL / CANCELLED 共 7 类，CLI 映射不同退出码 |
| 错误处理 | 分散于各层 | 工具失败结构化返回（不崩溃）、API 有界重试（429/5xx）、thinking 协议保护、配置校验、trace 脱敏 |

### 2.2 允许使用的（均在用）

- **模型厂商 API 客户端库**：官方 `openai` 客户端（uv.lock 全依赖树仅此一个业务依赖）✅
- **OpenAI 兼容网关**：`SEECODER_BASE_URL` 可指向任意兼容端点 ✅
- **模型原生 tool calling**：Chat Completions 的 `tools` + `tool_choice=auto` ✅

### 2.3 禁止的红线（全部未触犯）

| 红线 | 现状 |
| --- | --- |
| 在现成 agent 产品上封装界面 | 无。桌面端为原创 Electron/Tk，仅作展示层，不嵌入 Codex 等任何现成产品 |
| 使用 agent 框架/SDK（LangChain、LlamaIndex、OpenAI Agents SDK、Claude Agent SDK、AutoGen、CrewAI） | 依赖树中不存在任何一个 |
| 依赖 API 服务端托管的代码执行或文件工具（Code Interpreter、Files API） | 无。文件读写、命令执行、Git 全部在本地完成 |

### 2.4 其他规则

- 截止时间 2026-09-02 24:00 —— 距今约 6 天。
- 允许并鼓励 AI 工具辅助开发 —— 项目中已使用（docs 亦如实记录），且每一处设计均有归属。
- API key 不出现在仓库/README/视频 —— `.env` 已 gitignore 且未跟踪；trace 对密钥脱敏；CLI 拒绝 workspace 内的 `.env`/trace 目录；桌面 UI 不读取、不显示、不持久化 key。

## 3. 边界上的设计决策（答辩重点）

每个决策都要能回答"为什么这样设计、为什么在边界内"：

1. **Electron/Tk 桌面端为什么不是"封装现成 agent 产品"**：UI 层不拥有 agent 逻辑。它通过受控的子进程调用自研 CLI（`spawn(uv, [run, seecoder, run, task, --workspace, ..., --event-json])`，`shell:false`），只渲染本地 JSONL 事件。工具 schema、模型输出解析、文件/命令执行、终止条件全部在 Python 后端。UI 只是"展示层技术"，与考核要求允许的"编程语言不限"相容。
2. **`run_command` 不是 OS 沙箱**：这是最需要诚实声明的边界。cwd、黑名单、超时、敏感环境剥离能降低风险但挡不住有意的逃逸。因此 README 与工具契约都明确"本地 coding agent，非操作系统沙箱"，演示必须使用不含凭据的隔离 workspace。不要把它宣传成沙箱，否则答辩会被问倒。
3. **上下文用确定性裁剪而非自动摘要**：P0/P1 选择可测试、可解释的裁剪策略（保留任务意图+最近完整回合），避免引入额外模型调用与不可预测行为。答辩可说明这是"先正确后复杂"的取舍。
4. **DeepSeek thinking 模式必须原样回传 reasoning_content**：官方协议要求工具调用后的后续请求携带原始 reasoning；实现为可选 enabled 模式，缺字段时协议保护拒绝（FAILED_PROTOCOL），trace 只记长度+哈希。P0 默认 disabled 是为了建立稳定的 tool-calling 基线——这是有记录的决策。
5. **单模型适配 + Protocol 接口**：`ModelClient` 是 Protocol，OpenAICompatibleClient 是第一个 adapter；扩展其他厂商不改 agent loop。答辩可展示这是面向扩展而非堆砌。
6. **退出码语义**：FINAL=0，停止条件/失败/Ctrl+C 分别 3/4/130，desktop 与 CLI 据此区分结果，便于演示可审计。

## 4. 现状验证（2026-08-27 实测）

- 43 项 Python 单元测试在**干净环境**下全部通过（`env -i PATH=... PYTHONPATH=src python3.12 -m unittest discover -s tests`）。
- Electron 端 2 项测试通过（`node --test`）：后端调用为字面 argv、不传 `--host-shell`；事件解析器拒绝畸形输入。
- `python -m seecoder --help` 正常；缺 key 时 CLI 明确报 `Missing required configuration: SEECODER_API_KEY` 且不回显密钥。
- `demo_workspace` fixture 按设计失败（`normalize_tag` 未 strip 空白）——这正是视频演示的起点。

**一个环境注意点**：`Settings.from_environment` 语义是"进程环境变量优先于 .env 文件"。若 shell 中已 export `SEECODER_*`（例如通过 `source .env`），`test_config.py` 中"文件内非法值应报错"的两个用例会被环境覆盖而失败。这是设计语义而非代码缺陷；CI/录制时请用干净环境（本项目 README 中声明的测试命令在干净 shell 下通过）。

## 5. Git / 仓库合规审计

- remote：`https://github.com/WANGLEVY9/SEECODER_Version_WANGLEVY9.git`，GitHub 页面确认 `public:true`、`isFork:false`、`isMirror:false`。
- 仓库创建时间 2026-08-27T06:26:53Z（UTC，即北京 14:26），首笔提交 12:00Z —— 需与"题目发布"时间比对确认满足"题目发布后新建"。
- 共 4 个 commit，全部 2026-08-27；reflog 无改写痕迹；`git grep` 全历史未发现真实密钥（仅测试用假 key）。
- 本地存在 `refs/codex/*` 检查点引用（Codex CLI 开发痕迹），仅本地、普通 push 不会推送，无需处理。
- **提交粒度提示**：目前 4 个 commit 且代码一次成型（docs 中宣称"每步一提交"与实际不符）。这不违规（历史完整、未改写），但评委"结合提交时间与内容了解开发过程"时，4 个 commit 的过程性较弱。剩余 6 天建议按功能点/修复小步提交，9/2 24:00 后停止推送。

## 6. 剩余工作与风险清单（按优先级）

1. **三次连续预演（P1.5）**：每次从原始失败 fixture 复制到新的临时 workspace（保持源 fixture 永远失败），记录成功/失败、步数、耗时与 API 成本。
2. **README.txt（≤1000 汉字）**：仓库地址；如何运行；特色功能说明；其它说明。与仓库 README.md 分离，单独成文。
3. **视频（≤2 分钟、mp4、≤200 MB）**：用 Electron 桌面端录制一次成功预演（初始失败 → 工具调用 → 受限测试通过 → git diff → 总结），可剪辑加速；**录屏时避免画面中出现 `.env` 内容**。
4. **打包**：`姓名.zip` 只含 README.txt + 视频，提交到考核表单（可重复提交，以最后一次为准）。
5. **截止纪律**：9/2 24:00 后不再向仓库推送新提交。
6. **提交前复核**：公开仓库页渲染正常、README 地址正确、无密钥、历史未压缩。

## 7. 一句话边界总结

> SEECODER 自己实现了"模型提出的工具意图 → 本地校验 → 本地执行 → 结果回填 → 判定终止"的完整闭环；模型厂商只提供 Chat Completions 的 tool calling 原语，其余一切（历史、上下文、schema、解析、执行、错误、停止、追踪）都是本项目代码。红线之外的任何东西都没有进入依赖树。
