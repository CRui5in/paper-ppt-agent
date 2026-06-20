import type { Locale } from "../i18n";

type StageContext = "progress" | "history" | "logs";

const PROGRESS_STAGE_ALIASES: Record<string, string> = {
  visual_qa: "generation",
  repair: "generation",
};

const STAGE_LABELS: Record<string, { zh: string; en: string }> = {
  pending: { zh: "等待中", en: "Pending" },
  queued: { zh: "排队中", en: "Queued" },
  started: { zh: "已开始", en: "Started" },
  idle: { zh: "空闲", en: "Idle" },
  parsing: { zh: "解析论文", en: "Parsing" },
  research: { zh: "研究分析", en: "Research" },
  strategy: { zh: "策略规划", en: "Strategy" },
  image_search: { zh: "搜索配图", en: "Image Search" },
  generation: { zh: "生成页面", en: "Generation" },
  agent: { zh: "生成页面", en: "Generation" },
  template_design_spec: { zh: "模板设计规范", en: "Template Design Spec" },
  sequential: { zh: "默认顺序", en: "Default Order" },
  chapter_parallel: { zh: "按章节并行", en: "Parallel by Chapter" },
  page_parallel: { zh: "按页面并行", en: "Parallel by Page" },
  visual_qa: { zh: "视觉QA", en: "Visual QA" },
  repair: { zh: "修复", en: "Repair" },
  postprocess: { zh: "后处理", en: "Post-process" },
  export: { zh: "导出文件", en: "Export" },
  generate: { zh: "生成", en: "Generate" },
  refine: { zh: "反馈优化", en: "Refine" },
  complete: { zh: "已完成", en: "Complete" },
  error: { zh: "失败", en: "Error" },
  cancelled: { zh: "已取消", en: "Cancelled" },
  cancelling: { zh: "取消中", en: "Cancelling" },
  pausing: { zh: "暂停中", en: "Pausing" },
  paused: { zh: "已暂停", en: "Paused" },
  unknown: { zh: "未知", en: "Unknown" },
  "(unknown)": { zh: "未知", en: "Unknown" },
};

const HISTORY_LABELS: Record<string, { zh: string; en: string }> = {
  pending: { zh: "处理中", en: "Pending" },
  queued: { zh: "排队中", en: "Queued" },
  started: { zh: "处理中", en: "Started" },
  parsing: { zh: "解析中", en: "Parsing" },
  research: { zh: "研究中", en: "Research" },
  strategy: { zh: "规划中", en: "Strategy" },
  generation: { zh: "生成中", en: "Generation" },
  agent: { zh: "生成中", en: "Generation" },
  visual_qa: { zh: "视觉QA中", en: "Visual QA" },
  repair: { zh: "修复中", en: "Repair" },
  postprocess: { zh: "后处理中", en: "Post-process" },
  export: { zh: "导出中", en: "Export" },
  complete: { zh: "已完成", en: "Complete" },
  error: { zh: "失败", en: "Error" },
  cancelled: { zh: "已取消", en: "Cancelled" },
  cancelling: { zh: "取消中", en: "Cancelling" },
  pausing: { zh: "暂停中", en: "Pausing" },
  paused: { zh: "已暂停", en: "Paused" },
};

export function normalizeProgressStage(status: string | undefined | null): string {
  const normalized = normalizeStatus(status);
  return PROGRESS_STAGE_ALIASES[normalized] ?? normalized;
}

export function translateStageStatus(
  status: string | undefined | null,
  locale: Locale,
  context: StageContext = "progress",
): string {
  const normalized = normalizeStatus(status);
  const key = context === "progress" ? normalizeProgressStage(normalized) : normalized;
  const labels = context === "history" ? HISTORY_LABELS : STAGE_LABELS;
  const matched = labels[key] ?? STAGE_LABELS[key];
  if (matched) {
    return locale === "zh" ? matched.zh : matched.en;
  }
  return status ?? "";
}

