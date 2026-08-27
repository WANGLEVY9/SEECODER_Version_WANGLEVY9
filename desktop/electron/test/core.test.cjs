"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { buildBackendInvocation, parseEventLine } = require("../core.cjs");

test("backend invocation is literal argv and never enables host shell", () => {
  const result = buildBackendInvocation("/usr/local/bin/uv", "inspect files", "/tmp/workspace");
  assert.equal(result.command, "/usr/local/bin/uv");
  assert.deepEqual(result.args.slice(0, 4), ["run", "seecoder", "run", "inspect files"]);
  assert.ok(result.args.includes("--event-json"));
  assert.ok(!result.args.includes("--host-shell"));
});

test("mode is forwarded only for non-auto modes", () => {
  const ask = buildBackendInvocation("/usr/local/bin/uv", "inspect files", "/tmp/workspace", "ask");
  assert.ok(ask.args.includes("--mode"));
  assert.ok(ask.args.includes("ask"));
  const auto = buildBackendInvocation("/usr/local/bin/uv", "inspect files", "/tmp/workspace", "auto");
  assert.ok(!auto.args.includes("--mode"));
});

test("event parser rejects malformed and non-object data", () => {
  assert.deepEqual(parseEventLine('{"event":"tool_result","data":{"ok":true}}'), { event: "tool_result", data: { ok: true } });
  assert.equal(parseEventLine("not json"), null);
  assert.equal(parseEventLine('{"event":"x","data":[]}'), null);
});
