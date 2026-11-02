import type { Locale } from "../i18n";

/** Keep the original error for diagnostics; present the cause and next step first. */
export function describeRuntimeError(error: string, locale: Locale) {
  const zh = locale === "zh";
  const runtime = /codex/i.test(error) ? "Codex" : /claude|anthropic/i.test(error) ? "Claude Code" : "Agent";
  const model = error.match(/['"`]([^'"`]+)['"`] model/i)?.[1];
  const describe = (summary: string, action: string) => ({ summary, action });

  if (/model_not_found|unknown model|model[^\n]*(?:not supported|not found|does not exist|may not exist|not have access|not available|unavailable)|(?:unsupported|invalid)[ _-]model|没有.*模型|模型.*不支持/i.test(error)) {
    return describe(
      zh ? `${runtime} 无法使用当前模型${model ? `（${model}）` : ""}。` : `${runtime} cannot use the selected model${model ? ` (${model})` : ""}.`,
      zh ? "重启应用以更新 Agent 运行时后重试；若仍失败，请检查模型名称和账号访问权限，或选择可用模型。" : "Restart the app to update the Agent runtime and retry. If it still fails, check the model name and account access, or choose an available model.",
    );
  }
  if (/unauthorized|authentication[_ -]?(?:error|failed|required)|not (?:logged|signed) in|invalid[_ -](?:api[_ -]?key|token)|token[^\n]*(?:expired|revoked)|(?:login|sign in|re-authorization) required|refresh token|\b401\b/i.test(error)) {
    return describe(
      zh ? `${runtime} 登录或身份验证失败。` : `${runtime} authentication failed.`,
      zh ? `请重新登录 ${runtime}；使用自定义接口时，检查 API Key 和接口地址后重试。` : `Sign in to ${runtime} again. For a custom provider, check the API key and endpoint, then retry.`,
    );
  }
  if (/quota|usage[_ -]?limit|insufficient[_ -]credits|credit balance|billing[_ -]error|payment[_ -]required|rate[_ -]?limit|too many requests|\b(?:402|429)\b|spending.*limit|hit your limit/i.test(error)) {
    return describe(
      zh ? `${runtime} 额度不足或请求受限。` : `${runtime} reached a usage or rate limit.`,
      zh ? "请检查账号额度和限流提示，等待恢复或调整套餐后重试。" : "Check your account usage and rate limits. Retry when access resets or after adjusting your plan.",
    );
  }
  if (/permission denied|access denied|forbidden|approval[^\n]*(?:denied|rejected)|\b403\b|EACCES/i.test(error)) {
    return describe(
      zh ? `${runtime} 的请求或文件操作被拒绝。` : `${runtime} was denied access.`,
      zh ? "请检查账号权限、Agent 审批结果和工作目录的读写权限后重试。" : "Check account access, Agent approval results, and workspace read/write permissions, then retry.",
    );
  }
  if (/(?:openai-codex|claude-agent-sdk|codex|claude)[^\n]*(?:not available|not installed|not found|requires|cannot import|no module|runtime is not ready)|cannot import name[^\n]*(?:codex|claude)|No module named ['"](?:openai_codex|claude_agent_sdk)/i.test(error)) {
    return describe(
      zh ? `${runtime} 运行环境未就绪。` : `${runtime} is not ready.`,
      zh ? "请通过启动器重启应用，更新 Agent SDK 和配套运行时后重试；更新失败时查看启动器中的错误。" : "Restart using the launcher to update the Agent SDK and bundled runtime. If the update fails, check the launcher error.",
    );
  }
  if (/without producing SVG slides|no SVG slides|no finalized SVG/i.test(error)) {
    return describe(
      zh ? "Agent 未生成可导出的幻灯片。" : "The Agent did not produce slides to export.",
      zh ? "请检查 Agent 日志中的模型、登录或运行时错误；修复后重新生成。" : "Check the Agent log for model, authentication, or runtime errors. Resolve the cause and generate again.",
    );
  }
  if (/codex|claude|agent/i.test(error) && /stream[^\n]*(?:ended|closed|disconnect)|transport[^\n]*closed|no activity|timed? ?out|timeout|connection[^\n]*(?:failed|closed|reset|refused)/i.test(error)) {
    return describe(
      zh ? `${runtime} 连接中断或响应超时。` : `${runtime} lost its connection or timed out.`,
      zh ? "请检查网络和代理设置后重试；持续失败时，重启应用以更新运行时。" : "Check your network and proxy settings, then retry. If the problem persists, restart the app to update the runtime.",
    );
  }
  return null;
}