export function translateJobMessage(message: string | undefined, locale: Locale): string | undefined {
  if (!message || locale !== "zh") {
    return message;
  }

  const exact: Record<string, string> = {
    "Generation started": "任务已开始",
    "Refinement started": "优化任务已开始",
    "Queued for generation": "已加入生成队列",
    "Queued for refinement": "已加入优化队列",
    "Queued for Agent feedback revision": "已加入 Agent 反馈优化队列",
    "Preparing Agent workspace...": "正在准备 Agent 工作区...",
    "Agent is generating the deck...": "Agent 正在生成演示文稿...",
    "Codex Agent started.": "Codex Agent 已启动。",
    "Codex Agent completed.": "Codex Agent 已完成。",
    "Codex Agent usage updated.": "Codex Agent 用量已更新。",
    "Agent is applying feedback...": "Agent 正在根据反馈调整...",
    "Agent pause requested. Send guidance to continue.": "已请求暂停 Agent。发送指导后可继续。",
    "Pausing Agent...": "正在暂停 Agent...",
    "Cancelling Agent...": "正在取消 Agent...",
    "Agent paused. Send guidance to continue from the current workspace.": "Agent 已暂停。发送指导后将从当前工作区继续。",
    "Agent is applying guidance...": "Agent 正在根据指导继续...",
    "Interrupt requested. Waiting for the Agent to pause...": "已请求中断，正在等待 Agent 暂停...",
    "Agent has not produced new activity for a while. You can pause it or send guidance to continue.": "Agent 暂时没有新活动。你可以暂停它，或发送指导让它继续。",
    "Parsing paper...": "正在解析论文...",
    "Analyzing paper content...": "正在分析论文内容...",
    "Deep reading: analyzing paper content...": "深度研读：分析论文内容...",
    "Pass 1/4 — Deep reading & critical analysis": "第 1/4 轮 — 深度研读",
    "Enriching with external research APIs...": "正在通过外部研究 API 补充信息...",
    "Querying external research sources...": "正在查询外部信息源...",
    "External research returned no results": "外部研究未返回结果",
    "External research prefetch failed": "外部研究预取失败",
    "Agent job stopped before a live session was available.": "Agent 会话启动前已停止任务。",
    "Preparing paper brief": "正在准备论文概要",
    "Generating manuscript": "正在生成讲稿",
    "Generating manuscript from full paper": "正在根据完整论文生成讲稿",
    "Generating manuscript from brief": "正在根据论文概要生成讲稿",
    "Full paper exceeded provider context; retrying compact fallback": "完整论文超出模型上下文，正在使用精简内容重试",
    "Pass 1/4 — Deep reading": "第 1/4 轮 — 深度研读",
    "Pass 2/4 — Narrative arc": "第 2/4 轮 — 叙事弧线",
    "Pass 3/4 — Manuscript": "第 3/4 轮 — 讲稿生成",
    "Pass 4/4 — Quality review": "第 4/4 轮 — 质量审核",
    "Deep analysis complete (4-pass)": "深度分析完成（4 轮）",
    "Paper analysis complete": "论文分析完成",
    "Manuscript generated": "讲稿已生成",
    "Creating design specification...": "正在生成设计规范...",
    "Design spec created": "设计规范已生成",
    "Generating slide SVGs...": "正在生成幻灯片 SVG...",
    "Generating slide SVGs in parallel...": "正在并行生成幻灯片 SVG...",
    "Generating Direct template design_spec.md with LLM.": "正在使用 LLM 生成直接导入模板的 design_spec.md。",
    "Finalizing SVGs...": "正在整理 SVG...",
    "Finalizing updated SVGs...": "正在整理更新后的 SVG...",
    "Exporting to PowerPoint...": "正在导出 PowerPoint...",
    "Exporting updated PowerPoint...": "正在导出更新后的 PowerPoint...",
    "PowerPoint generated!": "PowerPoint 已生成",
    "Updated PowerPoint generated!": "更新后的 PowerPoint 已生成",
    "Re-generating slides with feedback...": "正在根据反馈重新生成幻灯片...",
    "Re-generating selected slides with feedback...": "正在根据反馈重新生成选定页面...",
    "Refined PowerPoint generated!": "优化后的 PowerPoint 已生成",
    "Job cancelled": "任务已取消",
    "Refine job cancelled": "优化任务已取消",
    "Cancelling generation...": "正在取消生成...",
    "Revising manuscript structure from feedback...": "正在根据反馈重写讲稿结构...",
    "Manuscript revised": "讲稿已更新",
    "Rebuilding design specification...": "正在重建设计规范...",
    "Design spec rebuilt": "设计规范已重建",
    "Usage update": "用量已更新",
    "Codex Agent turn started.": "Codex Agent 已开始当前轮次。",
    "Codex app-server is ready; starting Agent thread...": "Codex 服务已就绪，正在启动 Agent 线程...",
    "Codex Agent thread started; waiting for initial prompt...": "Codex Agent 线程已启动，正在等待初始任务...",
    "Submitting Codex Agent turn...": "正在提交 Codex Agent 轮次...",
    "Codex Agent turn submitted; streaming events...": "Codex Agent 轮次已提交，正在接收实时事件...",
    "Checking Agent output by exporting PPTX...": "正在通过导出 PPTX 检查 Agent 输出...",
    "Agent generation contract failed; requesting a direct per-slide repair...": "Agent 生成结果不符合要求，正在请求逐页修复...",
    "Agent still has SVG/layout contract warnings after repair; continuing export with warnings recorded.": "修复后仍有 SVG/布局警告，已记录警告并继续导出。",
    "Agent repaired direct authoring and SVG contract violations.": "Agent 已修复页面编写和 SVG 规范问题。",
    "Presentation re-exported": "演示文稿已重新导出",
    "Presentation saved from PPTist": "演示文稿已从编辑器保存",
    "Server is busy: too many jobs queued.": "服务器繁忙：排队任务过多。",
    "Server is shutting down.": "服务器正在关闭。",
    "Failed to prepare refine workspace": "无法准备优化工作区",
    "Cannot refine: manuscript.md or design_spec.md missing from project.": "无法优化：项目中缺少 manuscript.md 或 design_spec.md。",
  };
  if (exact[message]) {
    return exact[message];
  }

  if (message.startsWith("External research prefetch failed:")) {
    return message.replace("External research prefetch failed:", "外部研究预取失败：");
  }

  const parsingSourcesMatch = message.match(/^Parsing (\d+) sources\.\.\.$/);
  if (parsingSourcesMatch) {
    return `正在解析 ${parsingSourcesMatch[1]} 个来源...`;
  }

  const queuePositionMatch = message.match(/^Queued for generation \(position (\d+)\)$/);
  if (queuePositionMatch) {
    return `已加入生成队列（第 ${queuePositionMatch[1]} 位）`;
  }

  const parsedWithFailuresMatch = message.match(
    /^Parsed:\s*(.+)\s+\((\d+) source\(s\) failed and were skipped\)$/,
  );
  if (parsedWithFailuresMatch) {
    return `已解析：${parsedWithFailuresMatch[1]}（${parsedWithFailuresMatch[2]} 个来源解析失败，已跳过）`;
  }

  const parsedMatch = message.match(/^Parsed:\s*(.+)$/);
  if (parsedMatch) {
    const title = parsedMatch[1].replace(/\s+\(layout fallback used\)$/, "");
    const fallbackUsed = title !== parsedMatch[1];
    return `已解析：${title}${fallbackUsed ? "（已使用版面兜底解析）" : ""}`;
  }

  const generatedSlideMatch = message.match(/^Generated slide (\d+)\/(\d+)$/);
  if (generatedSlideMatch) {
    return `已生成第 ${generatedSlideMatch[1]}/${generatedSlideMatch[2]} 页`;
  }

  const repairedSlideMatch = message.match(/^Repaired slide (\d+)$/);
  if (repairedSlideMatch) {
    return `已修复第 ${repairedSlideMatch[1]} 页`;
  }

  const generatedSlidesMatch = message.match(/^(\d+) slides generated$/);
  if (generatedSlidesMatch) {
    return `已生成 ${generatedSlidesMatch[1]} 页`;
  }

  const agentGeneratedSlidesMatch = message.match(/^Agent generated (\d+) slides\.$/);
  if (agentGeneratedSlidesMatch) {
    return `Agent 已生成 ${agentGeneratedSlidesMatch[1]} 页`;
  }

  const agentUpdatedSlidesMatch = message.match(/^Agent updated (\d+) slides\.$/);
  if (agentUpdatedSlidesMatch) {
    return `Agent 已更新 ${agentUpdatedSlidesMatch[1]} 页`;
  }

  const agentUpdatedSlideMatch = message.match(/^Agent updated slide (\d+)$/);
  if (agentUpdatedSlideMatch) {
    return `Agent 已更新第 ${agentUpdatedSlideMatch[1]} 页`;
  }

  const regeneratedSlidesMatch = message.match(/^(\d+) slides regenerated$/);
  if (regeneratedSlidesMatch) {
    return `已重新生成 ${regeneratedSlidesMatch[1]} 页`;
  }

  const processedFilesMatch = message.match(/^Processed (\d+) files$/);
  if (processedFilesMatch) {
    return `已处理 ${processedFilesMatch[1]} 个文件`;
  }

  const templateCopiedMatch = message.match(/^Template '([^']+)' copied into Agent workspace\.$/);
  if (templateCopiedMatch) {
    return `模板「${templateCopiedMatch[1]}」已复制到 Agent 工作区。`;
  }

  const exportGatePassedMatch = message.match(/^Agent export gate passed(?::|; PPTX exported successfully:)\s*(.*)$/);
  if (exportGatePassedMatch) {
    const path = exportGatePassedMatch[1].trim();
    return path ? `Agent 导出检查已通过：${path}` : "Agent 导出检查已通过";
  }

  const exportGateFailedDuringMatch = message.match(/^Agent export gate failed during ([^:]+):\s*(.+)$/);
  if (exportGateFailedDuringMatch) {
    return `Agent 导出检查在 ${translateStageStatus(exportGateFailedDuringMatch[1], locale, "progress")} 阶段失败：${exportGateFailedDuringMatch[2]}`;
  }

  const exportGateFailedMatch = message.match(/^Agent export gate failed after (\d+) attempt\(s\)\.\s*(.*)$/);
  if (exportGateFailedMatch) {
    return `Agent 导出检查在 ${exportGateFailedMatch[1]} 次尝试后仍失败。${exportGateFailedMatch[2]}`;
  }

  const timeoutMatch = message.match(/^Job exceeded timeout of (\d+)s$/);
  if (timeoutMatch) {
    return `任务超过 ${timeoutMatch[1]} 秒超时限制`;
  }

  const producedSlidesMismatch = message.match(
    /^Agent produced (\d+) slides, expected at least (\d+)\.$/,
  );
  if (producedSlidesMismatch) {
    return `Agent 生成了 ${producedSlidesMismatch[1]} 页，至少需要 ${producedSlidesMismatch[2]} 页。`;
  }

  const projectMissingMatch = message.match(/^Project directory not found:\s*(.+)$/);
  if (projectMissingMatch) {
    return `未找到项目目录：${projectMissingMatch[1]}`;
  }

  const generationFailedDuringMatch = message.match(/^Generation failed during ([^.]+)\.$/);
  if (generationFailedDuringMatch) {
    return `生成在“${translateStageStatus(generationFailedDuringMatch[1], locale, "progress")}”阶段失败。`;
  }

  const failurePrefixes: Array<[string, string]> = [
    ["Pipeline failed:", "生成流程失败："],
    ["Generation failed:", "生成失败："],
    ["Agent generation failed:", "Agent 生成失败："],
    ["Refinement failed:", "优化失败："],
    ["External research prefetch failed:", "外部研究预取失败："],
    ["Codex runtime is not ready:", "Codex 运行环境未就绪："],
  ];
  for (const [prefix, translated] of failurePrefixes) {
    if (message.startsWith(prefix)) {
      return translated + message.slice(prefix.length).trimStart();
    }
  }

  if (message.startsWith("The complete multi-source corpus exceeds the selected model's context window")) {
    return "完整的多来源资料超出了当前模型的上下文窗口。系统未自动截断内容，请改用更大上下文的模型或主动移除部分来源。";
  }

  // External research enrichment summary, e.g.
  //   "External research — arXiv: 5, Semantic Scholar: 3, Web: no_api_key"
  // Translate the prefix and the per-source counts; leave provider names
  // (arXiv / Semantic Scholar / Web) verbatim because they are proper nouns.
  const enrichmentSummary = message.match(/^External research\s*—\s*(.+)$/);
  if (enrichmentSummary) {
    const parts = enrichmentSummary[1].split(",").map((p) => p.trim());
    const localized = parts.map((part) => {
      const m = part.match(/^([^:]+):\s*(.+)$/);
      if (!m) return part;
      const source = m[1].trim();
      const value = m[2].trim();
      const translatedValue = translateEnrichmentToken(value);
      return `${source}：${translatedValue}`;
    });
    return `外部研究 — ${localized.join("，")}`;
  }

  // Template loading messages (e.g. "Template 'corporate-pro' loaded: Corporate Pro").
  const templateLoadedMatch = message.match(/^Template '([^']+)' loaded:\s*(.+)$/);
  if (templateLoadedMatch) {
    return `已加载模板「${templateLoadedMatch[1]}」：${templateLoadedMatch[2]}`;
  }

  return message;
}

