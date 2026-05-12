import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import { cancelJob, deleteJob, fetchJobStatus, fetchPreview, fetchProjectPreview, fetchProviders, generatePresentation, isNotFoundError, refinePresentation, uploadPaper } from "../lib/api";
import type {
  CriticEvent,
  GenerateRequestPayload,
  GenerationOptions,
  GenerationHistoryItem,
  JobEvent,
  JobStatus,
  PreviewResponse,
  PreviewSlide,
  ProviderListItem,
  RefineRequestPayload,
  ResearchEnrichmentStats,
  ResearchConfig,
  UploadResponse,
} from "../lib/types";
import { openJobSocket } from "../lib/ws";

type ConnectionStatus = "disconnected" | "connecting" | "connected";
const FINAL_JOB_STATUSES = new Set(["complete", "error", "cancelled"]);
const HISTORY_STORAGE_LIMIT = 50;
const HISTORY_STATUS_SYNC_LIMIT = 8;
const LEGACY_GENERATION_STORAGE_KEY = "paper-ppt-agent-generation-v1";
const GENERATION_STORAGE_KEY = "paper-ppt-agent-generation-v2";
const PERSISTED_LOG_LIMIT = 200;
const LIVE_JOB_STORAGE_PREFIX = "paper-ppt-live-job:";
const HISTORY_STATUS_SYNC_TIMEOUT_MS = 4000;

/** Per-job seq deduper. Replays after reconnect can re-deliver events
 *  the client already processed; we drop anything with seq <= the last
 *  one we've applied for that job. */
const seenSeqByJob = new Map<string, number>();

function shouldAcceptEventSeq(jobId: string, seq: number | undefined): boolean {
  if (typeof seq !== "number" || seq <= 0) return true;
  const last = seenSeqByJob.get(jobId) ?? 0;
  if (seq <= last) return false;
  seenSeqByJob.set(jobId, seq);
  return true;
}

function clearOrphanedLiveMarkers(activeJobIds: Set<string>) {
  if (typeof window === "undefined") return;
  try {
    const keysToRemove: string[] = [];
    for (let i = 0; i < window.sessionStorage.length; i++) {
      const key = window.sessionStorage.key(i);
      if (!key || !key.startsWith(LIVE_JOB_STORAGE_PREFIX)) continue;
      const jobId = key.slice(LIVE_JOB_STORAGE_PREFIX.length);
      if (!activeJobIds.has(jobId)) {
        keysToRemove.push(key);
      }
    }
    for (const key of keysToRemove) {
      window.sessionStorage.removeItem(key);
    }
  } catch {
    // ignore storage errors (private mode, full quota, etc.)
  }
}

interface RunConfigSnapshot {
  provider: string;
  model: string;
  baseUrl?: string;
  options: GenerationOptions;
  parentJobId?: string | null;
}

interface RunSnapshot {
  jobId: string;
  uploadSession?: UploadResponse;
  job?: JobStatus;
  slides: PreviewSlide[];
  logs: string[];
  criticEvents: CriticEvent[];
  selectedSlide?: PreviewSlide;
  error?: string;
  result?: PreviewResponse;
  currentRunConfig?: RunConfigSnapshot;
  connectionStatus: ConnectionStatus;
  lastSeq?: number;
  // Latest external-research enrichment stats received via the progress
  // channel. Used by ProgressPanel to confirm to the user that the
  // toggles they enabled actually returned data (or to show why not).
  enrichmentStats?: ResearchEnrichmentStats;
}

type StoredRunSnapshot = Partial<RunSnapshot> & { jobId: string };

interface GenerationState {
  uploadSession?: UploadResponse;
  providers: ProviderListItem[];
  jobId?: string;
  job?: JobStatus;
  slides: PreviewSlide[];
  logs: string[];
  criticEvents: CriticEvent[];
  enrichmentStats?: ResearchEnrichmentStats;
  selectedSlide?: PreviewSlide;
  connectionStatus: ConnectionStatus;
  error?: string;
  result?: PreviewResponse;
  history: GenerationHistoryItem[];
  runs: Record<string, RunSnapshot>;
  activeJobId?: string;
  currentRunConfig?: RunConfigSnapshot;
  socketsByJob: Record<string, import("../lib/ws").ReconnectingSocket>;
  loadProviders: () => Promise<void>;
  uploadFile: (file: File) => Promise<void>;
  startGeneration: (payload: GenerateRequestPayload) => Promise<string>;
  startRefine: (payload: RefineRequestPayload) => Promise<string>;
  cancelCurrentRun: () => Promise<void>;
  connect: (jobId: string) => void;
  hydrateResult: (jobId: string) => Promise<void>;
  refreshHistoryStatuses: () => Promise<void>;
  resumeCurrentRun: (targetJobId?: string) => Promise<boolean>;
  selectSlide: (slide?: PreviewSlide) => void;
  syncHistory: (jobId?: string) => void;
  removeHistory: (jobId: string) => Promise<void>;
  reset: () => void;
  dismissError: () => void;
}

