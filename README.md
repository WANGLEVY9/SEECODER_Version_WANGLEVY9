# SEECODER

SEECODER is a small, auditable coding agent built for the Nanjing University software-engineering recommendation project. It deliberately does **not** use an agent framework or hosted code/file tools. The repository implements the agent loop, context budgeting, tool schemas, local execution, error handling, and traces directly.

## Current capabilities

- A single-task CLI agent using one OpenAI-compatible, native tool-calling model.
- Seven local tools: `list_files`, `read_file`, `search_files`, `write_file`, `apply_patch`, `git_diff`, and `run_command`.
- A workspace boundary that resolves symlinks before file access checks; paths outside the selected workspace are rejected.
- Default restricted command execution: literal argument arrays only, no shell expansion, with an allowlist for test/build/format/read-only-Git commands. Host-shell mode is opt-in.
- Deterministic context budgeting, bounded API retry, named stop conditions, Ctrl+C handling, and redacted JSONL execution traces.
- Unit tests that exercise the agent loop without a real model API key.

## Setup

Python 3.12 or newer is required. Install the declared runtime dependency with `uv`:

```bash
uv sync
cp .env.example .env
```

Then set `SEECODER_API_KEY` in `.env`, or export it in the shell. The checked-in example is configured for DeepSeek V4 Flash (`https://api.deepseek.com`, model `deepseek-v4-flash`) with thinking explicitly disabled for the P0 tool-call baseline. Any OpenAI-compatible gateway can be selected by changing the Base URL and model name.

Keep `.env` outside the editable workspace. By default SEECODER reads `.env` from the directory in which the CLI is launched and refuses an `--env-file` inside `--workspace`. This prevents the agent's own file tools from receiving the API key.

## Run

Launch from the project root and use a target directory as the agent workspace. File tools are constrained to that directory; command tools start there but are **not** an operating-system sandbox.

```bash
uv run seecoder run "Inspect this workspace, fix the failing tag-normalization behavior, run the test suite, and summarize the change." \
  --workspace demo_workspace
```

Use `uv run seecoder --help` for options. Each run writes an ignored, redacted JSONL trace under `<launch-directory>/runs/` by default. The program does not print API keys or request headers.

## Local desktop UI

The primary desktop interface is an original Electron shell with native HTML/CSS/JavaScript rendering. This is a UI technology only, not an Agent framework: Electron starts the existing self-written CLI as a local child process and renders its local JSONL events. It neither embeds Codex nor calls a hosted execution/file service.

```bash
node --version              # Node.js 22.12 or newer
cd desktop/electron
npm install                 # first launch only
cd ../..
./desktop/run_desktop_electron.sh
```

Sessions are saved only in the app's local browser storage and no UI code reads, displays, or persists API keys. The backend invocation uses a literal argv array in default restricted mode and never passes `--host-shell`. The earlier Tk implementation remains as a compatibility fallback at `desktop/run_desktop.sh`; the Electron UI is the recommended demo surface. See [docs/p2-desktop-plan.md](docs/p2-desktop-plan.md) for the architecture and assessment boundary.

The CLI returns `0` only when the model gives a final response; stop-limit, tool-error, model-failure, protocol-failure, and cancellation outcomes have distinct non-zero codes and remain visible in the trace.

## Test without any API call

```bash
PYTHONPATH=src python3.12 -m unittest discover -s tests -v
python3.12 -m unittest discover -s demo_workspace/tests -v
```

The demo fixture initially fails by design. It is not part of the repository's own test suite because the agent is supposed to repair it during the demonstration.

## Design boundary

SEECODER relies only on the ordinary model-provider client library and the model's native tool-calling output. It does not wrap an existing coding agent, use LangChain/LlamaIndex/OpenAI Agents SDK/AutoGen/CrewAI, or delegate file and command execution to an API service. Details and P0 acceptance criteria are in [docs/p0-plan.md](docs/p0-plan.md); P1 validation is recorded in [docs/p1-validation-2026-08-27.md](docs/p1-validation-2026-08-27.md).

## Safety note

This is a local coding agent, not an OS security sandbox. In default `restricted` mode, command execution uses a small allowlist and literal argv arrays, but it still runs on the host. `--host-shell` opts into the legacy shell path, which inherits the user's host permissions and is only partially guarded by cwd, timeouts, environment scrubbing, output bounds, and a conservative blocklist. Use an isolated demo workspace with no credentials; `--allow-dangerous-commands` affects only host-shell mode and should only be used when explicitly necessary.