export function translateTemplateImportMessage(message: string | undefined | null, locale: Locale): string | undefined {
  if (!message || locale !== "zh") {
    return message ?? undefined;
  }

  const exact: Record<string, string> = {
    "Agent mode workspace is ready.": "智能体模式工作区已准备好。",
    "Direct import workspace is ready.": "直接导入工作区已准备好。",
    "Claude Code is installed and the Agent SDK is available.": "已安装 Claude Code，Agent SDK 可用。",
    "Claude Code CLI and claude-agent-sdk are not available in the backend environment.": "后端环境中 Claude Code CLI 和 claude-agent-sdk 均不可用。",
    "Claude Code CLI is not installed or not on PATH.": "Claude Code CLI 未安装或不在 PATH 中。",
    "Agent job not found.": "未找到智能体任务。",
    "Upload received.": "已收到上传文件。",
    "Template import requires an LLM model configuration.": "模板导入需要先配置 LLM 模型。",
    "Optimizing template draft with feedback.": "正在根据反馈优化模板草稿。",
    "Running intelligent template analysis.": "正在进行智能模板分析。",
    "LLM template analysis failed.": "LLM 模板分析失败。",
    "Review page types and reusable assets before registering the template.": "注册模板前请检查页面类型和可复用资产。",
    "Generating Direct template design_spec.md with LLM.": "正在使用 LLM 生成直接导入模板的 design_spec.md。",
    "Direct template design_spec.md generation failed.": "直接导入模板的 design_spec.md 生成失败。",
    "Analyzing PPTX structure.": "正在分析 PPTX 结构。",
    "Rendering slides to SVG.": "正在将幻灯片渲染为 SVG。",
    "Cleaning SVGs and detecting reusable assets.": "正在清理 SVG 并检测可复用资产。",
    "Generating a rule-based template draft with placeholders.": "正在生成带占位符的规则模板草稿。",
    "Waiting for mandatory LLM template analysis.": "正在等待必需的 LLM 模板分析。",
    "Template import failed.": "模板导入失败。",
    "Registering reviewed template.": "正在注册已审核模板。",
    "Template registered.": "模板已注册。",
    "Direct template validation failed.": "直接导入模板校验失败。",
    "Registering direct template.": "正在注册直接导入模板。",
    "Direct template registered.": "直接导入模板已注册。",
    "Agent task queued.": "智能体任务已排队。",
    "Starting Claude Agent.": "正在启动 Claude Agent。",
    "Agent running.": "智能体正在运行。",
    "Agent reached the current turn limit; continuing...": "智能体达到当前轮次上限，正在继续。",
    "Agent stream interrupted; resuming the session.": "智能体流中断，正在恢复会话。",
    "Template artifacts updated.": "模板产物已更新。",
    "Agent task complete.": "智能体任务已完成。",
    "Agent task cancelled.": "智能体任务已取消。",
    "Agent task failed.": "智能体任务失败。",
    "Template already installed.": "模板已安装。",
    "Direct import uses the five uploaded slides as-is; no templateization was applied.": "直接导入将按原样使用上传的 5 页幻灯片，不执行模板化处理。",
    "Direct import installed the five uploaded slides as-is; no templateization was applied.": "直接导入已按原样安装上传的 5 页幻灯片，未执行模板化处理。",
    "Agent templateization has not produced a complete five-page template pack yet.": "智能体尚未产出完整的五页模板包。",
    "Preview includes Agent-authored template SVG outputs.": "预览包含智能体生成的模板 SVG 输出。",
    "Template was rebuilt from PPTist deck JSON because no server SVG renderer was available.": "由于服务器 SVG 渲染器不可用，模板已从 PPTist deck JSON 重建。",
    "Preview was rebuilt from the saved PPTist deck because server SVG rendering is unavailable.": "由于服务器 SVG 渲染不可用，预览已从保存的 PPTist deck 重建。",
    "Skipped until the user saves a PPTist deck.": "已跳过，等待用户保存 PPTist deck。",
    "User/Agent workflow owns template planning.": "模板规划由用户/智能体工作流接管。",
  };

  return exact[message] ?? message;
}

