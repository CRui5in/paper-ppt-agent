import type { Locale } from "../i18n";

type StageContext = "progress" | "history" | "logs";

const PROGRESS_STAGE_ALIASES: Record<string, string> = {};

const STAGE_LABELS: Record<string, { zh: string; en: string }> = {
  pending: { zh: "等待中", en: "Pending" },
  started: { zh: "已开始", en: "Started" },
  idle: { zh: "空闲", en: "Idle" },
  parsing: { zh: "解析论文", en: "Parsing" },
  research: { zh: "研究分析", en: "Research" },
  strategy: { zh: "策略规划", en: "Strategy" },
  generation: { zh: "生成页面", en: "Generation" },
  visual_qa: { zh: "视觉 QA", en: "Visual QA" },
  repair: { zh: "修复", en: "Repair" },
  postprocess: { zh: "后处理", en: "Post-process" },
  export: { zh: "导出文件", en: "Export" },
  generate: { zh: "生成", en: "Generate" },
  refine: { zh: "反馈优化", en: "Refine" },
  complete: { zh: "已完成", en: "Complete" },
  error: { zh: "失败", en: "Error" },
  cancelled: { zh: "已取消", en: "Cancelled" },
  cancelling: { zh: "取消中", en: "Cancelling" },
  unknown: { zh: "未知", en: "Unknown" },
  "(unknown)": { zh: "未知", en: "Unknown" },
};

const HISTORY_LABELS: Record<string, { zh: string; en: string }> = {
  pending: { zh: "处理中", en: "Pending" },
  started: { zh: "处理中", en: "Started" },
  parsing: { zh: "解析中", en: "Parsing" },
  research: { zh: "研究中", en: "Research" },
  strategy: { zh: "规划中", en: "Strategy" },
  generation: { zh: "生成中", en: "Generation" },
  visual_qa: { zh: "视觉 QA 中", en: "Visual QA" },
  repair: { zh: "修复中", en: "Repair" },
  postprocess: { zh: "后处理中", en: "Post-process" },
  export: { zh: "导出中", en: "Export" },
  complete: { zh: "已完成", en: "Complete" },
  error: { zh: "失败", en: "Error" },
  cancelled: { zh: "已取消", en: "Cancelled" },
  cancelling: { zh: "取消中", en: "Cancelling" },
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
    "Parsing paper...": "正在解析论文...",
    "Analyzing paper content...": "正在分析论文内容...",
    "Deep reading: analyzing paper content...": "深度研读：分析论文内容...",
    "Pass 1/4 — Deep reading & critical analysis": "第 1/4 轮 — 深度研读",
    "Enriching with external research APIs...": "正在通过外部研究 API 补充信息...",
    "Querying external research sources...": "正在查询外部信息源...",
    "External research returned no results": "外部研究未返回结果",
    "Generating manuscript": "正在生成讲稿",
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
    "Finalizing SVGs...": "正在整理 SVG...",
    "Exporting to PowerPoint...": "正在导出 PowerPoint...",
    "PowerPoint generated!": "PowerPoint 已生成",
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
  };
  if (exact[message]) {
    return exact[message];
  }

  const parsedMatch = message.match(/^Parsed:\s*(.+)$/);
  if (parsedMatch) {
    return `已解析：${parsedMatch[1]}`;
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

  const regeneratedSlidesMatch = message.match(/^(\d+) slides regenerated$/);
  if (regeneratedSlidesMatch) {
    return `已重新生成 ${regeneratedSlidesMatch[1]} 页`;
  }

  const processedFilesMatch = message.match(/^Processed (\d+) files$/);
  if (processedFilesMatch) {
    return `已处理 ${processedFilesMatch[1]} 个文件`;
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

function translateEnrichmentToken(value: string): string {
  // Numeric counts pass through unchanged.
  if (/^\d+$/.test(value)) {
    return `找到 ${value}`;
  }
  const map: Record<string, string> = {
    no_extractable_terms: "无可提取术语",
    package_missing: "依赖未安装",
    no_api_key: "未配置 API Key",
    no_title: "缺少标题",
    httpx_missing: "httpx 未安装",
  };
  return map[value] ?? value;
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
