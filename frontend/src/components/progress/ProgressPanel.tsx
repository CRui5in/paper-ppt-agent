import { AlertCircle, CheckCircle2, ChevronDown, ChevronRight, Circle, Download, FileSearch, Globe, GraduationCap, Loader2, Search, Settings, Sparkles, Target, Wand2 } from "lucide-react";
import { type ComponentType, type CSSProperties, useState } from "react";
import type { JobStatus, ResearchEnrichmentStats, ResearchFinding } from "../../lib/types";
import { useLocale } from "../../i18n";
import { normalizeProgressStage, translateJobMessage, translateStageStatus } from "../../lib/i18nStatus";
import { HoverTooltip } from "../common/HoverTooltip";

export const PROGRESS_STAGES: Array<{ id: string; icon: ComponentType<{ size?: number; color?: string }> }> = [
  { id: "parsing", icon: FileSearch },
  { id: "research", icon: Search },
  { id: "strategy", icon: Target },
  { id: "generation", icon: Wand2 },
  { id: "postprocess", icon: Settings },
  { id: "export", icon: Download },
];

interface ProgressPanelProps {
  job?: JobStatus;
  connectionStatus: string;
  enrichmentStats?: ResearchEnrichmentStats;
  hideHeader?: boolean;
  compact?: boolean;
  logs?: string[];
}

const STAGE_INDEX = new Map(PROGRESS_STAGES.map((stage, index) => [stage.id, index]));

