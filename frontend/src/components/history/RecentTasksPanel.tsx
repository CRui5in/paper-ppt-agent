import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { Check, ChevronDown, Loader2, MessageSquareText, Trash2 } from "lucide-react";
import { Badge } from "../ui/badge";
import { fetchProjectPreview } from "../../lib/api";
import type { GenerationHistoryItem, PreviewSlide } from "../../lib/types";
import { useGeneration } from "../../hooks/useGeneration";
import { useLocale } from "../../i18n";
import { translateStageStatus } from "../../lib/i18nStatus";
import { DeleteConfirmTooltip } from "../common/DeleteConfirmTooltip";

interface RecentTasksPanelProps {
  history: GenerationHistoryItem[];
  runs: ReturnType<typeof useGeneration.getState>["runs"];
  locale: "en" | "zh";
  currentJobId?: string;
  limit?: number;
}

export function RecentTasksPanel({
  history,
  runs,
  locale,
  currentJobId,
  limit,
}: RecentTasksPanelProps) {
  const { t } = useLocale();
  const removeHistory = useGeneration((state) => state.removeHistory);
  const refreshHistoryStatuses = useGeneration((state) => state.refreshHistoryStatuses);
  const [collapsed, setCollapsed] = useState(false);
  const [navigatingJobId, setNavigatingJobId] = useState<string | null>(null);
  const bulkDeleteRef = useRef<HTMLButtonElement | null>(null);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedJobIds, setSelectedJobIds] = useState<Set<string>>(new Set());
  const [confirmBulkDelete, setConfirmBulkDelete] = useState(false);
  const [deletingSelected, setDeletingSelected] = useState(false);
  const recentTasks = typeof limit === "number" ? history.slice(0, limit) : history;
  const [historyPreviews, setHistoryPreviews] = useState<Record<string, PreviewSlide | null | undefined>>({});
  const [hoveredTask, setHoveredTask] = useState<{ task: GenerationHistoryItem; rect: DOMRect } | null>(null);
  const previewRequestsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    const validIds = new Set(recentTasks.map((task) => task.jobId));
    setSelectedJobIds((current) => {
      const next = new Set([...current].filter((jobId) => validIds.has(jobId)));
      if (next.size === 0) {
        setSelectionMode(false);
      }
      return next;
    });
  }, [recentTasks]);

  const ensurePreview = (task: GenerationHistoryItem) => {
    if (!task.projectDir || task.jobId in historyPreviews || previewRequestsRef.current.has(task.jobId)) {
      return;
    }
    previewRequestsRef.current.add(task.jobId);
    fetchProjectPreview(task.projectDir, { lastSlideOnly: true })
      .then((preview) => {
        setHistoryPreviews((current) => ({
          ...current,
          [task.jobId]: preview.slides[preview.slides.length - 1] ?? null,
        }));
      })
      .catch(() => {
        setHistoryPreviews((current) => ({
          ...current,
          [task.jobId]: null,
        }));
      })
      .finally(() => {
        previewRequestsRef.current.delete(task.jobId);
      });
  };

  const showTaskPreview = (task: GenerationHistoryItem, rect: DOMRect) => {
    ensurePreview(task);
    setHoveredTask({ task, rect });
  };

  const toggleSelectedTask = (jobId: string) => {
    setSelectionMode(true);
    setSelectedJobIds((current) => {
      const next = new Set(current);
      if (next.has(jobId)) {
        next.delete(jobId);
      } else {
        next.add(jobId);
      }
      if (next.size === 0) {
        setSelectionMode(false);
      }
      return next;
    });
  };

  return (
    <section className={`recent-tasks-panel rounded-lg border border-border bg-card ${collapsed ? "recent-tasks-panel-collapsed" : ""}`}>
      <div className="workspace-panel-header recent-tasks-header">
        <div className="recent-tasks-title">
          <div className="workspace-panel-title">
            <MessageSquareText size={17} />
            <span>{t("recent.title")}</span>
          </div>
        </div>
        {selectionMode ? (
          <button
            ref={bulkDeleteRef}
            type="button"
            className="recent-task-bulk-delete"
            disabled={selectedJobIds.size === 0 || deletingSelected}
            onClick={() => setConfirmBulkDelete(true)}
          >
            {deletingSelected ? <Loader2 size={13} className="spin" /> : <Trash2 size={13} />}
            {t("sidebar.deleteSelected")} ({selectedJobIds.size})
          </button>
        ) : null}
        <button
          type="button"
          className="recent-tasks-toggle"
          onClick={() => setCollapsed((value) => !value)}
          aria-expanded={!collapsed}
        >
          <ChevronDown size={16} />
        </button>
      </div>
      <div className="recent-task-list">
        {recentTasks.length > 0 ? recentTasks.map((task) => {
          const selected = selectedJobIds.has(task.jobId);
          return (
          <div
            className={`recent-task-row-shell ${task.jobId === currentJobId ? "recent-task-row-shell-active" : ""} ${selectionMode ? "recent-task-row-selecting" : ""} ${selected ? "recent-task-row-selected" : ""}`}
            key={task.jobId}
          >
            <button
              type="button"
              className="recent-task-select"
              aria-label={selected ? t("versions.close") : t("sidebar.deleteSelected")}
              aria-pressed={selected}
              onClick={() => toggleSelectedTask(task.jobId)}
            >
              {selected ? <Check size={13} /> : null}
            </button>
            <Link
              className={`recent-task-row ${task.jobId === currentJobId ? "recent-task-row-active" : ""}`}
              to={getHistoryTarget(task)}
              onClick={(event) => {
                if (selectionMode) {
                  event.preventDefault();
                  toggleSelectedTask(task.jobId);
                  return;
                }
                setNavigatingJobId(task.jobId);
              }}
              onMouseEnter={(event) => showTaskPreview(task, event.currentTarget.getBoundingClientRect())}
              onMouseLeave={() => setHoveredTask(null)}
              onFocus={(event) => showTaskPreview(task, event.currentTarget.getBoundingClientRect())}
              onBlur={() => setHoveredTask(null)}
            >
              <span>
                <strong>{task.fileName}</strong>
                <em>{task.slideCount || 0} {locale === "zh" ? "页" : "slides"} · {formatTaskTime(task.createdAt ?? task.updatedAt, locale)}</em>
              </span>
              <Badge
                className="recent-task-status-badge"
                variant={task.status === "error" ? "destructive" : task.status === "complete" ? "success" : task.status === "cancelled" ? "muted" : "default"}
              >
                {navigatingJobId === task.jobId ? <Loader2 size={11} className="spin" /> : null}
              {task.status === "complete" ? t("recent.completed") : translateStageStatus(task.status, locale, "history")}
            </Badge>
            </Link>
          </div>
          );
        }) : (
          <div className="recent-task-empty">{t("recent.empty")}</div>
        )}
      </div>
      {confirmBulkDelete ? (
        <DeleteConfirmTooltip
          anchorRef={bulkDeleteRef}
          message={t("sidebar.confirmDeleteFiles")}
          confirmLabel={t("versions.delete")}
          cancelLabel={t("versions.close")}
          loading={deletingSelected}
          onCancel={() => setConfirmBulkDelete(false)}
          onConfirm={async () => {
            const ids = [...selectedJobIds];
            setDeletingSelected(true);
            try {
              await Promise.all(ids.map((jobId) => removeHistory(jobId)));
              await refreshHistoryStatuses().catch(() => undefined);
              setSelectedJobIds(new Set());
              setSelectionMode(false);
            } finally {
              setDeletingSelected(false);
              setConfirmBulkDelete(false);
            }
          }}
        />
      ) : null}
      {hoveredTask && !collapsed
        ? createPortal(
            <RecentTaskPopover
              task={hoveredTask.task}
              run={runs[hoveredTask.task.jobId]}
              preview={historyPreviews[hoveredTask.task.jobId]}
              previewLoading={Boolean(hoveredTask.task.projectDir && historyPreviews[hoveredTask.task.jobId] === undefined)}
              locale={locale}
              rect={hoveredTask.rect}
            />,
            document.body,
          )
        : null}
    </section>
  );
}

