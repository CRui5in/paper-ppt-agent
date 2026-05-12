import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { BringToFront, Bot, ChevronDown, CircleCheck, Copy, Database, Download, FileText, Image, ImagePlus, Info, Layers, Loader2, MessageSquareText, MousePointer2, Play, Plus, Redo2, Save, SendToBack, Sparkles, Square, Table2, Trash2, Type, Undo2, Wand2, X } from "lucide-react";
import { Layout } from "../components/layout/Layout";
import { AgentLog } from "../components/progress/AgentLog";
import { FloatingInspector } from "../components/progress/FloatingInspector";
import { inferActiveStage, PROGRESS_STAGES, ProgressPanel } from "../components/progress/ProgressPanel";
import { KonvaSlideEditor, type EditorCommand, type EditorCommandType, type EditorState } from "../components/preview/KonvaSlideEditor";
import { VersionHistory } from "../components/result/VersionHistory";
import { ImageSearchPanel } from "../components/result/ImageSearchPanel";
import { useGeneration } from "../hooks/useGeneration";
import { useLocale } from "../i18n";
import { createPreviewSlide, deletePreviewSlide, fetchCriticHistory, fetchJobStatus, fetchPreview, fetchProjectPreview, getDownloadUrl, getDownloadUrlForOutput, isNotFoundError, reexportPresentation, updatePreviewSlide } from "../lib/api";
import { FontCustomizer } from "../components/result/FontCustomizer";
import { HoverTooltip } from "../components/common/HoverTooltip";
import { DeleteConfirmTooltip } from "../components/common/DeleteConfirmTooltip";
import { translateJobMessage, translateStageStatus } from "../lib/i18nStatus";
import type { CriticEvent, DeepSeekSettings, GenerateRequestPayload, GenerationHistoryItem, JobStatus, OpenAISettings, PreviewResponse, PreviewSlide, SlideDocument } from "../lib/types";
import { Switch } from "../components/ui/switch";
import { Progress } from "../components/ui/progress";
import { RecentTasksPanel } from "../components/history/RecentTasksPanel";

// Routing profile stored by GeneratePage so we can re-use model config here.
const ROUTING_PROFILE_STORAGE_KEY = "paper-ppt-agent-routing-profiles-v1";

interface RoutingProfile {
  model: string;
  baseUrl: string;
  apiKey: string;
  deepseekSettings?: DeepSeekSettings;
  openaiSettings?: OpenAISettings;
}
type RoutingProfileMap = Record<string, RoutingProfile>;

function readProviderProfile(
  provider: string,
  defaults?: { model?: string; baseUrl?: string },
): { provider: string; model: string; apiKey: string; baseUrl: string; deepseekSettings?: DeepSeekSettings; openaiSettings?: OpenAISettings } | null {
  try {
    const raw = window.localStorage.getItem(ROUTING_PROFILE_STORAGE_KEY);
    if (!raw) return null;
    const profiles = JSON.parse(raw) as RoutingProfileMap;
    const profile = profiles[provider];
    if (!profile?.apiKey) {
      return null;
    }
    return {
      provider,
      model: defaults?.model || profile.model,
      apiKey: profile.apiKey,
      baseUrl: defaults?.baseUrl || profile.baseUrl,
      deepseekSettings: profile.deepseekSettings,
      openaiSettings: profile.openaiSettings,
    };
  } catch {
    return null;
  }
}