export function ProgressPanel({ job, connectionStatus, enrichmentStats, hideHeader = false, compact = false, logs = [] }: ProgressPanelProps) {
  const { t, locale } = useLocale();
  const isConnected = connectionStatus === "connected";
  const isConnecting = connectionStatus === "connecting";
  const activeStatus = inferActiveStage(job, logs);
  const activeStageIndex =
    activeStatus && STAGE_INDEX.has(activeStatus) ? STAGE_INDEX.get(activeStatus)! : -1;
  const allComplete = job?.status === "complete";
  const isStopped = job?.status === "error" || job?.status === "cancelled";
  const statusLabel = translateStageStatus(job?.status ?? "idle", locale, "progress");
  const activeMessage = translateJobMessage(job?.message, locale);
  const showEnrichment = !!enrichmentStats && (
    !!enrichmentStats.arxiv ||
    !!enrichmentStats.semantic_scholar ||
    !!enrichmentStats.web ||
    typeof enrichmentStats.total_findings === "number"
  );
  const activeStage = activeStageIndex >= 0 ? PROGRESS_STAGES[activeStageIndex] : undefined;
  const ActiveIcon = activeStage?.icon ?? Circle;
  const activeStageLabel = activeStage ? translateStageStatus(activeStage.id, locale, "progress") : statusLabel;

  if (compact) {
    const detail = isStopped
      ? (activeMessage || activeStageLabel)
      : activeMessage ?? t("progress.currentAt").replace("{stage}", activeStageLabel);
    return (
      <section className={`panel monitor-panel monitor-panel-compact ${isStopped ? "monitor-panel-stopped" : ""} ${job?.status === "error" ? "monitor-panel-error" : ""}`}>
        <div className={`compact-stage-current ${activeStage && !isStopped && !allComplete ? "compact-stage-current-active" : ""}`}>
          <span className="compact-stage-current-icon">
            <ActiveIcon size={16} />
          </span>
          <div>
            <strong>{activeStageLabel}</strong>
            <HoverTooltip content={detail} className="compact-stage-detail-wrap">
              <span>{detail}</span>
            </HoverTooltip>
          </div>
        </div>
        <ol className="compact-stage-list">
          {PROGRESS_STAGES.map((stage, index) => {
            const Icon = stage.icon;
            const isComplete = allComplete || (activeStageIndex >= 0 && index < activeStageIndex);
            const isActive = activeStageIndex === index;
            return (
              <li
                key={stage.id}
                className={`compact-stage-item ${isComplete ? "compact-stage-complete" : ""} ${isActive ? "compact-stage-active" : ""} ${isActive && isStopped ? "compact-stage-stopped" : ""}`}
              >
                <span><Icon size={13} /></span>
                <em>{translateStageStatus(stage.id, locale, "progress")}</em>
              </li>
            );
          })}
        </ol>
        {showEnrichment ? (
          <EnrichmentSummary stats={enrichmentStats!} />
        ) : null}
      </section>
    );
  }

  return (
    <section className="panel monitor-panel">
      {!hideHeader ? <div className="panel-header-row" style={{ justifyContent: "space-between" }}>
        <div>
          <p className="panel-title">{t("progress.title")}</p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.82rem", color: "var(--muted)" }}>
          <span
            className={`status-dot ${
              isConnected
                ? "status-dot-connected"
                : isConnecting
                ? "status-dot-connecting"
                : "status-dot-disconnected"
            }`}
          />
          {isConnected
            ? t("status.connected")
            : isConnecting
            ? t("status.connecting")
            : t("status.disconnected")}
        </div>
      </div> : null}

      <div className="monitor-metrics">
        <div>
          <span>{t("progress.metricProgress")}</span>
          <strong>{Math.round((job?.progress ?? 0) * 100)}%</strong>
        </div>
        <div>
          <span>{t("progress.metricSlides")}</span>
          <strong>{job?.slides_completed ?? 0}</strong>
        </div>
        <div className="monitor-metric-status">
          <span>{t("progress.metricStatus")}</span>
          <HoverTooltip content={statusLabel} className="monitor-status-value-wrap">
            <strong className="monitor-status-value">{statusLabel}</strong>
          </HoverTooltip>
        </div>
      </div>

      <div className="progress-bar">
        <div className="progress-fill" style={{ width: `${(job?.progress ?? 0) * 100}%` }} />
      </div>

      <ul className="stage-list">
        {PROGRESS_STAGES.map((stage, index) => {
          const isComplete =
            allComplete ||
            (activeStageIndex >= 0 && index < activeStageIndex);
          const isActive = activeStatus === stage.id;
          const Icon = stage.icon;
          const label = translateStageStatus(stage.id, locale, "progress");
          const message = isActive
            ? (activeMessage ?? (locale === "zh" ? "处理中..." : "Processing..."))
            : isComplete
            ? t("progress.ready")
            : t("progress.pending");
          return (
            <li
              key={stage.id}
              className={`stage-item ${isActive ? "stage-active" : ""} ${isComplete ? "stage-complete" : ""}`}
            >
              <div className="stage-item-head">
                <span
                  className="stage-icon"
                  style={{ color: isComplete ? "var(--success)" : isActive ? "var(--accent)" : "var(--muted)" }}
                >
                  <Icon size={15} />
                </span>
                <strong>{label}</strong>
              </div>
              <div className="stage-item-status">
                {isActive && <Loader2 size={14} className="spin" color="var(--accent)" />}
                {isComplete && !isActive && <CheckCircle2 size={14} color="var(--success)" />}
                {!isComplete && !isActive && <Circle size={14} color="var(--muted)" />}
                <HoverTooltip content={message} className="stage-status-tooltip-wrap">
                  <span
                    className="stage-status-text"
                    style={{ color: isComplete ? "var(--success)" : "var(--muted)" }}
                  >
                    {message}
                  </span>
                </HoverTooltip>
              </div>
            </li>
          );
        })}
      </ul>

      {showEnrichment ? (
        <EnrichmentSummary stats={enrichmentStats!} />
      ) : null}
    </section>
  );
}

