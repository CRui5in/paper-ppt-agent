import { AlertCircle, CheckCircle2, ChevronDown, ChevronRight, Circle, Download, FileSearch, Globe, GraduationCap, Loader2, Search, Settings, Sparkles, Target, Wand2 } from "lucide-react";
import { type ComponentType, type CSSProperties, useState } from "react";
import type { JobStatus, ResearchEnrichmentStats, ResearchFinding } from "../../lib/types";
import { useLocale } from "../../i18n";
import { normalizeProgressStage, translateJobMessage, translateStageStatus } from "../../lib/i18nStatus";
import { HoverTooltip } from "../common/HoverTooltip";

const STAGES: Array<{ id: string; icon: ComponentType<{ size?: number; color?: string }> }> = [
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
}

const STAGE_INDEX = new Map(STAGES.map((stage, index) => [stage.id, index]));

export function ProgressPanel({ job, connectionStatus, enrichmentStats }: ProgressPanelProps) {
  const { t, locale } = useLocale();
  const isConnected = connectionStatus === "connected";
  const isConnecting = connectionStatus === "connecting";
  const activeStatus = normalizeProgressStage(job?.status);
  const activeStageIndex =
    activeStatus && STAGE_INDEX.has(activeStatus) ? STAGE_INDEX.get(activeStatus)! : -1;
  const allComplete = job?.status === "complete";
  const statusLabel = translateStageStatus(job?.status ?? "idle", locale, "progress");
  const activeMessage = translateJobMessage(job?.message, locale);
  const showEnrichment = !!enrichmentStats && (
    !!enrichmentStats.arxiv ||
    !!enrichmentStats.semantic_scholar ||
    !!enrichmentStats.web ||
    typeof enrichmentStats.total_findings === "number"
  );

  return (
    <section className="panel monitor-panel">
      <div className="panel-header-row" style={{ justifyContent: "space-between" }}>
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
            ? locale === "zh" ? "已连接" : "Connected"
            : isConnecting
            ? locale === "zh" ? "连接中" : "Connecting"
            : locale === "zh" ? "未连接" : "Offline"}
        </div>
      </div>

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
        {STAGES.map((stage, index) => {
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

interface EnrichmentSummaryProps {
  stats: ResearchEnrichmentStats;
}

function EnrichmentSummary({ stats }: EnrichmentSummaryProps) {
  const { t, locale } = useLocale();
  const [expanded, setExpanded] = useState(false);

  const rows: Array<{
    icon: ComponentType<{ size?: number; style?: CSSProperties }>;
    name: string;
    found?: number;
    error?: string;
    extra?: string;
    findings?: ResearchFinding[];
  }> = [];

  if (stats.arxiv) {
    rows.push({
      icon: GraduationCap,
      name: t("progress.enrichment.arxiv"),
      found: stats.arxiv.found,
      error: stats.arxiv.error,
      findings: stats.arxiv.findings,
    });
  }
  if (stats.semantic_scholar) {
    rows.push({
      icon: Sparkles,
      name: t("progress.enrichment.scholar"),
      found: stats.semantic_scholar.found,
      error: stats.semantic_scholar.error,
      findings: stats.semantic_scholar.findings,
    });
  }
  if (stats.web) {
    rows.push({
      icon: Globe,
      name: t("progress.enrichment.web"),
      found: stats.web.found,
      error: stats.web.error,
      extra: stats.web.provider,
      findings: stats.web.findings,
    });
  }

  // Collect all findings for the expandable section
  const allFindings: ResearchFinding[] = rows.flatMap((r) => r.findings ?? []);
  const hasFindings = allFindings.length > 0;

  return (
    <div className="enrichment-summary">
      <p className="enrichment-summary-title">
        <Sparkles size={12} style={{ marginRight: 4, verticalAlign: "middle" }} />
        {t("progress.enrichment.title")}
      </p>
      {rows.length === 0 ? (
        <p className="enrichment-summary-empty">{t("progress.enrichment.empty")}</p>
      ) : (
        <ul className="enrichment-summary-list">
          {rows.map((row, idx) => {
            const Icon = row.icon;
            const hasError = !!row.error;
            const nameFull = row.name + (row.extra ? ` · ${row.extra}` : "");
            const statusText = stats.phase === "querying"
              ? (locale === "zh" ? "查询中..." : "querying...")
              : hasError
              ? translateEnrichmentError(row.error!, locale)
              : `${row.found ?? 0} ${t("progress.enrichment.found")}`;
            return (
              <li key={idx} className={`enrichment-summary-row ${hasError ? "enrichment-row-warn" : ""}`}>
                <HoverTooltip content={nameFull} className="enrichment-row-name-wrap">
                  <span className="enrichment-row-name">
                    <Icon size={12} style={{ marginRight: 4, verticalAlign: "middle" }} />
                    {row.name}
                    {row.extra ? <em className="enrichment-row-extra"> · {row.extra}</em> : null}
                  </span>
                </HoverTooltip>
                <HoverTooltip content={statusText} className="enrichment-row-status-wrap">
                  {hasError ? (
                    <span className="enrichment-row-status enrichment-row-status-warn">
                      <AlertCircle size={11} style={{ marginRight: 4, verticalAlign: "middle" }} />
                      {statusText}
                    </span>
                  ) : (
                    <span className="enrichment-row-status">{statusText}</span>
                  )}
                </HoverTooltip>
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
              ? `共 ${stats.total_findings} 条相关信息已注入 Pass 1`
              : `${stats.total_findings} findings injected into Pass 1`}
          </span>
          {hasFindings ? (
            <button
              className="enrichment-toggle-btn"
              onClick={() => setExpanded((v) => !v)}
              aria-label={expanded ? "collapse" : "expand"}
            >
              {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            </button>
          ) : null}
        </p>
      ) : null}
      {expanded && hasFindings ? (
        <ul className="enrichment-findings-list">
          {allFindings.map((f, i) => (
            <li key={i} className="enrichment-finding-item">
              <span className="enrichment-finding-source">{f.source === "semantic_scholar" ? "S2" : f.source === "arxiv" ? "ArX" : "Web"}</span>
              <span className="enrichment-finding-title">
                {f.url ? <a href={f.url} target="_blank" rel="noopener noreferrer">{f.title}</a> : f.title}
              </span>
              <span className="enrichment-finding-meta">
                {f.year ?? ""}{f.citation_count != null ? ` · ${f.citation_count} cit` : ""}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function translateEnrichmentError(err: string, locale: "zh" | "en"): string {
  const map: Record<string, { zh: string; en: string }> = {
    no_extractable_terms: { zh: "标题无可提取术语，已跳过", en: "no extractable terms — skipped" },
    package_missing: { zh: "依赖未安装", en: "package missing" },
    no_api_key: { zh: "未配置 API Key", en: "no API key" },
    no_title: { zh: "缺少论文标题", en: "no paper title" },
    httpx_missing: { zh: "依赖 httpx 未安装", en: "httpx missing" },
  };
  if (map[err]) {
    return locale === "zh" ? map[err].zh : map[err].en;
  }
  // Truncate long stack-style errors so they don't blow out the panel.
  return err.length > 60 ? `${err.slice(0, 60)}…` : err;
}