export function ResultPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const jobId = params.get("job");
  const { t, locale } = useLocale();
  const {
    reset,
    history,
    startRefine,
    connect,
    activeJobId,
    job: liveJob,
    slides: liveSlides,
    result: liveResult,
    logs: globalLogs,
    criticEvents: globalCriticEvents,
    connectionStatus,
    runs,
    removeHistory,
  } = useGeneration();

  // Read logs, criticEvents, and config from the specific run matching the
  // URL jobId instead of the global top-level state.  The global fields
  // always reflect the *last active* run, so opening a historical task
  // would incorrectly show that run's data instead of the requested one.
  const [remoteCriticEvents, setRemoteCriticEvents] = useState<CriticEvent[] | null>(null);
  const resolvedRun = jobId ? runs[jobId] : undefined;
  const isActiveJob = jobId === activeJobId;
  const logs = isActiveJob ? globalLogs : (resolvedRun?.logs ?? []);
  const localCritic = isActiveJob ? globalCriticEvents : (resolvedRun?.criticEvents ?? []);
  const criticEvents = localCritic.length > 0 ? localCritic : (remoteCriticEvents ?? []);
  // Direct-bind the global error-store setters so we can mirror local
  // page errors (load / refine / reexport / failed-job) into the global
  // error slot — that's what drives the floating ErrorBanner.
  const setGlobalError = (msg: string | undefined) =>
    useGeneration.setState({ error: msg && jobId ? `[${t("logs.job")} ${jobId.slice(0, 8)}]\n${msg}` : msg });

  const [result, setResult] = useState<PreviewResponse | null>(null);
  const [job, setJob] = useState<JobStatus | null>(null);
  const [slides, setSlides] = useState<PreviewSlide[]>([]);
  const [selectedSlide, setSelectedSlide] = useState<PreviewSlide | undefined>(undefined);
  const [loadError, setLoadError] = useState<string | null>(null);

  const historyEntry = history.find((entry) => entry.jobId === jobId);
  const outputPath = job?.output_path ?? result?.output_path ?? historyEntry?.outputPath;
  const resultStatus = job?.status ?? result?.status ?? historyEntry?.status;
  const canEditPreview = resultStatus === "complete" && !loadError && Boolean(jobId);
  const downloadHref = outputPath
    ? getDownloadUrlForOutput(outputPath)
    : jobId
      ? getDownloadUrl(jobId)
      : undefined;
  const isResultLoading = Boolean(jobId && !result && !loadError);

  // ── refine state ───────────────────────────────────────────────────────────
  type SecondaryPanel = "log" | "critic";
  const [secondaryPanel, setSecondaryPanel] = useState<SecondaryPanel | null>(null);
  const [feedback, setFeedback] = useState("");
  const [refineLoading, setRefineLoading] = useState(false);
  const [refineError, setRefineError] = useState<string | null>(null);
  const [targetPagesSet, setTargetPagesSet] = useState<Set<number>>(new Set());
  const [allowStructureChanges, setAllowStructureChanges] = useState(false);
  const [reexportLoading, setReexportLoading] = useState(false);
  const [reexportError, setReexportError] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId || jobId !== activeJobId) {
      return;
    }

    if (liveResult) {
      setResult(liveResult);
      setSlides(liveResult.slides);
      setSelectedSlide((current) => pickSelectedSlide(liveResult.slides, current));
    }
    if (liveJob) {
      setJob(liveJob);
    }
  }, [activeJobId, jobId, liveJob, liveResult]);

  useEffect(() => {
    let cancelled = false;

    async function loadResult(currentJobId: string, entry?: GenerationHistoryItem) {
      const projectDir = entry?.projectDir ?? deriveProjectDirFromOutputPath(entry?.outputPath);
      const canLoadFromProject = Boolean(projectDir) && currentJobId !== activeJobId;

      try {
        const [nextResult, nextJob] = canLoadFromProject
          ? await Promise.all([
              fetchProjectPreview(projectDir!),
              fetchJobStatus(currentJobId).catch(() => {
                if (!entry) {
                  throw new Error("Job not found.");
                }
                return buildStoredJob(entry);
              }),
            ])
          : await Promise.all([
              fetchPreview(currentJobId).catch(async () => {
                if (!projectDir) {
                  throw new Error("Result not found.");
                }
                return fetchProjectPreview(projectDir);
              }),
              fetchJobStatus(currentJobId).catch(() => {
                if (!entry) {
                  throw new Error("Job not found.");
                }
                return buildStoredJob(entry);
              }),
            ]);

        if (cancelled) {
          return;
        }

        setResult(nextResult);
        setJob(nextJob ?? (entry ? buildStoredJob(entry) : null));
        setSlides(nextResult.slides);
        setSelectedSlide((current) => pickSelectedSlide(nextResult.slides, current));
        setLoadError(null);
      } catch (error) {
        if (cancelled) {
          return;
        }
        setResult(null);
        setJob(entry ? buildStoredJob(entry) : null);
        setSlides([]);
        setSelectedSlide(undefined);
        // A 404 from the backend means the job is gone (server restart,
        // session GC, or someone shared a stale URL). Use a friendlier
        // message that points the user to the next step instead of
        // dumping the raw error string.
        if (isNotFoundError(error)) {
          setLoadError(
            entry
              ? "This run is no longer available on the server, but its history record was kept. Start a new run to regenerate."
              : "This job was not found. It may have been removed or never existed on this server.",
          );
        } else {
          setLoadError(error instanceof Error ? error.message : "Failed to load result.");
        }
      }
    }

    if (!jobId) {
      setResult(null);
      setJob(null);
      setSlides([]);
      setSelectedSlide(undefined);
      setLoadError("Missing job id.");
      return () => {
        cancelled = true;
      };
    }

    if (jobId === activeJobId && liveJob) {
      setJob(liveJob);
      setResult(liveResult ?? null);
      setSlides(liveResult?.slides ?? liveSlides);
      setSelectedSlide((current) => pickSelectedSlide(liveResult?.slides ?? liveSlides, current));
      setLoadError(null);
      return () => {
        cancelled = true;
      };
    }

    void loadResult(jobId, historyEntry);
    return () => {
      cancelled = true;
    };
  }, [activeJobId, historyEntry, jobId, liveJob, liveResult, liveSlides]);

  useEffect(() => {
    setSelectedSlide((current) => pickSelectedSlide(slides, current));
  }, [slides]);

  // Fetch critic events from backend when local data is empty (e.g. after
  // page refresh or server restart).  The backend persists critic events to
  // critic_history.json so they survive across sessions.
  useEffect(() => {
    if (!jobId || localCritic.length > 0 || isActiveJob) {
      setRemoteCriticEvents(null);
      return;
    }
    let cancelled = false;
    fetchCriticHistory(jobId)
      .then((data) => {
        if (!cancelled && Array.isArray(data.events) && data.events.length > 0) {
          setRemoteCriticEvents(data.events as CriticEvent[]);
        }
      })
      .catch(() => {
        // Silently ignore — critic data is optional
      });
    return () => { cancelled = true; };
  }, [jobId, localCritic.length, isActiveJob]);

  // Mirror any active page-level error into the global ``error`` store so
  // the floating ErrorBanner becomes visible. Priority order:
  //   1. ``loadError``     — the result preview / job lookup failed.
  //   2. ``reexportError`` — a re-export attempt failed.
  //   3. ``refineError``   — a refine submission failed.
  //   4. ``job.error``     — the failed run itself carries an error.
  // Cleared on unmount so navigating away from the page doesn't leave a
  // stale banner behind.
  useEffect(() => {
    const failedJobError =
      job?.status === "error" ? job.error ?? historyEntry?.error ?? null : null;
    const message = loadError || reexportError || refineError || failedJobError || null;
    if (message) {
      setGlobalError(message);
    } else {
      setGlobalError(undefined);
    }
    return () => {
      // Only clear if we were the ones who set it — comparing the current
      // store value to the message we set keeps unrelated errors (e.g.
      // raised by another page that just navigated in) intact.
      const current = useGeneration.getState().error;
      if (current && current === message) {
        setGlobalError(undefined);
      }
    };
  }, [loadError, reexportError, refineError, job?.status, job?.error, historyEntry?.error]);

  // Auto-sync multi-select when slide count changes (keeps valid pages only)
  useEffect(() => {
    const max = slides.length;
    setTargetPagesSet((prev) => {
      const next = new Set<number>();
      prev.forEach((page) => {
        if (page >= 1 && page <= max) next.add(page);
      });
      if (next.size === prev.size) return prev;
      return next;
    });
  }, [slides.length]);

  // Navigate to generation page to watch refine progress
  const handleRefine = async () => {
    if (!feedback.trim() || !jobId) return;

    const targetPages: number[] = Array.from(targetPagesSet).sort((a, b) => a - b);

    const profile = readProviderProfile(historyEntry?.provider ?? "openai", {
      model: historyEntry?.model,
      baseUrl: historyEntry?.baseUrl ?? undefined,
    });
    if (!profile || !profile.apiKey) {
      setRefineError("No model configuration found. Please return to the generate page and configure a model first.");
      return;
    }

    const fallbackOptions: GenerateRequestPayload["options"] = historyEntry?.options ?? {
      canvas_format: "ppt169",
      style: "academic",
      language: "zh",
      detail_level: "normal",
    };

    setRefineLoading(true);
    setRefineError(null);
    try {
      const newJobId = await startRefine({
        job_id: jobId,
        feedback: feedback.trim(),
        model_config: {
          provider: profile.provider,
          model: profile.model,
          api_key: profile.apiKey,
          base_url: profile.baseUrl || undefined,
          deepseek_settings:
            profile.provider === "deepseek" ? profile.deepseekSettings : undefined,
          openai_settings:
            profile.provider === "openai" ? profile.openaiSettings : undefined,
        },
        options: fallbackOptions,
        target_pages: targetPages,
        allow_structure_changes: allowStructureChanges,
      });
      setFeedback("");
      setTargetPagesSet(new Set());
      connect(newJobId);
      navigate(`/result?job=${newJobId}`);
    } catch (err) {
      setRefineError(err instanceof Error ? err.message : "Refinement failed.");
    } finally {
      setRefineLoading(false);
    }
  };

  const handleReexport = async () => {
    if (!jobId) return;
    setReexportLoading(true);
    setReexportError(null);
    try {
      const response = await reexportPresentation(jobId);
      setJob((current) =>
        current
          ? {
              ...current,
              status: response.status,
              output_path: response.output_path,
              error: null,
            }
          : {
              status: response.status,
              progress: 1,
              message: "",
              slides_completed: slides.length,
              total_slides: slides.length,
              output_path: response.output_path,
              error: null,
            },
      );
      setResult((current) =>
        current
          ? {
              ...current,
              output_path: response.output_path,
              status: response.status,
            }
          : current,
      );
    } catch (err) {
      setReexportError(err instanceof Error ? err.message : "Re-export failed.");
    } finally {
      setReexportLoading(false);
    }
  };

  const handleSaveSlideContent = async (slide: PreviewSlide, content: string, document: SlideDocument) => {
    if (!jobId || !canEditPreview) return;
    const updated = await updatePreviewSlide(jobId, slide.index, content, document, slide.notes ?? document.speakerNotes ?? "");
    setSlides((current) => current.map((item) => item.index === updated.index ? updated : item));
    setSelectedSlide(updated);
    setResult((current) =>
      current
        ? {
            ...current,
            slides: current.slides.map((item) => item.index === updated.index ? updated : item),
          }
        : current,
    );
  };

  const handleCreateSlide = async () => {
    if (!jobId || !canEditPreview) return;
    setReexportError(null);
    try {
      const created = await createPreviewSlide(jobId);
      setSlides((current) => [...current, created]);
      setSelectedSlide(created);
      setResult((current) => current ? { ...current, slides: [...current.slides, created] } : current);
    } catch (err) {
      setReexportError(err instanceof Error ? err.message : "Failed to create slide.");
    }
  };

  const handleDeleteSlide = async (slide: PreviewSlide) => {
    if (!jobId || !canEditPreview) return;
    setReexportError(null);
    try {
      const preview = await deletePreviewSlide(jobId, slide.index);
      setResult(preview);
      setSlides(preview.slides);
      setSelectedSlide(preview.slides[Math.min(slide.index - 1, preview.slides.length - 1)]);
    } catch (err) {
      setReexportError(err instanceof Error ? err.message : "Failed to delete slide.");
    }
  };

  const handleSaveSlideNotes = async (slide: PreviewSlide, notes: string) => {
    if (!jobId || !canEditPreview) return;
    const updated = await updatePreviewSlide(jobId, slide.index, slide.content, slide.document ?? undefined, notes);
    setSlides((current) => current.map((item) => item.index === updated.index ? updated : item));
    setSelectedSlide(updated);
    setResult((current) => current ? { ...current, slides: current.slides.map((item) => item.index === updated.index ? updated : item) } : current);
  };

  const handleRefreshPreview = async (preferredSlideIndex?: number) => {
    if (!jobId) return;
    const projectDir = result?.project_dir ?? historyEntry?.projectDir;
    const preview = projectDir ? await fetchProjectPreview(projectDir) : await fetchPreview(jobId);
    setSlides(preview.slides);
    setResult((current) => current ? { ...current, slides: preview.slides, output_path: preview.output_path ?? current.output_path, status: preview.status } : preview);
    setSelectedSlide((current) => {
      const targetIndex = preferredSlideIndex ?? current?.index;
      return preview.slides.find((slide) => slide.index === targetIndex) ?? preview.slides[0];
    });
  };

  return (
    <Layout showSidebar={false} contentClassName="studio-page result-page result-workspace-page">
      <section className="scholarly-workspace result-studio-workspace">
        <aside className="sources-panel result-sources-panel">
          <div className="workspace-panel-header">
            <div className="workspace-panel-title">
              <Database size={17} />
              <span>{t("source.title")}</span>
            </div>
          </div>
          <div className="sources-content">
            <ResultSourceSummary historyEntry={historyEntry} />
            {(job?.status ?? result?.status ?? historyEntry?.status) === "complete" && jobId ? (
              <div className="result-left-section">
                <div className="panel-title-row result-left-section-title">
                  <Type size={17} className="panel-title-icon" />
                  <span>{t("result.fontsTitle")}</span>
                </div>
                <FontCustomizer
                  jobId={jobId}
                  onReexported={(outputPath) => {
                    setJob((current) => current ? { ...current, output_path: outputPath, status: "complete", error: null } : current);
                    setResult((current) => current ? { ...current, output_path: outputPath } : current);
                    const projectDir = result?.project_dir ?? historyEntry?.projectDir;
                    if (projectDir) {
                      fetchProjectPreview(projectDir)
                        .then((p) => { setSlides(p.slides); setResult((c) => c ? { ...c, slides: p.slides } : c); })
                        .catch(() => undefined);
                    }
                  }}
                />
              </div>
            ) : (
              <div className="source-inline-process result-inline-process">
                <div className="panel-title-row source-inline-process-title">
                  <Bot size={17} className="panel-title-icon" />
                  <span>{t("progress.title")}</span>
                </div>
                <ProgressPanel compact hideHeader job={job ?? liveJob ?? undefined} connectionStatus={connectionStatus} enrichmentStats={jobId ? runs[jobId]?.enrichmentStats : undefined} logs={logs} />
              </div>
            )}
            <RecentTasksPanel history={history} runs={runs} currentJobId={jobId ?? undefined} locale={locale} />
          </div>
        </aside>

        <ResultSlideWorkspace
          jobId={jobId ?? undefined}
          slides={slides}
          selectedSlide={selectedSlide}
          onSelect={setSelectedSlide}
          downloadHref={downloadHref}
          onReexport={() => void handleReexport()}
          reexportLoading={reexportLoading}
          editable={canEditPreview}
          onSaveSlide={handleSaveSlideContent}
          onSaveNotes={handleSaveSlideNotes}
          onCreateSlide={() => void handleCreateSlide()}
          onDeleteSlide={handleDeleteSlide}
          onImageApplied={(slideIndex) => handleRefreshPreview(slideIndex)}
          onNewRun={() => {
            reset();
            navigate("/generate?fresh=1");
          }}
          loading={isResultLoading}
          onDeleteRun={jobId ? async () => {
            await removeHistory(jobId);
            navigate("/generate?fresh=1");
          } : undefined}
        />

        <aside className="configuration-panel result-configuration-panel">
          <div className="workspace-panel-header">
            <div className="workspace-panel-title">
              <Wand2 size={17} />
              <span>{t("result.refineTitle")}</span>
            </div>
          </div>
          <div className="configuration-scroll">
            <section className="panel result-refine">
              <div className="refine-form">
                <div className="selectPages-toolbar">
                  <strong>{t("result.selectPages")}</strong>
                  <button
                    type="button"
                    onClick={() =>
                      setTargetPagesSet(
                        targetPagesSet.size === slides.length
                          ? new Set()
                          : new Set(slides.map((_, i) => i + 1)),
                      )
                    }
                    disabled={refineLoading || slides.length === 0}
                    className="ghost-button"
                  >
                    {t("result.selectAll")} ({targetPagesSet.size}/{slides.length})
                  </button>
                </div>
                <div className="result-page-chip-grid">
                  {slides.map((slide, idx) => {
                    const page = idx + 1;
                    const selected = targetPagesSet.has(page);
                    return (
                      <button
                        type="button"
                        key={slide.name ?? idx}
                        className={`result-page-chip ${selected ? "result-page-chip-active" : ""}`}
                        disabled={refineLoading}
                        onClick={() => {
                          setTargetPagesSet((prev) => {
                            const next = new Set(prev);
                            if (next.has(page)) next.delete(page);
                            else next.add(page);
                            return next;
                          });
                        }}
                      >
                        {page}
                      </button>
                    );
                  })}
                </div>
                <label className="checkbox-row">
                  <Switch checked={allowStructureChanges} onCheckedChange={setAllowStructureChanges} disabled={refineLoading} />
                  <span>{t("result.allowStructure")}</span>
                </label>
                <textarea className="refine-textarea" rows={4} placeholder={t("result.refinePlaceholder")} value={feedback} onChange={(e) => setFeedback(e.target.value)} disabled={refineLoading} />
                <button type="button" className="primary-button full-width" disabled={!feedback.trim() || refineLoading || !jobId} onClick={() => void handleRefine()}>
                  {refineLoading ? <Loader2 size={16} className="spin" /> : <Wand2 size={16} />}
                  {refineLoading ? t("result.refineLoading") : t("result.refineSubmit")}
                </button>
              </div>
            </section>

            <VersionHistory jobId={jobId} onError={setReexportError} />
          </div>
        </aside>

        <ResultMonitor
          job={job ?? undefined}
          result={result}
          historyEntry={historyEntry}
          logs={logs}
          criticEvents={criticEvents}
          connectionStatus={connectionStatus}
          activePanel={secondaryPanel}
          onOpenPanel={setSecondaryPanel}
        />
      </section>

      <FloatingInspector
        open={secondaryPanel === "log"}
        title={t("log.title")}
        icon={<MessageSquareText size={15} className="panel-title-icon" />}
        onClose={() => setSecondaryPanel(null)}
      >
        <AgentLog mode="logs" hideHeader logs={logs} criticEvents={[]} jobId={jobId ?? undefined} />
      </FloatingInspector>
      <FloatingInspector
        open={secondaryPanel === "critic"}
        title={t("monitor.review")}
        icon={<Sparkles size={15} className="panel-title-icon" />}
        onClose={() => setSecondaryPanel(null)}
      >
        <AgentLog mode="critic" hideHeader logs={[]} criticEvents={criticEvents} jobId={jobId ?? undefined} />
      </FloatingInspector>
    </Layout>
  );
}

