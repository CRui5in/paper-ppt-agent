import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { ChevronDown, MessageSquareText } from "lucide-react";
import { Badge } from "../ui/badge";
import { fetchProjectPreview } from "../../lib/api";
import type { GenerationHistoryItem, PreviewSlide } from "../../lib/types";
import type { useGeneration } from "../../hooks/useGeneration";
import { useLocale } from "../../i18n";
import { translateStageStatus } from "../../lib/i18nStatus";

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
  const [collapsed, setCollapsed] = useState(false);
  const recentTasks = typeof limit === "number" ? history.slice(0, limit) : history;
  const [historyPreviews, setHistoryPreviews] = useState<Record<string, PreviewSlide | null | undefined>>({});
  const [hoveredTask, setHoveredTask] = useState<{ task: GenerationHistoryItem; rect: DOMRect } | null>(null);

  useEffect(() => {
    let cancelled = false;
    recentTasks.forEach((task) => {
      if (!task.projectDir || task.jobId in historyPreviews) {
        return;
      }
      fetchProjectPreview(task.projectDir)
        .then((preview) => {
          if (cancelled) return;
          setHistoryPreviews((current) => ({
            ...current,
            [task.jobId]: preview.slides[preview.slides.length - 1] ?? null,
          }));
        })
        .catch(() => {
          if (cancelled) return;
          setHistoryPreviews((current) => ({
            ...current,
            [task.jobId]: null,
          }));
        });
    });
    return () => {
      cancelled = true;
    };
  }, [historyPreviews, recentTasks]);

  return (
    <section className={`recent-tasks-panel rounded-lg border border-border bg-card ${collapsed ? "recent-tasks-panel-collapsed" : ""}`}>
      <button
        type="button"
        className="workspace-panel-header recent-tasks-toggle"
        onClick={() => setCollapsed((value) => !value)}
        aria-expanded={!collapsed}
      >
        <div className="workspace-panel-title">
          <MessageSquareText size={17} />
          <span>{t("recent.title")}</span>
        </div>
        <ChevronDown size={16} />
      </button>
      <div className="recent-task-list">
        {recentTasks.length > 0 ? recentTasks.map((task) => (
          <Link
            className={`recent-task-row ${task.jobId === currentJobId ? "recent-task-row-active" : ""}`}
            key={task.jobId}
            to={getHistoryTarget(task)}
            onMouseEnter={(event) => setHoveredTask({ task, rect: event.currentTarget.getBoundingClientRect() })}
            onMouseLeave={() => setHoveredTask(null)}
            onFocus={(event) => setHoveredTask({ task, rect: event.currentTarget.getBoundingClientRect() })}
            onBlur={() => setHoveredTask(null)}
          >
            <span>
              <strong>{task.fileName}</strong>
              <em>{task.slideCount || 0} {locale === "zh" ? "页" : "slides"} · {formatTaskTime(task.createdAt ?? task.updatedAt, locale)}</em>
            </span>
            <Badge variant={task.status === "error" ? "destructive" : task.status === "complete" ? "success" : "muted"}>
              {task.status === "complete" ? t("recent.completed") : translateStageStatus(task.status, locale, "history")}
            </Badge>
          </Link>
        )) : (
          <div className="recent-task-empty">{t("recent.empty")}</div>
        )}
      </div>
      {hoveredTask && !collapsed
        ? createPortal(
            <RecentTaskPopover
              task={hoveredTask.task}
              run={runs[hoveredTask.task.jobId]}
              preview={historyPreviews[hoveredTask.task.jobId]}
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
  locale,
  rect,
}: {
  task: GenerationHistoryItem;
  run?: ReturnType<typeof useGeneration.getState>["runs"][string];
  preview?: PreviewSlide | null;
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
        {latestPreview ? <i dangerouslySetInnerHTML={{ __html: latestPreview.content }} /> : <em>{t("recent.noPreview")}</em>}
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