function appendSlide(slides: PreviewSlide[], slide: PreviewSlide): PreviewSlide[] {
  const remaining = slides.filter((item) => item.index !== slide.index);
  return [...remaining, slide].sort((left, right) => left.index - right.index);
}

function formatLog(event: JobEvent): string {
  return `[${event.stage}] ${event.message}`;
}

function buildExtraLogs(event: JobEvent): string[] {
  const extras: string[] = [];
  const data = event.data ?? {};
  const parseInfo = (data as { parse_info?: Record<string, unknown> }).parse_info;
  if (parseInfo && typeof parseInfo === "object") {
    const path = String(parseInfo.path ?? "heuristic");
    if (parseInfo.fallback) {
      const reason = String(parseInfo.fallback_reason ?? "heuristic parser insufficient");
      extras.push(`⚠️ [parsing] Layout fallback → ${path}. ${reason}`);
    } else if (parseInfo.fallback_error) {
      extras.push(
        `⚠️ [parsing] Layout-enhanced parse failed; kept heuristic. Error: ${parseInfo.fallback_error}`,
      );
    } else if (parseInfo.layout_available === false) {
      extras.push(
        "[parsing] Layout extension not installed — running heuristic parser only.",
      );
    }
  }
  return extras;
}

function shouldReplaceSlides(current: PreviewSlide[], incoming: PreviewSlide[]): boolean {
  if (incoming.length > current.length) {
    return true;
  }
  if (incoming.length === 0 || current.length === 0) {
    return incoming.length > 0;
  }
  return incoming.some((slide, index) => current[index]?.content !== slide.content);
}

function upsertHistoryItem(history: GenerationHistoryItem[], item: GenerationHistoryItem): GenerationHistoryItem[] {
  return [item, ...history.filter((entry) => entry.jobId !== item.jobId)]
    .sort((left, right) =>
      (right.createdAt ?? right.updatedAt).localeCompare(left.createdAt ?? left.updatedAt),
    )
    .slice(0, HISTORY_STORAGE_LIMIT);
}

function buildHistoryItemFromRun(history: GenerationHistoryItem[], run?: RunSnapshot) {
  if (!run) {
    return undefined;
  }

  const existing = history.find((entry) => entry.jobId === run.jobId);
  const status = deriveRunStatus(run, existing);
  const slideCount = Math.max(
    run.slides.length,
    run.result?.slides.length ?? 0,
    run.job?.slides_completed ?? 0,
    existing?.slideCount ?? 0,
  );

  const now = new Date().toISOString();

  return {
    jobId: run.jobId,
    fileName: run.uploadSession?.file_info.name ?? existing?.fileName ?? run.jobId,
    sourceType: run.uploadSession?.file_info.source_type ?? existing?.sourceType,
    status,
    slideCount,
    createdAt: existing?.createdAt ?? existing?.updatedAt ?? now,
    updatedAt: now,
    projectDir:
      run.result?.project_dir ??
      existing?.projectDir ??
      deriveProjectDirFromOutputPath(run.job?.output_path ?? run.result?.output_path ?? existing?.outputPath),
    outputPath: run.job?.output_path ?? run.result?.output_path ?? existing?.outputPath ?? null,
    provider: run.currentRunConfig?.provider ?? existing?.provider,
    model: run.currentRunConfig?.model ?? existing?.model,
    baseUrl: run.currentRunConfig?.baseUrl ?? existing?.baseUrl,
    options: run.currentRunConfig?.options ?? existing?.options,
    parentJobId: run.currentRunConfig?.parentJobId ?? existing?.parentJobId ?? null,
    // Persist the live error so a failed run, when re-opened from the
    // sidebar, can still show what went wrong. We prefer the most recent
    // signal: live ``run.error`` first, fall back to whatever was stored
    // previously in history. ``null`` clears the slot on success.
    error: run.error ?? existing?.error ?? null,
  } satisfies GenerationHistoryItem;
}

function deriveRunStatus(run: RunSnapshot, existing?: GenerationHistoryItem): string {
  // JobStatus is the authoritative lifecycle state. PreviewResponse.status is
  // only a rendering/result hint and may lag behind after cancellation or
  // failure, so it must never overwrite a terminal job state in history.
  const jobStatus = normalizeLifecycleStatus(run.job?.status);
  if (jobStatus) {
    return jobStatus;
  }
  const resultStatus = normalizeLifecycleStatus(run.result?.status);
  if (resultStatus) {
    return resultStatus;
  }
  return normalizeLifecycleStatus(existing?.status) ?? "pending";
}