function ResultSourceSummary({ historyEntry }: { historyEntry?: GenerationHistoryItem }) {
  const { t } = useLocale();
  const type = (historyEntry?.sourceType ?? "pdf").toLowerCase();
  const label = type.includes("doc") ? "DOC" : type.toUpperCase().slice(0, 3);
  const meta = [
    historyEntry?.sourceType?.toUpperCase(),
    historyEntry?.updatedAt,
  ].filter(Boolean).join(" · ");

  if (!historyEntry) {
    return (
      <div className="source-empty-state result-source-summary">
        <FileText size={22} />
        <span>{t("source.empty")}</span>
      </div>
    );
  }

  return (
    <div className="source-list source-list-compact result-source-summary">
      <div className="source-row">
        <span className={`source-file-type source-file-${type.includes("doc") ? "doc" : "pdf"}`}>{label}</span>
        <span className="source-row-copy">
          <strong>{historyEntry.fileName}</strong>
          <em>{meta}</em>
        </span>
        <CircleCheck size={15} className="source-check" />
      </div>
    </div>
  );
}

function ResultSlideWorkspace({
  jobId,
  slides,
  selectedSlide,
  onSelect,
  downloadHref,
  onReexport,
  reexportLoading,
  editable,
  onSaveSlide,
  onSaveNotes,
  onCreateSlide,
  onDeleteSlide,
  onImageApplied,
  onNewRun,
  loading,
  onDeleteRun,
}: {
  jobId?: string;
  slides: PreviewSlide[];
  selectedSlide?: PreviewSlide;
  onSelect: (slide: PreviewSlide) => void;
  downloadHref?: string;
  onReexport: () => void;
  reexportLoading: boolean;
  editable: boolean;
  onSaveSlide: (slide: PreviewSlide, content: string, document: SlideDocument) => Promise<void>;
  onSaveNotes: (slide: PreviewSlide, notes: string) => Promise<void>;
  onCreateSlide: () => void;
  onDeleteSlide: (slide: PreviewSlide) => Promise<void>;
  onImageApplied: (slideIndex: number) => Promise<void>;
  onNewRun: () => void;
  loading?: boolean;
  onDeleteRun?: () => Promise<void>;
}) {
  const { t } = useLocale();
  const [editorCommand, setEditorCommand] = useState<EditorCommand | undefined>(undefined);
  const [editorState, setEditorState] = useState<EditorState>({
    autoSave: true,
    saveState: "idle",
    canEdit: editable,
    canUndo: false,
    canRedo: false,
  });
  const [thumbnailMenu, setThumbnailMenu] = useState<{ left: number; top: number; slide: PreviewSlide } | null>(null);
  const [pendingDeleteIndexes, setPendingDeleteIndexes] = useState<number[]>([]);
  const [notesDraft, setNotesDraft] = useState(selectedSlide?.notes ?? "");
  const [slideshowOpen, setSlideshowOpen] = useState(false);
  const [slideshowIndex, setSlideshowIndex] = useState(0);
  const [imageSearchOpen, setImageSearchOpen] = useState(false);
  const exportMenuRef = useRef<HTMLDetailsElement | null>(null);
  const deleteRunRef = useRef<HTMLButtonElement | null>(null);
  const [exportMenuOpen, setExportMenuOpen] = useState(false);
  const [deleteRunConfirmOpen, setDeleteRunConfirmOpen] = useState(false);
  const [deleteRunLoading, setDeleteRunLoading] = useState(false);
  const visibleSlides = slides.filter((slide) => !pendingDeleteIndexes.includes(slide.index));
  const runCommand = (type: EditorCommandType) => {
    if (type === "save" && pendingDeleteIndexes.length) {
      void flushPendingSlideDeletes();
    }
    setEditorCommand({ type, id: Date.now() });
  };
  const markSlideForDelete = (slide: PreviewSlide) => {
    if (slides.length - pendingDeleteIndexes.length <= 1) return;
    setPendingDeleteIndexes((current) => current.includes(slide.index) ? current : [...current, slide.index]);
    if (selectedSlide?.index === slide.index) {
      const fallback = slides.find((item) => item.index !== slide.index && !pendingDeleteIndexes.includes(item.index));
      if (fallback) onSelect(fallback);
    }
  };
  const flushPendingSlideDeletes = async () => {
    const targets = [...pendingDeleteIndexes].sort((a, b) => b - a);
    for (const index of targets) {
      const slide = slides.find((item) => item.index === index);
      if (slide) await onDeleteSlide(slide);
    }
    setPendingDeleteIndexes([]);
  };
  useEffect(() => {
    setNotesDraft(selectedSlide?.notes ?? "");
  }, [selectedSlide?.index, selectedSlide?.notes]);
  useEffect(() => {
    if (!slideshowOpen) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSlideshowOpen(false);
      if (event.key === "ArrowRight" || event.key === " ") setSlideshowIndex((index) => Math.min(visibleSlides.length - 1, index + 1));
      if (event.key === "ArrowLeft") setSlideshowIndex((index) => Math.max(0, index - 1));
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [slideshowOpen, visibleSlides.length]);
  useEffect(() => {
    if (!thumbnailMenu) return undefined;
    const close = () => setThumbnailMenu(null);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("click", close);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [thumbnailMenu]);
  useEffect(() => {
    if (!exportMenuOpen) return undefined;
    const closeOnOutside = (event: PointerEvent) => {
      if (exportMenuRef.current && !exportMenuRef.current.contains(event.target as Node)) {
        setExportMenuOpen(false);
      }
    };
    window.addEventListener("pointerdown", closeOnOutside);
    return () => window.removeEventListener("pointerdown", closeOnOutside);
  }, [exportMenuOpen]);
  return (
    <main className="slide-workspace-panel">
      <div className="slide-workspace-header">
        <p>
          <span>{t("preview.slidePreview")}</span>
          <HoverTooltip content={t("preview.editorWarning")}><span className="preview-info-tip"><Info size={15} /></span></HoverTooltip>
        </p>
        {onDeleteRun ? (
          <button
            ref={deleteRunRef}
            type="button"
            className="result-delete-run-button"
            aria-label={t("versions.delete")}
            onClick={() => setDeleteRunConfirmOpen((open) => !open)}
          >
            <Trash2 size={16} />
            <span>{t("result.deleteTask")}</span>
          </button>
        ) : null}
        <details ref={exportMenuRef} className="result-export-menu" open={exportMenuOpen}>
          <summary
            className="result-export-summary"
            onClick={(event) => {
              event.preventDefault();
              setExportMenuOpen((open) => !open);
            }}
          >
            <Download size={16} />
            <span>{t("result.download")}</span>
            <ChevronDown size={14} />
          </summary>
          <div className="result-export-menu-content">
            {downloadHref ? (
              <a href={downloadHref} onClick={() => setExportMenuOpen(false)}>
                <Download size={15} />
                <span>{t("result.download")}</span>
              </a>
            ) : (
              <button type="button" disabled>
                <Download size={15} />
                <span>{t("common.pending")}</span>
              </button>
            )}
            <button
              type="button"
              onClick={() => {
                setExportMenuOpen(false);
                onReexport();
              }}
              disabled={reexportLoading || !editable}
            >
              {reexportLoading ? <Loader2 size={15} className="spin" /> : <Wand2 size={15} />}
              <span>{reexportLoading ? t("result.reexportLoading") : t("result.reexport")}</span>
            </button>
            <button
              type="button"
              onClick={() => {
                setExportMenuOpen(false);
                onNewRun();
              }}
            >
              <Plus size={15} />
              <span>{t("result.newRun")}</span>
            </button>
          </div>
        </details>
        {deleteRunConfirmOpen && onDeleteRun ? (
          <DeleteConfirmTooltip
            anchorRef={deleteRunRef}
            message={t("sidebar.confirmDeleteFiles")}
            confirmLabel={t("versions.delete")}
            cancelLabel={t("versions.close")}
            loading={deleteRunLoading}
            onCancel={() => setDeleteRunConfirmOpen(false)}
            onConfirm={async () => {
              setDeleteRunLoading(true);
              try {
                await onDeleteRun();
              } finally {
                setDeleteRunLoading(false);
                setDeleteRunConfirmOpen(false);
              }
            }}
          />
        ) : null}
      </div>
      <div className="slide-toolbar">
        <button type="button" disabled={!editable} onClick={onCreateSlide}><Plus size={15} /> {t("preview.newSlide")}</button>
        <span className="toolbar-divider" />
        <HoverTooltip content={t("editor.textTool")}><button type="button" disabled={!editable} onClick={() => runCommand("addText")}><Type size={15} /></button></HoverTooltip>
        <HoverTooltip content={t("editor.shapeTool")}><button type="button" disabled={!editable} onClick={() => runCommand("addRect")}><Square size={15} /></button></HoverTooltip>
        <HoverTooltip content={t("editor.pictureTool")}><button type="button" disabled={!editable} onClick={() => runCommand("addImage")}><Image size={15} /></button></HoverTooltip>
        <HoverTooltip content={t("imageSearch.title")}>
          <button
            type="button"
            className={imageSearchOpen ? "slide-toolbar-button-active" : undefined}
            disabled={!editable || !jobId || !selectedSlide}
            onClick={() => setImageSearchOpen((open) => !open)}
          >
            <ImagePlus size={15} /> {t("imageSearch.shortTitle")}
          </button>
        </HoverTooltip>
        <HoverTooltip content={t("editor.tableTool")}><button type="button" disabled={!editable} onClick={() => runCommand("addTable")}><Table2 size={15} /></button></HoverTooltip>
        <span className="toolbar-divider" />
        <HoverTooltip content={t("editor.undo")}><button type="button" disabled={!editable || !editorState.canUndo} onClick={() => runCommand("undo")}><Undo2 size={15} /></button></HoverTooltip>
        <HoverTooltip content={t("editor.redo")}><button type="button" disabled={!editable || !editorState.canRedo} onClick={() => runCommand("redo")}><Redo2 size={15} /></button></HoverTooltip>
        <HoverTooltip content={t("editor.duplicate")}><button type="button" disabled={!editable || !editorState.selectedType} onClick={() => runCommand("duplicate")}><Copy size={15} /></button></HoverTooltip>
        <HoverTooltip content={t("editor.delete")}><button type="button" disabled={!editable || !editorState.selectedType} onClick={() => runCommand("delete")}><Trash2 size={15} /></button></HoverTooltip>
        <span className="toolbar-divider" />
        <HoverTooltip content={t("editor.sendBackward")}><button type="button" disabled={!editable || !editorState.selectedType} onClick={() => runCommand("backward")}><SendToBack size={15} /></button></HoverTooltip>
        <HoverTooltip content={t("editor.bringForward")}><button type="button" disabled={!editable || !editorState.selectedType} onClick={() => runCommand("forward")}><BringToFront size={15} /></button></HoverTooltip>
        <span className="toolbar-divider" />
        <HoverTooltip content={t("editor.autosave")}>
          <button type="button" disabled={!editable} onClick={() => runCommand("toggleAutosave")}>
            <Layers size={15} /> {editorState.autoSave ? t("editor.autosave") : t("editor.manual")}
          </button>
        </HoverTooltip>
        <HoverTooltip content={t("editor.saveEdits")}>
          <button type="button" disabled={!editable || editorState.saveState === "saving"} onClick={() => runCommand("save")}>
            <Save size={15} /> {editorState.saveState === "saving" ? t("editor.saving") : editorState.saveState === "saved" ? t("editor.saved") : t("editor.save")}
          </button>
        </HoverTooltip>
        <span className="toolbar-spacer" />
        <HoverTooltip content={t("preview.startSlideshow")}>
          <button
            type="button"
            disabled={!visibleSlides.length}
            onClick={() => {
              setSlideshowIndex(Math.max(0, visibleSlides.findIndex((slide) => slide.index === selectedSlide?.index)));
              setSlideshowOpen(true);
            }}
          >
            <Play size={15} /> {t("preview.slideshow")}
          </button>
        </HoverTooltip>
        <button type="button" disabled><MousePointer2 size={15} /> {t("editor.fit")}</button>
        <button type="button" disabled>16:9</button>
      </div>
      <div className="slide-stage result-slide-stage">
        <div className="thumbnail-rail">
          {loading ? Array.from({ length: 5 }).map((_, index) => (
            <button type="button" className={`rail-slide ${index === 0 ? "rail-slide-active" : ""}`} key={index} disabled>
              <span>{index + 1}</span>
              <div className="rail-placeholder motion-skeleton" />
            </button>
          )) : visibleSlides.length > 0 ? visibleSlides.map((slide) => (
            <button
              type="button"
              key={slide.index}
              className={`rail-slide ${selectedSlide?.index === slide.index ? "rail-slide-active" : ""}`}
              onClick={() => onSelect(slide)}
              onContextMenu={(event) => {
                event.preventDefault();
                onSelect(slide);
                setThumbnailMenu({ left: event.clientX, top: event.clientY, slide });
              }}
            >
              <span>{slide.index}</span>
              <div dangerouslySetInnerHTML={{ __html: slide.content }} />
            </button>
          )) : Array.from({ length: 1 }).map((_, index) => (
            <button type="button" className={`rail-slide ${index === 0 ? "rail-slide-active" : ""}`} key={index}>
              <span>{index + 1}</span>
              <div className="rail-placeholder" />
            </button>
          ))}
          <HoverTooltip content={editable ? t("preview.newSlide") : t("common.pending")}>
            <button className="rail-add" type="button" disabled={!editable} onClick={onCreateSlide}>
              <Plus size={18} />
            </button>
          </HoverTooltip>
          {thumbnailMenu ? (
            <div
              className="konva-context-menu thumbnail-context-menu"
              style={{ left: thumbnailMenu.left, top: thumbnailMenu.top }}
              onClick={(event) => event.stopPropagation()}
            >
              <strong>{t("preview.slidePreview")}</strong>
              <button
                type="button"
                disabled={!editable || slides.length - pendingDeleteIndexes.length <= 1}
                onClick={() => {
                markSlideForDelete(thumbnailMenu.slide);
                setThumbnailMenu(null);
              }}
              >
                {t("editor.delete")}
              </button>
            </div>
          ) : null}
        </div>
        <div className="slide-canvas-area">
          {loading ? (
            <div className="scholarly-slide-frame slide-loading-frame motion-skeleton" />
          ) : selectedSlide ? (
            <KonvaSlideEditor
              slide={selectedSlide}
              editable={editable}
              command={editorCommand}
              onStateChange={setEditorState}
              onSave={onSaveSlide}
            />
          ) : (
            <div className="scholarly-slide-frame viewer-empty" />
          )}
          <label className="speaker-notes speaker-notes-editable">
            <textarea
              value={notesDraft}
              maxLength={1000}
              disabled={!editable || !selectedSlide}
              placeholder={t("preview.notesPlaceholder")}
              onChange={(event) => setNotesDraft(event.target.value)}
              onBlur={() => {
                if (selectedSlide && notesDraft !== (selectedSlide.notes ?? "")) void onSaveNotes(selectedSlide, notesDraft);
              }}
            />
            <em>{notesDraft.length} / 1000</em>
          </label>
          {imageSearchOpen && jobId && selectedSlide ? (
            <ImageSearchPanel
              jobId={jobId}
              slideIndex={selectedSlide.index}
              slideTitle={selectedSlide.name}
              onClose={() => setImageSearchOpen(false)}
              onImageApplied={() => onImageApplied(selectedSlide.index)}
            />
          ) : null}
        </div>
      </div>
      {slideshowOpen && visibleSlides[slideshowIndex] ? (
        <div className="slideshow-overlay" role="dialog" aria-modal="true" onClick={() => setSlideshowIndex((index) => Math.min(visibleSlides.length - 1, index + 1))}>
          <HoverTooltip content={t("preview.exitSlideshow")}>
            <button type="button" className="slideshow-close" onClick={(event) => { event.stopPropagation(); setSlideshowOpen(false); }}>
              <X size={18} />
            </button>
          </HoverTooltip>
          <div className="slideshow-slide" dangerouslySetInnerHTML={{ __html: visibleSlides[slideshowIndex].content }} />
          <div className="slideshow-footer">
            <span>{slideshowIndex + 1} / {visibleSlides.length}</span>
            <span>{t("preview.slideshowHint")}</span>
          </div>
        </div>
      ) : null}
    </main>
  );
}

function ResultRunStatus({
  job,
  historyEntry,
  logs,
  locale,
}: {
  job?: JobStatus;
  historyEntry?: GenerationHistoryItem;
  logs: string[];
  locale: "en" | "zh";
}) {
  const { t } = useLocale();
  const status = job?.status ?? historyEntry?.status;
  const reason = job?.error ?? historyEntry?.error ?? (status === "cancelled" ? t("result.cancelledReason") : "");
  const latest = logs.length ? logs[logs.length - 1].replace(/^\[[^\]]+\]\s*/, "") : job?.message;
  return (
    <section className="result-run-status">
      <div className={`result-run-status-card result-run-status-${status ?? "unknown"}`}>
        <strong>{formatStatusLabel(status, locale, t("common.unknown"))}</strong>
        <span>{latest || t("monitor.waiting")}</span>
        {reason ? <em>{reason}</em> : null}
      </div>
    </section>
  );
}

function ResultMonitor({
  job,
  result,
  historyEntry,
  logs,
  criticEvents,
  connectionStatus,
  activePanel,
  onOpenPanel,
}: {
  job?: JobStatus;
  result: PreviewResponse | null;
  historyEntry?: GenerationHistoryItem;
  logs: string[];
  criticEvents: unknown[];
  connectionStatus: string;
  activePanel: "log" | "critic" | null;
  onOpenPanel: (panel: "log" | "critic") => void;
}) {
  const { t, locale } = useLocale();
  const status = job?.status ?? result?.status ?? historyEntry?.status;
  const completedSlides = job?.slides_completed ?? historyEntry?.slideCount ?? result?.slides.length ?? 0;
  const totalSlides = job?.total_slides ?? historyEntry?.slideCount ?? result?.slides.length ?? 0;
  const progress = totalSlides > 0 ? Math.round((completedSlides / totalSlides) * 100) : 0;
  const rawLatestText = logs.length ? logs[logs.length - 1].replace(/^\[[^\]]+\]\s*/, "") : job?.message ?? t("monitor.waiting");
  const latestText = translateJobMessage(rawLatestText, locale) ?? rawLatestText;
  const outputPath = job?.output_path ?? result?.output_path ?? historyEntry?.outputPath;
  const outputName = outputPath ? outputPath.replace(/\\/g, "/").split("/").pop() : t("common.pending");
  const nextStep = formatMonitorNextStep(
    job ?? (status ? {
      status,
      progress: status === "complete" ? 1 : 0,
      message: "",
      slides_completed: completedSlides,
      total_slides: totalSlides,
      output_path: outputPath ?? null,
      error: null,
    } : undefined),
    logs,
    locale,
    t,
  );
  const isActiveRun = Boolean(job && status && !["idle", "complete", "error", "cancelled"].includes(status));
  const connectionLabel =
    connectionStatus === "connected"
      ? t("status.connected")
      : connectionStatus === "connecting"
        ? t("status.connecting")
        : t("status.disconnected");
  return (
    <section className="agent-monitor-panel result-monitor-panel">
      <div className="agent-monitor-header">
        <div className="workspace-panel-title">
          <Bot size={18} />
          <span>{t("monitor.title")}</span>
        </div>
        <div className="monitor-tabs">
          <button type="button" className={activePanel === "log" ? "monitor-tab-active" : ""} onClick={() => onOpenPanel("log")}>
            <MessageSquareText size={14} />
            {t("monitor.logs")}
          </button>
          <button type="button" className={activePanel === "critic" ? "monitor-tab-active" : ""} onClick={() => onOpenPanel("critic")}>
            <Sparkles size={14} />
            {t("monitor.review")}
          </button>
        </div>
      </div>
      <div className="agent-monitor-body result-monitor-body">
        <div className={`agent-avatar ${isActiveRun ? "agent-avatar-active" : ""}`}><Bot size={26} /></div>
        <div className="agent-summary">
          <strong>{formatStatusLabel(status, locale, t("common.unknown"))}</strong>
          <span>{outputName}</span>
          <em>{connectionLabel}</em>
        </div>
        <div className="monitor-progress-block">
          <span><strong>{progress}%</strong> {t("monitor.slideGeneration")}</span>
          <Progress value={progress} className="monitor-progress" />
          <em>{completedSlides} / {totalSlides || "?"} {t("preview.slides")}</em>
        </div>
        <div className="monitor-event">
          <strong>{t("monitor.lastEvent")}</strong>
          <HoverTooltip content={latestText}><span><i className={connectionStatus === "connected" ? "event-dot-on" : ""} />{latestText}</span></HoverTooltip>
        </div>
        <div className="monitor-event">
          <strong>{t("monitor.nextStep")}</strong>
          <HoverTooltip content={nextStep}><span>{nextStep}</span></HoverTooltip>
        </div>
      </div>
    </section>
  );
}