function translateEnrichmentToken(value: string): string {
  // Numeric counts pass through unchanged.
  if (/^\d+$/.test(value)) {
    return `找到 ${value}`;
  }
  const map: Record<string, string> = {
    no_extractable_terms: "标题关键词不足，已跳过",
    package_missing: "当前环境未启用，已跳过",
    no_api_key: "未配置 API Key，已跳过",
    no_title: "缺少论文标题，已跳过",
    httpx_missing: "网页请求组件未启用，已跳过",
    query_failed: "查询失败，已跳过",
    timeout: "查询超时，已跳过",
    rate_limited: "请求受限，已跳过",
  };
  if (map[value]) return map[value];
  if (/future at|exception|traceback|error|timeout|timed out|429/i.test(value)) {
    return "查询失败，已跳过";
  }
  return value;
}

export function translateLogLine(log: string, locale: Locale): string {
  const match = log.match(/^\[([^\]]+)\]\s*(.*)$/);
  if (!match) {
    return translateJobMessage(log, locale) ?? log;
  }
  const stage = translateStageStatus(match[1], locale, "logs");
  const message = translateJobMessage(match[2], locale) ?? match[2];
  return `[${stage}] ${message}`;
}

function normalizeStatus(status: string | undefined | null): string {
  return (status ?? "").toLowerCase();
}