function normalizeLifecycleStatus(status?: string | null): string | undefined {
  const value = status?.toLowerCase().trim();
  return value || undefined;
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

function pickLiveSelectedSlide(
  previousSlides: PreviewSlide[],
  nextSlides: PreviewSlide[],
  selectedSlide?: PreviewSlide,
) {
  if (!nextSlides.length) {
    return undefined;
  }
  const previousLast = previousSlides[previousSlides.length - 1];
  const nextLast = nextSlides[nextSlides.length - 1];
  if (!selectedSlide || !previousLast || selectedSlide.index === previousLast.index) {
    return nextLast;
  }
  return pickSelectedSlide(nextSlides, selectedSlide);
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

function buildStoredJob(historyItem: GenerationHistoryItem, result?: PreviewResponse): JobStatus {
  const slideCount = result?.slides.length ?? historyItem.slideCount;
  return {
    status: historyItem.status,
    progress: historyItem.status === "complete" ? 1 : 0,
    message: "",
    slides_completed: slideCount,
    total_slides: slideCount,
    output_path: historyItem.outputPath ?? result?.output_path ?? null,
    // Use the persisted error message if we have one. Falling back to
    // "Job not found." (the previous behaviour) hid the real failure
    // reason when re-opening a failed run from the sidebar.
    error:
      historyItem.error ??
      (historyItem.status === "error" ? "This run failed. The original error message is no longer available." : null),
  };
}

function createRunSnapshot(
  jobId: string,
  params?: Partial<Omit<RunSnapshot, "jobId" | "slides" | "logs" | "criticEvents" | "connectionStatus">> & {
    slides?: PreviewSlide[];
    logs?: string[];
    criticEvents?: CriticEvent[];
    connectionStatus?: ConnectionStatus;
  },
): RunSnapshot {
  return {
    jobId,
    uploadSession: params?.uploadSession,
    job: params?.job,
    slides: params?.slides ?? [],
    criticEvents: params?.criticEvents ?? [],
    logs: params?.logs ?? [],
    selectedSlide: params?.selectedSlide,
    error: params?.error,
    result: params?.result,
    currentRunConfig: params?.currentRunConfig,
    connectionStatus: params?.connectionStatus ?? "disconnected",
    lastSeq: params?.lastSeq,
    enrichmentStats: params?.enrichmentStats,
  };
}

function hasResearchSources(config?: ResearchConfig): boolean {
  return Boolean(
    config?.arxiv_search_enabled ||
      config?.semantic_scholar_enabled ||
      config?.web_search_enabled,
  );
}

function buildEnrichmentPlaceholder(config?: ResearchConfig): ResearchEnrichmentStats | undefined {
  if (!hasResearchSources(config)) {
    return undefined;
  }
  const stats: ResearchEnrichmentStats = { phase: "querying", total_findings: 0, filtered_findings: 0 };
  if (config?.arxiv_search_enabled) {
    stats.arxiv = { found: 0 };
  }
  if (config?.semantic_scholar_enabled) {
    stats.semantic_scholar = { found: 0 };
  }
  if (config?.web_search_enabled) {
    stats.web = {
      found: 0,
      provider: config.tavily_api_key ? "tavily" : config.serpapi_key ? "serpapi" : undefined,
    };
  }
  return stats;
}

function normalizeEnrichmentPayload(raw: unknown, fallback?: ResearchEnrichmentStats) {
  if (!raw || typeof raw !== "object") {
    return fallback;
  }
  return raw as ResearchEnrichmentStats;
}

function applyRunToCurrent(run?: RunSnapshot) {
  if (!run) {
    return {};
  }
  return {
    uploadSession: run.uploadSession,
    jobId: run.jobId,
    job: run.job,
    slides: run.slides,
    logs: run.logs,
    criticEvents: run.criticEvents,
    enrichmentStats: run.enrichmentStats,
    selectedSlide: run.selectedSlide,
    connectionStatus: run.connectionStatus,
    error: run.error,
    result: run.result,
    activeJobId: run.job && FINAL_JOB_STATUSES.has(run.job.status) ? undefined : run.jobId,
    currentRunConfig: run.currentRunConfig,
  };
}

function clearLegacyGenerationStorage() {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.removeItem(LEGACY_GENERATION_STORAGE_KEY);
  } catch {
    // ignore storage cleanup failures
  }
}

function serializeRunsForStorage(runs: Record<string, RunSnapshot>) {
  return Object.fromEntries(
    Object.entries(runs).map(([jobId, run]) => [
      jobId,
      {
        jobId,
        uploadSession: run.uploadSession,
        job: run.job,
        logs: run.logs.slice(-PERSISTED_LOG_LIMIT),
        criticEvents: run.criticEvents,
        error: run.error,
        currentRunConfig: run.currentRunConfig,
        connectionStatus: "disconnected" as const,
      } satisfies StoredRunSnapshot,
    ]),
  );
}

function normalizeStoredRun(jobId: string, run?: Partial<RunSnapshot>): RunSnapshot {
  return createRunSnapshot(jobId, {
    uploadSession: run?.uploadSession,
    job: run?.job,
    slides: Array.isArray(run?.slides) ? run.slides : [],
    logs: Array.isArray(run?.logs)
      ? run.logs.filter((log): log is string => typeof log === "string")
      : [],
    criticEvents: Array.isArray(run?.criticEvents) ? run.criticEvents : [],
    selectedSlide: run?.selectedSlide,
    error: run?.error,
    result: run?.result,
    currentRunConfig: run?.currentRunConfig,
    connectionStatus: "disconnected",
    enrichmentStats: run?.enrichmentStats,
  });
}

function normalizeStoredRuns(runs?: Record<string, StoredRunSnapshot>) {
  if (!runs || typeof runs !== "object") {
    return {} as Record<string, RunSnapshot>;
  }
  return Object.fromEntries(
    Object.entries(runs).map(([jobId, run]) => [jobId, normalizeStoredRun(jobId, run)]),
  );
}

async function fetchJobStatusWithTimeout(jobId: string, timeoutMs = HISTORY_STATUS_SYNC_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetchJobStatus(jobId, { signal: controller.signal });
  } finally {
    window.clearTimeout(timer);
  }
}

