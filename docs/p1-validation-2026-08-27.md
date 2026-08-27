# P1 验收记录（2026-08-27）

## 约束复核

- 仅使用普通 `openai` 厂商客户端和模型原生 tool calling。
- Agent 循环、上下文、工具 schema/执行/输出解析、停止条件和 trace 均由本仓库实现。
- 未引入 LangChain、LlamaIndex、OpenAI Agents SDK、Claude Agent SDK、AutoGen、CrewAI，或托管代码/文件执行服务。
- API 密钥只存在于被 `.gitignore` 忽略的本地 `.env`；本记录和 trace 均不记录密钥。

## 离线验证

执行：`PYTHONPATH=src python3.12 -m unittest discover -s tests -v`

结果：42 项测试通过。覆盖路径边界、dotenv 隔离、超大/二进制文件、精确补丁失败不写入、只读 Git、thinking 协议、上下文终止、redacted trace、受限 argv 与 host-shell 兼容路径。

## 真实 API 分级验证

模型为 `deepseek-v4-flash`，每次测试均使用有限步数和临时、无凭据 workspace。

1. 禁用 thinking 的一轮无工具请求返回约定短句，确认基本连通性。
2. 启用 thinking 的受限工具回合成功保留并回传 provider 返回的 `reasoning_content`；最初缺失字段的响应被协议保护明确拒绝，随后修复客户端兼容字段读取后成功。
3. 受限 demo 闭环：新建 `/private/tmp` 下的临时 Git fixture，提交原始失败样例；Agent 最多 10 步运行，实际 6 步完成：`list_files`、`read_file`、精确 `apply_patch`、`run_command(argv)`、`git_diff`、最终总结。

该闭环将 `src/tag_tools.py` 中 `tag.lower()` 改为 `tag.strip().lower()`；工具内测试退出码为 0，独立复跑 `unittest` 退出码为 0，`git diff --check` 退出码为 0。对应本地忽略 trace：`runs/20260827T111530Z-1ef4e4c7.jsonl`。

## P1.5 提交前清单

- 每次从原始失败 fixture 新建临时副本，连续完整预演三次，记录成功/失败、步数、耗时与 API 成本。
- 视频只录一次成功预演：展示初始失败、Agent 工具调用、受限测试通过、Git diff 和简短总结；确保小于 2 分钟、MP4、小于 200 MB。
- `README.txt` 另行压缩至 1000 个中文字符以内；ZIP 仅包含 `README.txt` 与视频，不包含源代码、`.env`、trace 或依赖目录。
- 提交前检查公开仓库完整历史、远程可见、无密钥、无 deadline 后提交，并准备单 Agent/本地工具/reasoning 回传/受限执行的答辩说明。