function formatMonitorNextStep(
  job: JobStatus | undefined,
  logs: string[],
  locale: "en" | "zh",
  t: (key: string) => string,
) {
  const status = job?.status ?? "idle";
  if (status === "idle") {
    return t("monitor.nextUpload");
  }
  if (status === "complete") {
    return t("result.refineTitle");
  }
  if (status === "error" || status === "cancelled") {
    return translateStageStatus(status, locale, "progress");
  }
  const activeStage = inferActiveStage(job, logs);
  const activeIndex = PROGRESS_STAGES.findIndex((stage) => stage.id === activeStage);
  const nextStage = activeIndex >= 0 ? PROGRESS_STAGES[activeIndex + 1] : undefined;
  return nextStage
    ? translateStageStatus(nextStage.id, locale, "progress")
    : translateStageStatus(status, locale, "progress");
}

function ConfigViewer({
  provider,
  model,
  baseUrl,
  options,
  parentJobId,
}: {
  provider?: string;
  model?: string;
  baseUrl?: string;
  options?: import("../lib/types").GenerationOptions;
  parentJobId?: string | null;
}) {
  const { t } = useLocale();
  const entries: { label: string; value: string }[] = [];
  if (provider) entries.push({ label: t("config.provider"), value: provider });
  if (model) entries.push({ label: t("config.model"), value: model });
  if (baseUrl) entries.push({ label: "Base URL", value: baseUrl });
  if (options?.style) entries.push({ label: t("config.style"), value: options.style });
  if (options?.language) entries.push({ label: t("config.language"), value: options.language });
  if (options?.detail_level) entries.push({ label: t("config.detailLevel"), value: options.detail_level });
  if (options?.canvas_format) entries.push({ label: t("config.canvasFormat"), value: options.canvas_format });
  if (options?.num_pages) entries.push({ label: t("config.numPages"), value: String(options.num_pages) });
  if (options?.max_critic_attempts) entries.push({ label: t("config.maxCriticAttempts"), value: String(options.max_critic_attempts) });
  if (options?.enable_deep_research !== undefined) entries.push({ label: t("config.deepResearch"), value: options.enable_deep_research ? "ON" : "OFF" });
  if (options?.enable_visual_critic !== undefined) entries.push({ label: t("config.visualCritic"), value: options.enable_visual_critic ? "ON" : "OFF" });
  if (options?.enable_visual_critic && options?.visual_qa_max_attempts) entries.push({ label: t("config.visualQaMaxAttempts"), value: String(options.visual_qa_max_attempts) });
  if (options?.enable_icon !== undefined) entries.push({ label: t("config.enableIcon"), value: options.enable_icon ? "ON" : "OFF" });
  if (options?.enable_icon_rag !== undefined) entries.push({ label: t("config.iconRag"), value: options.enable_icon_rag ? "ON" : "OFF" });
  if (options?.research_config && (options.research_config.arxiv_search_enabled || options.research_config.semantic_scholar_enabled || options.research_config.web_search_enabled)) entries.push({ label: t("config.researchEnrichment"), value: "ON" });
  if (options?.template_id) entries.push({ label: t("config.template"), value: options.template_id });
  if (options?.style_overrides?.palette?.length) entries.push({ label: t("config.palette"), value: options.style_overrides.palette.join(", ") });
  if (options?.style_overrides?.font) entries.push({ label: t("config.font"), value: options.style_overrides.font });
  if (options?.style_overrides?.density) entries.push({ label: t("config.density"), value: options.style_overrides.density });
  if (parentJobId) entries.push({ label: t("config.parentJob"), value: parentJobId.slice(0, 8) });

  if (entries.length === 0) {
    return <p className="muted-copy">{t("config.empty")}</p>;
  }

  return (
    <div className="config-viewer">
      {entries.map((entry) => (
        <div key={entry.label} className="config-item">
          <span className="config-label">{entry.label}</span>
          <span className="config-value">{entry.value}</span>
        </div>
      ))}
    </div>
  );
}

