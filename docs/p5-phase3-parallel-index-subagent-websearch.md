# P5 Phase-3：并行工具调用 + 代码索引 + 子代理 + web_search（已实现并验证）

> 完成日期：2026-08-27。全部自研，未引入 Agent 框架/SDK；仅用 `openai` 客户端 + 原生 tool calling。

## 1. 新增/修改模块

| 模块 | 内容 |
| --- | --- |
| `runner.py` | 并行工具调用：同一回合内全为只读调用时并发执行（`ThreadPoolExecutor`），有变更调用则串行以保因果；新增 `enable_subagents` 与 `SpawnAgentTool` 工厂 |
| `tools/search_code.py`（新） | 确定性符号索引：扫描源码提取 class/def/function/struct 等定义，按查询返回 file/line/kind/snippet |
| `tools/subagent.py`（新） | `spawn_agent` 工具：运行一个有界子代理（相同 workspace/模型），子代理内禁用再 spawn 以防无界递归 |
| `tools/web_search.py`（新） | 自研本地 HTTP 搜索（可注入 fetcher、有界、失败降级为结构化错误） |
| `tools/__init__.py` | 导出并注册 3 个新工具 |

## 2. 关键设计

- **并行**：仅当同一回合内所有工具都是只读（list/read/search/git_diff）时并发；任何变更调用（write/patch/command）都走串行，避免读写竞态与因果错乱。
- **子代理**：`spawn_agent` 作为工具暴露给模型；工厂内部构建一个 `enable_subagents=False` 的嵌套 runner（独立的上下文与步数预算），复用同一模型客户端与 workspace；子代理失败作为 `SubAgentError` 可观测结果回填，不致命。
- **代码索引**：语言无关的符号正则 + 源码扩展名白名单 + 噪声目录/二进制/超大文件防护 + 结果上限，确定性、可离线测试。
- **web_search**：用 `urllib` 抓取并解析；fetcher 可注入（测试用 mock），网络不可用时返回 `WebSearchUnavailable`。

## 3. 离线测试（干净环境，65 项全绿）

新增并行只读回填、search_code 符号命中、spawn_agent 子代理回合、web_search 解析 + 失败降级等用例。Electron 核心 3 项通过。

## 4. 真实 API 受控验证（deepseek-v4-flash）

对 demo_workspace 执行：先用 `search_code` 定位 `normalize_tag`（命中 `src/tag_tools.py:4`），再用 `spawn_agent` 派 `inspector` 子代理概括该函数。子代理给出深入审查（含空白/标点/类型安全等边界情况），主代理汇总结论。usage 约 13.7k tokens。

## 5. 仍待做

更强执行隔离（macOS sandbox-exec / 临时工作副本）、会话自动检查点、插件/MCP 注册、多模型路由。