export function inferActiveStage(job?: JobStatus, logs: string[] = []): string {
  const normalized = normalizeProgressStage(job?.status);
  if (STAGE_INDEX.has(normalized)) {
    return normalized;
  }
  for (let index = logs.length - 1; index >= 0; index -= 1) {
    const match = logs[index]?.match(/^\[([^\]]+)\]/);
    const stage = normalizeProgressStage(match?.[1]);
    if (STAGE_INDEX.has(stage)) {
      return stage;
    }
  }
  const message = (job?.message ?? "").toLowerCase();
  if (message.includes("export")) return "export";
  if (message.includes("repair")) return "generation";
  if (message.includes("visual") || message.includes("critic") || message.includes("qa")) return "generation";
  if (message.includes("svg") || message.includes("slide") || message.includes("generat")) return "generation";
  if (message.includes("design") || message.includes("strateg") || message.includes("content outline")) return "strategy";
  if (message.includes("research") || message.includes("analyz") || message.includes("analysis")) return "research";
  if (message.includes("pars") || message.includes("paper")) return "parsing";
  return "";
}

interface EnrichmentSummaryProps {
  stats: ResearchEnrichmentStats;
}

function EnrichmentSummary({ stats }: EnrichmentSummaryProps) {
  const { t, locale } = useLocale();
  const [expandedSources, setExpandedSources] = useState<Set<string>>(() => new Set());
  const isQuerying = stats.phase === "querying";

  const rows: Array<{
    id: string;
    icon: ComponentType<{ size?: number; style?: CSSProperties }>;
    name: string;
    found?: number;
    error?: string;
    extra?: string;
    findings?: ResearchFinding[];
  }> = [];

  if (stats.arxiv) {
    rows.push({
      id: "arxiv",
      icon: GraduationCap,
      name: t("progress.enrichment.arxiv"),
      found: stats.arxiv.found,
      error: stats.arxiv.error,
      findings: stats.arxiv.findings,
    });
  }
  if (stats.semantic_scholar) {
    rows.push({
      id: "semantic_scholar",
      icon: Sparkles,
      name: t("progress.enrichment.scholar"),
      found: stats.semantic_scholar.found,
      error: stats.semantic_scholar.error,
      findings: stats.semantic_scholar.findings,
    });
  }
  if (stats.web) {
    rows.push({
      id: "web",
      icon: Globe,
      name: t("progress.enrichment.web"),
      found: stats.web.found,
      error: stats.web.error,
      extra: stats.web.provider,
      findings: stats.web.findings,
    });
  }

  const toggleSource = (sourceId: string) => {
    setExpandedSources((current) => {
      const next = new Set(current);
      if (next.has(sourceId)) {
        next.delete(sourceId);
      } else {
        next.add(sourceId);
      }
      return next;
    });
  };

  return (
    <div className="enrichment-summary">
      <p className="enrichment-summary-title">
        {isQuerying ? (
          <Loader2 size={12} className="spin" style={{ marginRight: 4, verticalAlign: "middle" }} />
        ) : (
          <Sparkles size={12} style={{ marginRight: 4, verticalAlign: "middle" }} />
        )}
        {isQuerying
          ? locale === "zh"
            ? "正在查询外部信息源"
            : "Querying external sources"
          : t("progress.enrichment.title")}
      </p>
      <p className="enrichment-summary-subtitle">
        {locale === "zh"
          ? "用于辅助论文分析；失败或跳过不会中断 PPT 生成。"
          : "Used to support paper analysis. Failures or skips do not stop generation."}
      </p>
      {rows.length === 0 ? (
        <p className="enrichment-summary-empty">{t("progress.enrichment.empty")}</p>
      ) : (
        <ul className="enrichment-summary-list">
          {rows.map((row) => {
            const Icon = row.icon;
            const nameFull = row.name + (row.extra ? ` · ${row.extra}` : "");
            const findings = row.findings ?? [];
            const canExpand = !isQuerying && !row.error && findings.length > 0;
            const isExpanded = expandedSources.has(row.id);
            const rowState = getEnrichmentRowState({
              error: row.error,
              found: row.found,
              isQuerying,
              locale,
              sourceName: row.name,
              hasExpandableFindings: canExpand,
            });
            return (
              <li key={row.id} className={`enrichment-source-block ${isExpanded ? "enrichment-source-expanded" : ""}`}>
                <div className={`enrichment-summary-row enrichment-row-${rowState.kind}`}>
                  <div className="enrichment-row-main">
                    <div className="enrichment-row-head">
                      <HoverTooltip content={nameFull} className="enrichment-row-name-wrap">
                        <span className="enrichment-row-name">
                          <Icon size={12} style={{ marginRight: 4, verticalAlign: "middle" }} />
                          {row.name}
                          {row.extra ? <em className="enrichment-row-extra"> · {row.extra}</em> : null}
                        </span>
                      </HoverTooltip>
                      <span className={`enrichment-row-badge enrichment-row-badge-${rowState.kind}`}>
                        {rowState.kind === "querying" ? <Loader2 size={11} className="spin" /> : null}
                        {rowState.kind === "success" ? <CheckCircle2 size={11} /> : null}
                        {rowState.kind === "empty" ? <Circle size={11} /> : null}
                        {rowState.kind === "skipped" || rowState.kind === "error" ? <AlertCircle size={11} /> : null}
                        {rowState.label}
                      </span>
                    </div>
                    <HoverTooltip content={rowState.tooltip ?? rowState.detail} className="enrichment-row-detail-wrap">
                      <span className="enrichment-row-detail">{rowState.detail}</span>
                    </HoverTooltip>
                  </div>
                  <div className="enrichment-row-actions">
                    {canExpand ? (
                      <button
                        type="button"
                        className="enrichment-toggle-btn"
                        onClick={() => toggleSource(row.id)}
                        aria-label={isExpanded ? "collapse" : "expand"}
                        aria-expanded={isExpanded}
                      >
                        {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                      </button>
                    ) : null}
                  </div>
                </div>
                {isExpanded ? (
                  <ul className="enrichment-findings-list">
                    {findings.map((finding, i) => (
                      <li key={`${row.id}-${i}`} className="enrichment-finding-item">
                        <span className="enrichment-finding-source">{formatFindingSource(finding.source)}</span>
                        <span className="enrichment-finding-title">
                          {finding.url ? <a href={finding.url} target="_blank" rel="noopener noreferrer">{finding.title}</a> : finding.title}
                        </span>
                        <span className="enrichment-finding-meta">
                          {finding.year ?? ""}{finding.citation_count != null ? ` · ${finding.citation_count} cit` : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      {typeof stats.total_findings === "number" && stats.total_findings > 0 ? (
        <p className="enrichment-summary-total">
          <span className="enrichment-total-icon-text">
            <CheckCircle2 size={11} style={{ color: "var(--success)" }} />
            {locale === "zh"
              ? `共 ${stats.total_findings} 条相关信息已用于内容分析`
              : `${stats.total_findings} findings used for content analysis`}
          </span>
        </p>
      ) : null}
    </div>
  );
}

function formatFindingSource(source: string): string {
  if (source === "semantic_scholar") return "S2";
  if (source === "arxiv") return "ArX";
  return "Web";
}

type EnrichmentRowKind = "querying" | "success" | "empty" | "skipped" | "error";

interface EnrichmentRowStateInput {
  error?: string;
  found?: number;
  isQuerying: boolean;
  locale: "zh" | "en";
  sourceName: string;
  hasExpandableFindings: boolean;
}

interface EnrichmentRowState {
  kind: EnrichmentRowKind;
  label: string;
  detail: string;
  tooltip?: string;
}

function getEnrichmentRowState(input: EnrichmentRowStateInput): EnrichmentRowState {
  const { error, found = 0, isQuerying, locale, sourceName, hasExpandableFindings } = input;
  if (isQuerying) {
    return {
      kind: "querying",
      label: locale === "zh" ? "查询中" : "Querying",
      detail: locale === "zh" ? `正在检索 ${sourceName}...` : `Searching ${sourceName}...`,
    };
  }
  if (error) {
    return translateEnrichmentError(error, locale, sourceName);
  }
  if (found > 0) {
    return {
      kind: "success",
      label: locale === "zh" ? `${found} 条` : `${found}`,
      detail: hasExpandableFindings
        ? locale === "zh"
          ? "已找到相关信息，可展开查看"
          : "Findings available. Expand to inspect."
        : locale === "zh"
        ? "已找到相关信息，已用于内容分析"
        : "Findings were used for content analysis.",
    };
  }
  return {
    kind: "empty",
    label: locale === "zh" ? "0 条" : "0",
    detail: locale === "zh" ? "没有找到可用的相关信息" : "No usable findings returned.",
  };
}

function translateEnrichmentError(err: string, locale: "zh" | "en", sourceName: string): EnrichmentRowState {
  const map: Record<string, { kind: "skipped" | "error"; zhLabel: string; enLabel: string; zhDetail: string; enDetail: string }> = {
    no_extractable_terms: {
      kind: "skipped",
      zhLabel: "已跳过",
      enLabel: "Skipped",
      zhDetail: "标题里没有足够检索关键词，未查询此来源",
      enDetail: "Not enough searchable terms in the title.",
    },
    package_missing: {
      kind: "skipped",
      zhLabel: "已跳过",
      enLabel: "Skipped",
      zhDetail: "当前环境未启用这个信息源，已跳过",
      enDetail: "This source is not available in the current environment.",
    },
    no_api_key: {
      kind: "skipped",
      zhLabel: "缺少密钥",
      enLabel: "No key",
      zhDetail: "未配置 API Key，已跳过这个信息源",
      enDetail: "No API key was configured for this source.",
    },
    no_title: {
      kind: "skipped",
      zhLabel: "已跳过",
      enLabel: "Skipped",
      zhDetail: "缺少论文标题，未查询这个信息源",
      enDetail: "Paper title is missing.",
    },
    httpx_missing: {
      kind: "skipped",
      zhLabel: "已跳过",
      enLabel: "Skipped",
      zhDetail: "当前环境未启用网页请求组件，已跳过",
      enDetail: "The web request dependency is unavailable.",
    },
    timeout: {
      kind: "error",
      zhLabel: "超时",
      enLabel: "Timeout",
      zhDetail: `${sourceName} 响应超时，已跳过，不影响生成`,
      enDetail: `${sourceName} timed out and was skipped.`,
    },
    rate_limited: {
      kind: "error",
      zhLabel: "请求受限",
      enLabel: "Limited",
      zhDetail: `${sourceName} 请求受限，已跳过，不影响生成`,
      enDetail: `${sourceName} was rate limited and skipped.`,
    },
    query_failed: {
      kind: "error",
      zhLabel: "查询失败",
      enLabel: "Failed",
      zhDetail: `${sourceName} 查询失败，已跳过，不影响生成`,
      enDetail: `${sourceName} failed and was skipped.`,
    },
  };
  if (map[err]) {
    const item = map[err];
    return {
      kind: item.kind,
      label: locale === "zh" ? item.zhLabel : item.enLabel,
      detail: locale === "zh" ? item.zhDetail : item.enDetail,
    };
  }

  const lower = err.toLowerCase();
  const looksLikeTimeout = lower.includes("timeout") || lower.includes("timed out");
  const looksLikeRateLimit = lower.includes("rate") || lower.includes("429");
  const detail = looksLikeTimeout
    ? locale === "zh"
      ? `${sourceName} 响应超时，已跳过，不影响生成`
      : `${sourceName} timed out and was skipped.`
    : looksLikeRateLimit
    ? locale === "zh"
      ? `${sourceName} 请求受限，已跳过，不影响生成`
      : `${sourceName} was rate limited and skipped.`
    : locale === "zh"
    ? `${sourceName} 查询失败，已跳过，不影响生成`
    : `${sourceName} failed and was skipped.`;

  return {
    kind: "error",
    label: locale === "zh" ? "查询失败" : "Failed",
    detail,
    tooltip: `${detail}${locale === "zh" ? "。技术详情：" : " Technical detail: "}${truncateEnrichmentError(err)}`,
  };
}

function truncateEnrichmentError(err: string): string {
  return err.length > 120 ? `${err.slice(0, 120)}...` : err;
}
