"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { buildBackendInvocation, buildChatInvocation, parseEventLine, parseGitEnvironment, parseUnifiedDiff, desktopCapabilities, validateWorkspaceFolderName } = require("../core.cjs");

test("backend invocation is literal argv and never enables host shell", () => {
  const result = buildBackendInvocation("/usr/local/bin/uv", "inspect files", "/tmp/workspace");
  assert.equal(result.command, "/usr/local/bin/uv");
  assert.deepEqual(result.args.slice(0, 4), ["run", "seecoder", "run", "inspect files"]);
  assert.ok(result.args.includes("--event-json"));
  assert.ok(!result.args.includes("--host-shell"));
});

test("selected mode is always forwarded so UI and backend cannot diverge", () => {
  const ask = buildBackendInvocation("/usr/local/bin/uv", "inspect files", "/tmp/workspace", "ask");
  assert.ok(ask.args.includes("--mode"));
  assert.ok(ask.args.includes("ask"));
  const auto = buildBackendInvocation("/usr/local/bin/uv", "inspect files", "/tmp/workspace", "auto");
  assert.deepEqual(auto.args.slice(-2), ["--mode", "auto"]);
});

test("chat invocation persists and resumes a local session without a shell", () => {
  const result = buildChatInvocation("uv", "/tmp/workspace", "ask", "/tmp/session.json", true);
  assert.equal(result.command, "uv");
  assert.deepEqual(result.args, ["run", "seecoder", "chat", "--workspace", "/tmp/workspace", "--event-json", "--save", "/tmp/session.json", "--mode", "ask", "--resume", "/tmp/session.json"]);
});

test("event parser rejects malformed and non-object data", () => {
  assert.deepEqual(parseEventLine('{"event":"tool_result","data":{"ok":true}}'), { event: "tool_result", data: { ok: true } });
  assert.equal(parseEventLine("not json"), null);
  assert.equal(parseEventLine('{"event":"x","data":[]}'), null);
});

test("git environment parser produces file and line-change summaries", () => {
  const result = parseGitEnvironment({
    branch: "main\n",
    nameStatus: "M\tsrc/tag_tools.py\nM\tREADME.md\n",
    numstat: "1\t1\tsrc/tag_tools.py\n3\t0\tREADME.md\n",
  });
  assert.deepEqual(result, {
    isRepository: true, branch: "main", added: 4, deleted: 1,
    files: [
      { path: "src/tag_tools.py", status: "M", added: 1, deleted: 1 },
      { path: "README.md", status: "M", added: 3, deleted: 0 },
    ],
  });
});

test("git environment parser includes untracked files", () => {
  const result = parseGitEnvironment({
    branch: "main\n",
    nameStatus: "M\tREADME.md\n",
    numstat: "1\t0\tREADME.md\n",
    untracked: "src/new.js\n",
    untrackedCounts: { "src/new.js": { added: 4, deleted: 0 } },
  });
  assert.deepEqual(result.files.at(-1), { path: "src/new.js", status: "??", added: 4, deleted: 0 });
  assert.equal(result.added, 5);
});

test("git environment parser keeps detached or unborn repositories marked as repositories", () => {
  const environment = parseGitEnvironment({ isRepository: true, branch: "", untracked: "new.py", untrackedCounts: { "new.py": { added: 2, deleted: 0 } } });
  assert.equal(environment.isRepository, true);
  assert.equal(environment.files[0].path, "new.py");
});

test("unified diff parser classifies lines for the local review panel", () => {
  const lines = parseUnifiedDiff("diff --git a/a.js b/a.js\n--- a/a.js\n+++ b/a.js\n@@ -1 +1 @@\n-old\n+new\n same");
  assert.deepEqual(lines.map((line) => line.kind), ["meta", "file", "file", "hunk", "removed", "added", "context"]);
});

test("desktop capability handshake advertises local Git review", () => {
  assert.deepEqual(desktopCapabilities(), { protocolVersion: 2, features: ["local_git_diff"] });
});

test("workspace creator accepts only one safe directory component", () => {
  assert.equal(validateWorkspaceFolderName("  my-feature  "), "my-feature");
  for (const value of ["", ".", "..", "a/b", "a\\b", "\0hidden"]) assert.equal(validateWorkspaceFolderName(value), null);
});
