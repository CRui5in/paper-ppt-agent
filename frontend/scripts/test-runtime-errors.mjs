import { readFile } from "node:fs/promises";
import assert from "node:assert/strict";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/lib/runtimeErrors.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } });
const { describeRuntimeError } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

for (const [name, message, expected] of [
  ["Codex model access", "Codex Agent failed: The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account.", "无法使用当前模型"],
  ["Claude authentication", "Claude Code Agent failed: authentication_failed HTTP 401", "身份验证失败"],
  ["Claude model access", "Claude Code Agent failed: There's an issue with the selected model. It may not exist or you may not have access to it.", "无法使用当前模型"],
  ["Claude usage limit", "Claude Code Agent failed: rate_limit_error HTTP 429", "额度不足"],
  ["Claude billing error", "Claude Code Agent failed: billing_error", "额度不足"],
  ["approval denied", "Codex Agent failed: approval rejected", "被拒绝"],
  ["missing SDK", "Codex Agent runtime requires the `openai-codex` Python SDK.", "运行环境未就绪"],
  ["stream interrupted", "Claude Code Agent stream ended without a result.", "连接中断"],
  ["legacy empty output", "Agent mode completed without producing SVG slides in svg_output/.", "未生成可导出"],
]) {
  test(name, () => {
    const result = describeRuntimeError(message, "zh");
    assert.ok(result.summary.includes(expected), result.summary);
    assert.ok(result.action.length > 0);
  });
}

test("model name and English guidance", () => {
  const result = describeRuntimeError("Codex: The 'gpt-6.1-sol' model is not supported", "en");
  assert.ok(result.summary.includes("gpt-6.1-sol"));
  assert.ok(result.action.includes("account access"));
});

test("unrelated errors retain their original presentation", () => {
  assert.equal(describeRuntimeError("Invalid XML on slide 3", "zh"), null);
});
