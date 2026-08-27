# 交付审计与桌面端预演记录（2026-08-28）

## 静态审计

- 依赖清单仅含模型厂商客户端 `openai`；未发现 LangChain、LlamaIndex、OpenAI Agents SDK、Claude Agent SDK、AutoGen、CrewAI、Code Interpreter 或 Files API。
- 检查了可达 Git 历史：8 个提交均在截止前；`git fsck --no-reflogs --full` 未发现悬空 commit。存在本地不可达 tree 对象，但不属于可达历史，也不会随普通 push 上传。
- 对可追踪内容进行凭据模式扫描，只命中单元测试中的显式假值；没有真实密钥。`.env`、运行轨迹、录屏和预演目录均被 Git 忽略。
- 主项目验证基线：Python 65 项、Electron 3 项、Tk 3 项测试均通过；最终交付仍缺 `README.txt`、视频、三次连续预演和仅含两项文件的 ZIP。

## 桌面端预演

测试任务：在独立工作区修复 `normalize_tag`，让其保留小写化并去除输入首尾空白；禁止修改测试；复跑 unittest 与 Git diff。

| 轮次 | 工作区 | 结果 | 证据 |
| --- | --- | --- | --- |
| 01 | `validation_workspaces/desktop-agent-run-01` | 功能成功，交付证据不完整 | Agent 按审批完成读取、失败复现、补丁与测试；2 项测试通过。该目录未建 Git 基线，`git_diff` 无可展示变更，流程被停止。 |
| 02 | `validation_workspaces/desktop-agent-run-02` | 完整成功 | 先建初始 Git 提交并确认测试失败；桌面端 ask 模式 6 步完成读取、命令审批、补丁审批、回归测试、`git_diff` 与中文总结；进程退出码 0，2 项测试通过，diff 仅改 `src/tag_tools.py`。 |

## 发现与修复

预演发现任务完成或停止后，旧的“批准工具调用”横幅可能残留。Electron 渲染层已修正：所有终态、进程退出与新建会话都会清除审批横幅。此改动不改变 Agent 后端或工具权限。

## 结论与后续

桌面端真实调用的是自研本地 CLI/AgentRunner，审批卡片确实在 `run_command` 和 `apply_patch` 前阻断执行；模型只输出工具意图。还需以同一带 Git 基线的方式再完成两次独立预演，再制作最终提交物。