function deriveProjectDirFromOutputPath(outputPath?: string | null): string | null {
  if (!outputPath) {
    return null;
  }
  const normalized = outputPath.replace(/\\/g, "/");
  const exportsMarker = "/exports/";
  const idx = normalized.lastIndexOf(exportsMarker);
  if (idx === -1) {
    return null;
  }
  return outputPath.slice(0, idx);
}

function buildStoredJob(historyItem: GenerationHistoryItem): JobStatus {
  return {
    status: historyItem.status,
    progress: historyItem.status === "complete" ? 1 : 0,
    message: historyItem.error ?? "",
    slides_completed: historyItem.slideCount,
    total_slides: historyItem.slideCount,
    output_path: historyItem.outputPath ?? null,
    error: historyItem.status === "error" ? historyItem.error ?? "Job not found." : null,
  };
}

function pickSelectedSlide(slides: PreviewSlide[], selectedSlide?: PreviewSlide) {
  if (!slides.length) {
    return undefined;
  }
  if (!selectedSlide) {
    return slides[0];
  }
  return slides.find((slide) => slide.index === selectedSlide.index) ?? slides[0];
}

function formatStatusLabel(
  status: string | null | undefined,
  locale: "en" | "zh",
  unknownLabel: string,
) {
  return status ? translateStageStatus(status, locale, "history") : unknownLabel;
}