function RecentTaskPopover({
  task,
  run,
  preview,
  previewLoading,
  locale,
  rect,
}: {
  task: GenerationHistoryItem;
  run?: ReturnType<typeof useGeneration.getState>["runs"][string];
  preview?: PreviewSlide | null;
  previewLoading?: boolean;
  locale: "en" | "zh";
  rect: DOMRect;
}) {
  const { t } = useLocale();
  const runSlides = Array.isArray(run?.slides) ? run.slides : [];
  const resultSlides = Array.isArray(run?.result?.slides) ? run.result.slides : [];
  const latestPreview = preview ?? runSlides[runSlides.length - 1] ?? resultSlides[resultSlides.length - 1];
  const formatter = new Intl.DateTimeFormat(locale === "zh" ? "zh-CN" : "en-US", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
  const left = Math.min(rect.right + 10, window.innerWidth - 296);
  const top = Math.min(Math.max(12, rect.top - 12), window.innerHeight - 320);
  return (
    <span className="recent-task-popover recent-task-popover-portal" style={{ left, top }}>
      <strong>{task.fileName}</strong>
      <span>{t("recent.status")}: {translateStageStatus(task.status, locale, "history")}</span>
      <span>{t("recent.slides")}: {task.slideCount || runSlides.length || resultSlides.length || 0}</span>
      {task.provider || task.model ? <span>{[task.provider, task.model].filter(Boolean).join(" · ")}</span> : null}
      <span>{t("recent.updated")}: {formatter.format(new Date(task.updatedAt ?? task.createdAt ?? Date.now()))}</span>
      <span className="recent-task-preview">
        {previewLoading ? <i className="recent-task-preview-loading motion-skeleton" /> : latestPreview ? <i dangerouslySetInnerHTML={{ __html: latestPreview.content }} /> : <em>{t("recent.noPreview")}</em>}
      </span>
    </span>
  );
}

function getHistoryTarget(entry: GenerationHistoryItem) {
  const status = entry.status.toLowerCase();
  if (entry.parentJobId || status === "complete" || status === "error" || status === "cancelled") {
    return `/result?job=${entry.jobId}`;
  }
  return `/generate?job=${entry.jobId}`;
}

function formatTaskTime(value: string | undefined, locale: "en" | "zh") {
  if (!value) {
    return "";
  }
  return new Intl.DateTimeFormat(locale === "zh" ? "zh-CN" : "en-US", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}