export const useGeneration = create<GenerationState>()(
  persist(
    (set, get) => ({
      providers: [],
      slides: [],
      logs: [],
      criticEvents: [],
      connectionStatus: "disconnected",
      history: [],
      runs: {},
      socketsByJob: {},
      async loadProviders() {
        const response = await fetchProviders();
        set({ providers: response.providers });
      },
      dismissError() {
        // Clear the global error so the floating banner closes. Per-page
        // local error state (loadError / refineError on ResultPage) is
        // managed via local component state and is unaffected.
        set({ error: undefined });
      },
      async uploadFile(file) {
        const uploadSession = await uploadPaper(file);
        set({ uploadSession, error: undefined });
      },
      async startGeneration(payload) {
        const response = await generatePresentation(payload);
        const run = createRunSnapshot(response.job_id, {
          uploadSession: get().uploadSession,
          job: {
            status: response.status,
            progress: 0,
            message: "Generation started",
            slides_completed: 0,
            total_slides: 0,
          },
          logs: ["[generate] Generation started"],
          currentRunConfig: {
            provider: payload.model_config.provider,
            model: payload.model_config.model,
            baseUrl: payload.model_config.base_url,
            options: payload.options,
            parentJobId: null,
          },
          enrichmentStats: buildEnrichmentPlaceholder(payload.options.research_config),
          error: undefined,
          result: undefined,
          selectedSlide: undefined,
        });
        set((state) => ({
          ...applyRunToCurrent(run),
          runs: {
            ...state.runs,
            [response.job_id]: run,
          },
        }));
        sessionStorage.setItem(`${LIVE_JOB_STORAGE_PREFIX}${response.job_id}`, "1");
        get().syncHistory(response.job_id);
        return response.job_id;
      },
      async startRefine(payload) {
        const response = await refinePresentation(payload);
        const existingParent = get().history.find((entry) => entry.jobId === payload.job_id);
        const run = createRunSnapshot(response.job_id, {
          uploadSession: existingParent?.sourceType
            ? {
                session_id: payload.job_id,
                file_info: {
                  name: existingParent.fileName,
                  size: 0,
                  source_type: existingParent.sourceType,
                },
              }
            : undefined,
          job: {
            status: response.status,
            progress: 0,
            message: "Refinement started",
            slides_completed: 0,
            total_slides: 0,
          },
          logs: ["[refine] Refinement started"],
          currentRunConfig: {
            provider: payload.model_config.provider,
            model: payload.model_config.model,
            baseUrl: payload.model_config.base_url,
            options: payload.options,
            parentJobId: payload.job_id,
          },
          enrichmentStats: buildEnrichmentPlaceholder(payload.options.research_config),
          error: undefined,
          result: undefined,
          selectedSlide: undefined,
        });
        set((state) => ({
          ...applyRunToCurrent(run),
          runs: {
            ...state.runs,
            [response.job_id]: run,
          },
        }));
        sessionStorage.setItem(`${LIVE_JOB_STORAGE_PREFIX}${response.job_id}`, "1");
        get().syncHistory(response.job_id);
        return response.job_id;
      },
      async cancelCurrentRun() {
        const jobId = get().jobId;
        if (!jobId) {
          return;
        }
        const currentRun = get().runs[jobId] ?? createRunSnapshot(jobId);
        const status = currentRun.job?.status;
        if (status && FINAL_JOB_STATUSES.has(status)) {
          return;
        }

        const nextJob: JobStatus = {
          status: "cancelling",
          progress: currentRun.job?.progress ?? 0,
          message: "Cancelling generation...",
          slides_completed: currentRun.job?.slides_completed ?? currentRun.slides.length,
          total_slides: currentRun.job?.total_slides ?? currentRun.slides.length,
          output_path: currentRun.job?.output_path,
          error: undefined,
        };
        const cancellingRun: RunSnapshot = {
          ...currentRun,
          job: nextJob,
          error: undefined,
        };
        set((state) => ({
          ...applyRunToCurrent(cancellingRun),
          runs: {
            ...state.runs,
            [jobId]: cancellingRun,
          },
        }));
        get().syncHistory(jobId);

        try {
          await cancelJob(jobId);
        } catch (error) {
          set({
            error: error instanceof Error ? error.message : "Failed to cancel generation",
          });
        }
      },
      connect(jobId) {
        const existingSocket = get().socketsByJob[jobId];
        const existingRun = get().runs[jobId];
        const initialSeq = Math.max(existingRun?.lastSeq ?? 0, seenSeqByJob.get(jobId) ?? 0);
        if (initialSeq > 0) {
          seenSeqByJob.set(jobId, initialSeq);
        }

        if (existingSocket) {
          const isOpen = existingSocket.isOpen();
          set((state) => ({
            ...applyRunToCurrent({
              ...(existingRun ?? createRunSnapshot(jobId)),
              connectionStatus: isOpen ? "connected" : "connecting",
            }),
            runs: existingRun
              ? {
                  ...state.runs,
                  [jobId]: {
                    ...existingRun,
                    connectionStatus: isOpen ? "connected" : "connecting",
                  },
                }
              : state.runs,
          }));
          return;
        }

        set((state) => ({
          ...applyRunToCurrent(existingRun ?? createRunSnapshot(jobId, { connectionStatus: "connecting" })),
          runs: {
            ...state.runs,
            [jobId]: {
              ...(existingRun ?? createRunSnapshot(jobId)),
              connectionStatus: "connecting",
            },
          },
        }));

        const socket = openJobSocket(
          jobId,
          (event) => {
            const seq = (event as { seq?: number }).seq;
            if (!shouldAcceptEventSeq(jobId, seq)) {
              return;
            }
            set((state) => {
              const currentRun = state.runs[jobId] ?? createRunSnapshot(jobId);
              const isSnapshot = typeof seq !== "number" && typeof event.last_seq === "number";
              const logLine = formatLog(event);
              let logs =
                !isSnapshot && event.message && !currentRun.logs.includes(logLine)
                  ? [...currentRun.logs, logLine]
                  : currentRun.logs;
              for (const extra of buildExtraLogs(event)) {
                if (!isSnapshot && !logs.includes(extra)) {
                  logs = [...logs, extra];
                }
              }

              const nextJob: JobStatus = {
                status: event.type === "complete" ? "complete" : event.type === "error" ? "error" : event.stage,
                progress: event.progress,
                message: event.message,
                slides_completed: event.slides_completed,
                total_slides: event.total_slides,
                output_path:
                  typeof event.data.output_path === "string" ? event.data.output_path : currentRun.job?.output_path,
                error: event.type === "error" ? event.message : undefined,
              };

              let slides = currentRun.slides;
              if (event.type === "slide_ready" && typeof event.data.svg === "string") {
                slides = appendSlide(currentRun.slides, {
                  index: Number(event.data.page ?? currentRun.slides.length + 1),
                  name: `slide_${event.data.page ?? currentRun.slides.length + 1}`,
                  source: "output",
                  content: String(event.data.svg),
                });
              }

              // Accumulate critic events from progress and complete events
              let criticEvents = currentRun.criticEvents;
              const rawCritic = event.data.critic;
              if (Array.isArray(rawCritic) && rawCritic.length > 0) {
                const newEvents = rawCritic as CriticEvent[];
                if (event.type === "complete") {
                  // Complete event carries the full list — replace
                  criticEvents = newEvents;
                } else {
                  // Progress events carry per-page events — merge by dedup
                  const existing = new Set(criticEvents.map((e) => `${e.page}-${e.attempt}`));
                  const toAdd = newEvents.filter((e) => !existing.has(`${e.page}-${e.attempt}`));
                  criticEvents = [...criticEvents, ...toAdd];
                }
              }

              // Capture external-research enrichment stats. Pipeline emits
              // these once per run during the research stage so users see
              // that the toggles they enabled actually returned data.
              let enrichmentStats = currentRun.enrichmentStats;
              const rawEnrichment = (event.data as { enrichment?: unknown }).enrichment;
              if (rawEnrichment && typeof rawEnrichment === "object") {
                enrichmentStats = normalizeEnrichmentPayload(rawEnrichment, enrichmentStats);
              }

              const updatedRun: RunSnapshot = {
                ...currentRun,
                job: nextJob,
                slides,
                criticEvents,
                enrichmentStats,
                selectedSlide:
                  event.type === "slide_ready"
                    ? pickLiveSelectedSlide(currentRun.slides, slides, currentRun.selectedSlide)
                    : pickSelectedSlide(slides, currentRun.selectedSlide),
                logs,
                error: event.type === "error" ? event.message : undefined,
                connectionStatus: FINAL_JOB_STATUSES.has(nextJob.status) ? "disconnected" : currentRun.connectionStatus,
                lastSeq: typeof seq === "number" && seq > 0 ? Math.max(currentRun.lastSeq ?? 0, seq) : currentRun.lastSeq,
              };

              return {
                ...(state.jobId === jobId ? applyRunToCurrent(updatedRun) : {}),
                runs: {
                  ...state.runs,
                  [jobId]: updatedRun,
                },
              };
            });
            get().syncHistory(jobId);

            if (
              event.stage === "generation" ||
              (event.stage === "postprocess" && event.status === "complete")
            ) {
              void fetchPreview(jobId)
                .then((preview) => {
                  set((state) => {
                    const currentRun = state.runs[jobId] ?? createRunSnapshot(jobId);
                    if (!shouldReplaceSlides(currentRun.slides, preview.slides)) {
                      return state.jobId === jobId
                        ? {
                            ...(state.jobId === jobId ? applyRunToCurrent(currentRun) : {}),
                          }
                        : {};
                    }
                    const updatedRun: RunSnapshot = {
                      ...currentRun,
                      result: preview,
                      slides: preview.slides,
                      selectedSlide: pickLiveSelectedSlide(currentRun.slides, preview.slides, currentRun.selectedSlide),
                    };
                    return {
                      ...(state.jobId === jobId ? applyRunToCurrent(updatedRun) : {}),
                      runs: {
                        ...state.runs,
                        [jobId]: updatedRun,
                      },
                    };
                  });
                  get().syncHistory(jobId);
                })
                .catch(() => undefined);
            }

            if (event.type === "complete") {
              void get().hydrateResult(jobId);
            }
          },
          () =>
            set((state) => {
              const run = state.runs[jobId] ?? createRunSnapshot(jobId);
              const updatedRun = { ...run, connectionStatus: "connected" as const };
              return {
                ...(state.jobId === jobId ? applyRunToCurrent(updatedRun) : {}),
                runs: {
                  ...state.runs,
                  [jobId]: updatedRun,
                },
              };
            }),
          (willReconnect) =>
            set((state) => {
              const run = state.runs[jobId] ?? createRunSnapshot(jobId);
              const updatedRun = { ...run, connectionStatus: willReconnect ? "connecting" as const : "disconnected" as const };
              const nextSockets = { ...state.socketsByJob };
              if (!willReconnect) {
                delete nextSockets[jobId];
              }
              return {
                ...(state.jobId === jobId ? applyRunToCurrent(updatedRun) : {}),
                runs: {
                  ...state.runs,
                  [jobId]: updatedRun,
                },
                socketsByJob: nextSockets,
              };
            }),
          () =>
            // We've exhausted reconnect attempts. Surface a global error
            // so the UI banner explains what happened — the user can
            // refresh to retry, or open the run from history once the
            // backend is reachable again.
            set((state) => ({
              ...state,
              error:
                "Lost connection to the server and could not reconnect. " +
                "Refresh the page or check your network and try again.",
            })),
          initialSeq,
        );

        set((state) => ({
          socketsByJob: {
            ...state.socketsByJob,
            [jobId]: socket,
          },
        }));
      },
      async hydrateResult(jobId) {
        const historyEntry = get().history.find((entry) => entry.jobId === jobId);
        const projectDir =
          historyEntry?.projectDir ?? deriveProjectDirFromOutputPath(historyEntry?.outputPath);

        const [result, job] = await Promise.all([
          fetchPreview(jobId).catch(async () => {
            if (!projectDir) {
              throw new Error("Result not found.");
            }
            return fetchProjectPreview(projectDir);
          }),
          fetchJobStatus(jobId).catch(() => {
            if (!historyEntry) {
              throw new Error("Job not found.");
            }
            return buildStoredJob(historyEntry);
          }),
        ]);
        set((state) => {
          const currentRun = state.runs[jobId] ?? createRunSnapshot(jobId);
          const updatedRun: RunSnapshot = {
            ...currentRun,
            result,
            job,
            slides: result.slides,
            selectedSlide: pickSelectedSlide(result.slides, currentRun.selectedSlide),
            error: job.error ?? undefined,
            connectionStatus: FINAL_JOB_STATUSES.has(job.status) ? "disconnected" : currentRun.connectionStatus,
          };
          return {
            ...(state.jobId === jobId ? applyRunToCurrent(updatedRun) : {}),
            runs: {
              ...state.runs,
              [jobId]: updatedRun,
            },
          };
        });
        get().syncHistory(jobId);
      },
      async refreshHistoryStatuses() {
        const candidates = get()
          .history.filter((entry) => !FINAL_JOB_STATUSES.has(entry.status.toLowerCase()))
          .slice(0, HISTORY_STATUS_SYNC_LIMIT);
        if (!candidates.length) {
          return;
        }

        await Promise.all(
          candidates.map(async (entry) => {
            try {
              const job = await fetchJobStatusWithTimeout(entry.jobId);
              set((state) => {
                const currentRun =
                  state.runs[entry.jobId] ??
                  createRunSnapshot(entry.jobId, {
                    job: buildStoredJob(entry),
                    error: entry.error ?? undefined,
                  });
                const updatedRun: RunSnapshot = {
                  ...currentRun,
                  job,
                  error: job.error ?? undefined,
                  connectionStatus: FINAL_JOB_STATUSES.has(job.status)
                    ? "disconnected"
                    : currentRun.connectionStatus,
                };
                const nextItem = buildHistoryItemFromRun(state.history, updatedRun);
                return {
                  ...(state.jobId === entry.jobId ? applyRunToCurrent(updatedRun) : {}),
                  runs: {
                    ...state.runs,
                    [entry.jobId]: updatedRun,
                  },
                  history: nextItem ? upsertHistoryItem(state.history, nextItem) : state.history,
                  activeJobId:
                    state.activeJobId === entry.jobId && FINAL_JOB_STATUSES.has(job.status)
                      ? undefined
                      : state.activeJobId,
                };
              });
              if (job.status === "complete") {
                void get().hydrateResult(entry.jobId).catch(() => undefined);
              }
            } catch (error) {
              if (isNotFoundError(error)) {
                try {
                  sessionStorage.removeItem(`${LIVE_JOB_STORAGE_PREFIX}${entry.jobId}`);
                } catch {
                  /* noop */
                }
              }
            }
          }),
        );
      },
      async resumeCurrentRun(targetJobId) {
        const currentJobId = targetJobId ?? get().activeJobId ?? get().jobId;
        if (!currentJobId) {
          return false;
        }

        const currentRun = get().runs[currentJobId];
        const currentJob = currentRun?.job ?? get().job;
        if (
          !targetJobId &&
          ((get().socketsByJob[currentJobId] && currentRun?.connectionStatus !== "disconnected") ||
            (currentJob && FINAL_JOB_STATUSES.has(currentJob.status)))
        ) {
          if (currentRun) {
            set(() => ({
              ...applyRunToCurrent(currentRun),
            }));
          }
          return true;
        }

        try {
          const [job, preview] = await Promise.all([fetchJobStatus(currentJobId), fetchPreview(currentJobId).catch(() => undefined)]);

          set((state) => {
            const run = state.runs[currentJobId] ?? createRunSnapshot(currentJobId);
            const nextSlides = preview?.slides ?? run.slides;
            const updatedRun: RunSnapshot = {
              ...run,
              job,
              result: preview ?? run.result,
              slides: nextSlides,
              selectedSlide: pickSelectedSlide(nextSlides, run.selectedSlide),
              error: job.error ?? undefined,
              connectionStatus: FINAL_JOB_STATUSES.has(job.status) ? "disconnected" : run.connectionStatus,
            };
            return {
              ...applyRunToCurrent(updatedRun),
              runs: {
                ...state.runs,
                [currentJobId]: updatedRun,
              },
            };
          });
          get().syncHistory(currentJobId);

          if (job.status === "complete") {
            await get().hydrateResult(currentJobId);
            return true;
          }

          if (job.status === "error") {
            return true;
          }

          get().connect(currentJobId);
          return true;
        } catch (error) {
          // 404 means the backend no longer knows this job — most often
          // because the process restarted between page loads. Detach the
          // local "active" pointer and surface a soft hint instead of a
          // raw error: history still has the entry, the user can choose
          // to remove it or start a new run.
          if (isNotFoundError(error)) {
            set((state) => {
              const isCurrent = state.jobId === currentJobId;
              try {
                sessionStorage.removeItem(`${LIVE_JOB_STORAGE_PREFIX}${currentJobId}`);
              } catch {
                /* noop */
              }
              return {
                ...(isCurrent
                  ? {
                      jobId: undefined,
                      job: undefined,
                      slides: [],
                      logs: [],
                      selectedSlide: undefined,
                      result: undefined,
                      currentRunConfig: undefined,
                    }
                  : {}),
                activeJobId: state.activeJobId === currentJobId ? undefined : state.activeJobId,
                connectionStatus: "disconnected",
                error: undefined,
              };
            });
            return false;
          }
          set({
            connectionStatus: "disconnected",
            error: error instanceof Error ? error.message : "Failed to resume generation",
          });
          return false;
        }
      },
      selectSlide(slide) {
        set((state) => {
          if (!state.jobId) {
            return { selectedSlide: slide };
          }
          const currentRun = state.runs[state.jobId];
          if (!currentRun) {
            return { selectedSlide: slide };
          }
          return {
            selectedSlide: slide,
            runs: {
              ...state.runs,
              [state.jobId]: {
                ...currentRun,
                selectedSlide: slide,
              },
            },
          };
        });
      },
      syncHistory(targetJobId) {
        const jobId = targetJobId ?? get().jobId;
        const nextItem = buildHistoryItemFromRun(get().history, jobId ? get().runs[jobId] : undefined);
        if (!nextItem) {
          return;
        }
        set((state) => ({
          history: upsertHistoryItem(state.history, nextItem),
          activeJobId:
            state.jobId === nextItem.jobId && FINAL_JOB_STATUSES.has(nextItem.status) ? undefined : state.activeJobId,
        }));
      },
      async removeHistory(jobId) {
        const run = get().runs[jobId];
        const status = run?.job?.status ?? get().history.find((entry) => entry.jobId === jobId)?.status;
        if (status && !FINAL_JOB_STATUSES.has(status)) {
          await cancelJob(jobId).catch(() => undefined);
        }
        await deleteJob(jobId).catch(() => undefined);
        const socket = get().socketsByJob[jobId];
        socket?.close();
        set((state) => {
          const nextRuns = { ...state.runs };
          delete nextRuns[jobId];
          const nextSockets = { ...state.socketsByJob };
          delete nextSockets[jobId];
          const isCurrent = state.jobId === jobId;
          return {
            history: state.history.filter((entry) => entry.jobId !== jobId),
            runs: nextRuns,
            socketsByJob: nextSockets,
            ...(isCurrent
              ? {
                  uploadSession: undefined,
                  jobId: undefined,
                  job: undefined,
                  slides: [],
                  logs: [],
                  criticEvents: [],
                  selectedSlide: undefined,
                  connectionStatus: "disconnected" as const,
                  error: undefined,
                  result: undefined,
                  activeJobId: undefined,
                  currentRunConfig: undefined,
                }
              : {}),
          };
        });
        sessionStorage.removeItem(`${LIVE_JOB_STORAGE_PREFIX}${jobId}`);
        seenSeqByJob.delete(jobId);
      },
      reset() {
        const { jobId, socketsByJob } = get();
        if (jobId) {
          // Tear down any live socket for the run we're about to abandon
          // so it doesn't keep mutating store state in the background.
          socketsByJob[jobId]?.close();
          try {
            sessionStorage.removeItem(`${LIVE_JOB_STORAGE_PREFIX}${jobId}`);
          } catch {
            /* noop */
          }
          seenSeqByJob.delete(jobId);
        }
        set((state) => {
          const nextSockets = { ...state.socketsByJob };
          if (jobId) {
            delete nextSockets[jobId];
          }
          return {
            uploadSession: undefined,
            jobId: undefined,
            job: undefined,
            slides: [],
            logs: [],
            criticEvents: [],
            selectedSlide: undefined,
            connectionStatus: "disconnected",
            error: undefined,
            result: undefined,
            activeJobId: undefined,
            currentRunConfig: undefined,
            socketsByJob: nextSockets,
          };
        });
      },
    }),
    {
      name: GENERATION_STORAGE_KEY,
      version: 1,
      storage: createJSONStorage(() => {
        clearLegacyGenerationStorage();
        return window.localStorage;
      }),
      migrate: (persistedState) => {
        const state = (persistedState ?? {}) as Partial<GenerationState> & {
          runs?: Record<string, StoredRunSnapshot>;
        };
        const runs = normalizeStoredRuns(state.runs);
        const currentRun = state.jobId ? runs[state.jobId] : undefined;

        // Drop ``paper-ppt-live-job:*`` markers for jobs that no longer
        // appear in our persisted run map — they accumulated from prior
        // sessions where the page was killed before cleanup ran.
        clearOrphanedLiveMarkers(new Set(Object.keys(runs)));

        return {
          ...state,
          slides: currentRun?.slides ?? [],
          logs: currentRun?.logs ?? [],
          criticEvents: currentRun?.criticEvents ?? [],
          selectedSlide: currentRun?.selectedSlide,
          history: Array.isArray(state.history) ? state.history : [],
          runs,
          connectionStatus: "disconnected" as const,
          socketsByJob: {},
        };
      },
      partialize: (state) => ({
        uploadSession: state.uploadSession,
        jobId: state.jobId,
        job: state.job,
        connectionStatus: "disconnected",
        error: state.error,
        history: state.history,
        runs: serializeRunsForStorage(state.runs),
        activeJobId: state.activeJobId,
        currentRunConfig: state.currentRunConfig,
      }),
    },
  ),
);
