import { useCallback, useState } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, ChevronDown, ChevronRight, Eye, Loader2, Terminal, X } from "lucide-react";
import { useLocale } from "../../i18n";
import { translateLogLine } from "../../lib/i18nStatus";
import type { CriticEvent } from "../../lib/types";

function buildArchiveUrl(archivePath: string, jobId?: string): string | null {
  if (!jobId || !archivePath) return null;
  const filename = archivePath.split("/").pop();
  if (!filename) return null;
  return `/api/critic-archive/${jobId}/${filename}`;
}

function formatRawResponse(raw?: string | null): string {
  if (!raw) return "";
  const trimmed = raw.trim();
  const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const candidate = fenced?.[1] ?? trimmed.match(/\{[\s\S]*\}/)?.[0] ?? trimmed;
  try {
    return JSON.stringify(JSON.parse(candidate), null, 2);
  } catch {
    return raw;
  }
}

interface AgentLogProps {
  logs?: string[];
  criticEvents?: CriticEvent[];
  jobId?: string;
  mode?: "logs" | "critic" | "mixed";
  hideHeader?: boolean;
}

export function AgentLog({ logs, criticEvents, jobId, mode = "mixed", hideHeader = false }: AgentLogProps) {
  const { t, locale } = useLocale();
  const safeLogs = Array.isArray(logs) ? logs : [];
  const safeCritic = Array.isArray(criticEvents) ? criticEvents : [];
  const showLogs = mode === "logs" || mode === "mixed";
  const showCritic = mode === "critic" || mode === "mixed";
  const summary =
    mode === "critic"
      ? safeCritic.length === 0
        ? t("review.emptyShort")
        : t("review.summary").replace("{count}", String(safeCritic.length))
      : safeLogs.length === 0
        ? t("log.summaryEmpty")
        : `${safeLogs.length} ${t("log.summaryCount")}`;

  const dedicatedCritic = mode === "critic";
  const [criticOpen, setCriticOpen] = useState(dedicatedCritic);
  const [expandedPages, setExpandedPages] = useState<Set<number>>(new Set());
  const [expandedPrompts, setExpandedPrompts] = useState<Set<string>>(new Set());
  const [archivePreview, setArchivePreview] = useState<{ url: string; label: string; content: string } | null>(null);
  const [comparePreview, setComparePreview] = useState<{
    label: string;
    before: { url: string; content: string };
    after: { url: string; content: string };
  } | null>(null);
  const [imagePreview, setImagePreview] = useState<{ url: string; label: string } | null>(null);
  const [archiveLoadingKey, setArchiveLoadingKey] = useState<string | null>(null);

  const fetchArchiveContent = useCallback(async (archivePath: string) => {
    if (!jobId) return null;
    const url = buildArchiveUrl(archivePath, jobId);
    if (!url) return null;
    const res = await fetch(url);
    if (!res.ok) return null;
    return { url, content: await res.text() };
  }, [jobId]);

  const openArchivePreview = useCallback(async (archivePath: string, label: string) => {
    setArchiveLoadingKey(archivePath);
    try {
      const payload = await fetchArchiveContent(archivePath);
      if (!payload) return;
      setArchivePreview({ label, ...payload });
    } catch {
      // ignore
    } finally {
      setArchiveLoadingKey(null);
    }
  }, [fetchArchiveContent]);

  const openArchiveComparison = useCallback(async (beforePath: string, afterPath: string, label: string) => {
    const key = `${beforePath}|${afterPath}`;
    setArchiveLoadingKey(key);
    try {
      const [before, after] = await Promise.all([
        fetchArchiveContent(beforePath),
        fetchArchiveContent(afterPath),
      ]);
      if (!before || !after) return;
      setComparePreview({ label, before, after });
    } catch {
      // ignore
    } finally {
      setArchiveLoadingKey(null);
    }
  }, [fetchArchiveContent]);

  const openImagePreview = useCallback((archivePath: string, label: string) => {
    const url = buildArchiveUrl(archivePath, jobId);
    if (!url) return;
    setImagePreview({ url, label });
  }, [jobId]);

  const togglePage = (page: number) => {
    setExpandedPages((prev) => {
      const next = new Set(prev);
      if (next.has(page)) {
        next.delete(page);
      } else {
        next.add(page);
      }
      return next;
    });
  };

  const togglePrompt = (key: string) => {
    setExpandedPrompts((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  // Group critic events by page
  const criticByPage = new Map<number, CriticEvent[]>();
  for (const ev of safeCritic) {
    const existing = criticByPage.get(ev.page) ?? [];
    existing.push(ev);
    criticByPage.set(ev.page, existing);
  }
  const sortedPages = Array.from(criticByPage.keys()).sort((a, b) => a - b);

  const totalErrors = safeCritic.reduce((sum, ev) => sum + ev.report.error_count, 0);
  const totalWarnings = safeCritic.reduce((sum, ev) => sum + ev.report.warning_count, 0);

  return (
    <>
    <section className="panel">
      {!hideHeader ? <div className="panel-header-row">
        <div>
          <div className="panel-title-row">
            {mode === "critic" ? <AlertTriangle size={15} className="panel-title-icon" /> : <Terminal size={15} className="panel-title-icon" />}
            <p className="panel-title">{mode === "critic" ? t("monitor.review") : t("log.title")}</p>
          </div>
          <p className="panel-support-text">{summary}</p>
        </div>
      </div> : null}
      {showLogs ? <div className="log-console">
        {safeLogs.length === 0 ? <p className="muted-copy">{t("log.empty")}</p> : null}
        {safeLogs.map((log, index) => (
          <p key={`${log}-${index}`} className="log-line">{translateLogLine(log, locale)}</p>
        ))}
      </div> : null}
      {showCritic && safeCritic.length === 0 ? (
        <div className="critic-empty-state">
          <AlertTriangle size={20} />
          <strong>{t("monitor.review")}</strong>
          <span>{t("review.empty")}</span>
        </div>
      ) : null}
      {showCritic && safeCritic.length > 0 ? (
        <div className="critic-section">
          {dedicatedCritic ? (
            <div className="critic-overview">
              <span>{t("review.summary").replace("{count}", String(safeCritic.length))}</span>
              <span className="critic-badge critic-badge-error">{t("review.errors").replace("{count}", String(totalErrors))}</span>
              <span className="critic-badge critic-badge-warn">{t("review.warnings").replace("{count}", String(totalWarnings))}</span>
            </div>
          ) : (
            <button
              type="button"
              className="critic-toggle"
              onClick={() => setCriticOpen((v) => !v)}
            >
              {criticOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              <AlertTriangle size={14} />
              <span>{t("monitor.review")}</span>
              <span className="critic-badge critic-badge-error">{totalErrors}</span>
              <span className="critic-badge critic-badge-warn">{totalWarnings}</span>
            </button>
          )}
          {criticOpen ? (
            <div className="critic-list">
              {sortedPages.map((page) => {
                const events = criticByPage.get(page) ?? [];
                const lastEvent = events[events.length - 1];
                const passed = lastEvent?.report.passed ?? true;
                const pageErrors = events.reduce((s, e) => s + e.report.error_count, 0);
                const pageWarnings = events.reduce((s, e) => s + e.report.warning_count, 0);
                const isExpanded = expandedPages.has(page);

                return (
                  <div key={page} className="critic-page">
                    <button
                      type="button"
                      className="critic-page-header"
                      onClick={() => togglePage(page)}
                    >
                      {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                      <span className="critic-page-label">{t("review.page").replace("{page}", String(page))}</span>
                      <span className={`critic-status ${passed ? "critic-status-pass" : "critic-status-fail"}`}>
                        {passed ? t("review.pass") : t("review.fail")}
                      </span>
                      <span className="critic-attempts">{t("review.attempts").replace("{count}", String(events.length))}</span>
                      {pageErrors > 0 ? <span className="critic-badge critic-badge-error">{t("review.errors").replace("{count}", String(pageErrors))}</span> : null}
                      {pageWarnings > 0 ? <span className="critic-badge critic-badge-warn">{t("review.warnings").replace("{count}", String(pageWarnings))}</span> : null}
                    </button>
                    {isExpanded ? (
                      <div className="critic-attempts-list">
                        {events.map((ev, idx) => {
                          const source = ev.source ?? "static";
                          const sourceLabel = source === "visual" ? t("review.sourceVisual") : t("review.sourceStatic");
                          const beforeArchive = ev.before_archive_path ?? ev.archive_path;
                          const afterArchive = ev.after_archive_path;
                          const renderedImagePath = ev.rendered_image_path;
                          const compareKey = beforeArchive && afterArchive ? `${beforeArchive}|${afterArchive}` : "";
                          const singleArchivePath = afterArchive ?? beforeArchive;
                          const svgActionLoadingKey = compareKey || singleArchivePath || "";
                          return (
                          <div key={`${ev.page}-${ev.attempt}-${source}-${idx}`} className="critic-attempt">
                            <div className="critic-attempt-head">
                              <p className="critic-attempt-label">{t("review.attempt").replace("{count}", String(ev.attempt))}</p>
                              <span className={`critic-badge critic-source-badge critic-source-${source}`}>{sourceLabel}</span>
                              {ev.skipped_reason ? (
                                <span className="critic-badge critic-source-badge">{ev.skipped_reason}</span>
                              ) : null}
                              {ev.report.error_count > 0 ? <span className="critic-badge critic-badge-error">{t("review.errors").replace("{count}", String(ev.report.error_count))}</span> : null}
                              {ev.report.warning_count > 0 ? <span className="critic-badge critic-badge-warn">{t("review.warnings").replace("{count}", String(ev.report.warning_count))}</span> : null}
                            </div>
                            {ev.report.violations.length === 0 ? (
                              <p className="critic-no-violations">{t("review.noViolations")}</p>
                            ) : (
                              ev.report.violations.map((v, vi) => (
                                <div key={vi} className={`critic-violation critic-violation-${v.severity}`}>
                                  <span className="critic-violation-severity">{v.severity.toUpperCase()}</span>
                                  <span className="critic-violation-rule">{v.rule}</span>
                                  {v.element ? <span className="critic-violation-element">{v.element}</span> : null}
                                  <p className="critic-violation-detail">{v.detail}</p>
                                </div>
                              ))
                            )}
                            {ev.repair_prompt ? (
                              <div className="critic-repair">
                                <button
                                  type="button"
                                  className="critic-repair-toggle"
                                  onClick={() => togglePrompt(`${ev.page}-${ev.attempt}`)}
                                >
                                  {expandedPrompts.has(`${ev.page}-${ev.attempt}`) ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
                                  {t("review.repairPrompt")}
                                </button>
                                {expandedPrompts.has(`${ev.page}-${ev.attempt}`) ? (
                                  <pre className="critic-repair-text">{ev.repair_prompt}</pre>
                                ) : null}
                              </div>
                            ) : null}
                            {ev.raw_response_excerpt ? (
                              <details className="critic-meta-details">
                                <summary>{t("review.rawResponse")}</summary>
                                <pre className="critic-repair-text">{formatRawResponse(ev.raw_response_excerpt)}</pre>
                              </details>
                            ) : null}
                            {renderedImagePath || singleArchivePath ? (
                              <div className="critic-archive-actions">
                                {renderedImagePath ? (
                                  <button
                                    type="button"
                                    className="critic-archive-link"
                                    onClick={() => openImagePreview(renderedImagePath, t("review.visualRenderImageLabel").replace("{page}", String(ev.page)).replace("{attempt}", String(ev.attempt)))}
                                  >
                                    <Eye size={11} />
                                    {t("review.visualRenderImage")}
                                  </button>
                                ) : null}
                                {singleArchivePath ? (
                                  <button
                                    type="button"
                                    className="critic-archive-link"
                                    onClick={() => {
                                      const label = t("review.compareArchiveLabel").replace("{page}", String(ev.page)).replace("{attempt}", String(ev.attempt));
                                      if (beforeArchive && afterArchive) {
                                        void openArchiveComparison(beforeArchive, afterArchive, label);
                                      } else {
                                        void openArchivePreview(singleArchivePath, label);
                                      }
                                    }}
                                  >
                                    {archiveLoadingKey === svgActionLoadingKey ? <Loader2 size={11} className="spin" /> : <Eye size={11} />}
                                    {t("review.repairSvgSnapshot")}
                                  </button>
                                ) : null}
                              </div>
                            ) : null}
                          </div>
                        )})}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
    {archivePreview ? createPortal(
      <div className="svg-preview-overlay" onClick={() => setArchivePreview(null)}>
        <div className="svg-preview-panel" onClick={(e) => e.stopPropagation()}>
          <div className="svg-preview-header">
            <span className="svg-preview-title">{archivePreview.label}</span>
            <button type="button" className="icon-btn" onClick={() => setArchivePreview(null)}>
              <X size={16} />
            </button>
          </div>
          <div className="svg-preview-content" dangerouslySetInnerHTML={{ __html: archivePreview.content }} />
        </div>
      </div>,
      document.body,
    ) : null}
    {comparePreview ? createPortal(
      <div className="svg-preview-overlay" onClick={() => setComparePreview(null)}>
        <div className="svg-preview-panel svg-preview-panel-compare" onClick={(e) => e.stopPropagation()}>
          <div className="svg-preview-header">
            <span className="svg-preview-title">{comparePreview.label}</span>
            <button type="button" className="icon-btn" onClick={() => setComparePreview(null)}>
              <X size={16} />
            </button>
          </div>
          <div className="svg-preview-compare">
            <div className="svg-preview-column">
              <span>{t("review.preRepairSvgColumn")}</span>
              <div className="svg-preview-content" dangerouslySetInnerHTML={{ __html: comparePreview.before.content }} />
            </div>
            <div className="svg-preview-column">
              <span>{t("review.postRepairSvgColumn")}</span>
              <div className="svg-preview-content" dangerouslySetInnerHTML={{ __html: comparePreview.after.content }} />
            </div>
          </div>
        </div>
      </div>,
      document.body,
    ) : null}
    {imagePreview ? createPortal(
      <div className="svg-preview-overlay" onClick={() => setImagePreview(null)}>
        <div className="svg-preview-panel svg-preview-panel-image" onClick={(e) => e.stopPropagation()}>
          <div className="svg-preview-header">
            <span className="svg-preview-title">{imagePreview.label}</span>
            <button type="button" className="icon-btn" onClick={() => setImagePreview(null)}>
              <X size={16} />
            </button>
          </div>
          <div className="svg-preview-image-content">
            <img src={imagePreview.url} alt={imagePreview.label} />
          </div>
        </div>
      </div>,
      document.body,
    ) : null}
    </>
  );
}
