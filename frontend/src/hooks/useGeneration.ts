import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import { cancelJob, deleteJob, deleteSession, fetchBackendHealth, fetchJobEvents, fetchJobStatus, fetchPreview, fetchProjectPreview, fetchProviders, generatePresentation, interruptGenerationAgent, isNotFoundError, refinePresentation, sendGenerationAgentFeedback, uploadPaper, createSourcesGroup, addFileSources, addTextSource, addUrlSource, getSourcesGroup, removeSource } from "../lib/api";
import type {
  CriticEvent,
  GenerateRequestPayload,
  GenerationOptions,
  GenerationHistoryItem,
  GenerationAgentMessage,
  JobEvent,
  JobStatus,
  PreviewResponse,
  PreviewSlide,
  ProviderListItem,
  RefineRequestPayload,
  ResearchEnrichmentStats,
  ResearchConfig,
  UploadResponse,
  SourcesGroupResponse,
  SourceItem,
  SourceType,
} from "../lib/types";
import { openJobSocket } from "../lib/ws";

type ConnectionStatus = "disconnected" | "connecting" | "connected";
const FINAL_JOB_STATUSES = new Set(["complete", "error", "cancelled"]);
const HISTORY_STORAGE_LIMIT = 50;
const HISTORY_STATUS_SYNC_LIMIT = 8;
const LEGACY_GENERATION_STORAGE_KEY = "paper-ppt-agent-generation-v1";
const GENERATION_STORAGE_KEY = "paper-ppt-agent-generation-v2";
const PERSISTED_LOG_LIMIT = 500;
const PERSISTED_RUN_LOG_LIMIT = 80;
const LIVE_JOB_STORAGE_PREFIX = "paper-ppt-live-job:";
const HISTORY_STATUS_SYNC_TIMEOUT_MS = 4000;
const BACKEND_HEALTH_TIMEOUT_MS = 6000;
const BACKEND_HEALTH_FAILURE_LIMIT = 2;
const PREVIEW_REFRESH_DEBOUNCE_MS = 500;
const HISTORY_SYNC_DEBOUNCE_MS = 750;
const previewRefreshTimers = new Map<string, ReturnType<typeof setTimeout>>();
const historySyncTimers = new Map<string, ReturnType<typeof setTimeout>>();
let backendHealthFailures = 0;

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

function clearHistorySyncTimer(jobId: string) {
  const timer = historySyncTimers.get(jobId);
  if (timer) {
    clearTimeout(timer);
    historySyncTimers.delete(jobId);
  }
}

function scheduleHistorySync(jobId: string, sync: () => void, immediate = false) {
  clearHistorySyncTimer(jobId);
  if (immediate) {
    sync();
    return;
  }
  historySyncTimers.set(
    jobId,
    setTimeout(() => {
      historySyncTimers.delete(jobId);
      sync();
    }, HISTORY_SYNC_DEBOUNCE_MS),
  );
}

function isTerminalJobEvent(event: JobEvent): boolean {
  return (
    event.type === "complete" ||
    event.type === "error" ||
    event.status === "error" ||
    (event.stage === "export" && event.status === "complete") ||
    event.stage === "cancelled"
  );
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
  /** Multi-source group snapshot at generation time, so a run can be
   *  resumed / displayed in history with its source list intact even when
   *  the user has since cleared the Sources panel. */
  sourcesGroup?: SourcesGroupResponse;
  job?: JobStatus;
  slides: PreviewSlide[];
  logs: string[];
  agentMessages: GenerationAgentMessage[];
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

function hasActiveRecoverableRun(runs: Record<string, RunSnapshot>): boolean {
  return Object.values(runs).some((run) => run.job && !FINAL_JOB_STATUSES.has(run.job.status));
}

type StoredRunSnapshot = Partial<RunSnapshot> & { jobId: string };

interface GenerationState {
  uploadSession?: UploadResponse;
  /** Multi-source group (NotebookLM-style Sources panel). Takes precedence
   *  over uploadSession at generation time when non-empty. */
  sourcesGroup?: SourcesGroupResponse;
  providers: ProviderListItem[];
  jobId?: string;
  job?: JobStatus;
  slides: PreviewSlide[];
  logs: string[];
  agentMessages: GenerationAgentMessage[];
  criticEvents: CriticEvent[];
  enrichmentStats?: ResearchEnrichmentStats;
  selectedSlide?: PreviewSlide;
  connectionStatus: ConnectionStatus;
  backendStatus: ConnectionStatus;
  error?: string;
  result?: PreviewResponse;
  history: GenerationHistoryItem[];
  runs: Record<string, RunSnapshot>;
  activeJobId?: string;
  currentRunConfig?: RunConfigSnapshot;
  socketsByJob: Record<string, import("../lib/ws").ReconnectingSocket>;
  checkBackendStatus: () => Promise<void>;
  loadProviders: () => Promise<void>;
  uploadFile: (file: File) => Promise<void>;
  clearUploadSession: () => Promise<void>;
  // Multi-source (Sources panel)
  initSourcesGroup: () => Promise<string>;
  addFiles: (files: File[]) => Promise<void>;
  addText: (payload: { label: string; text: string; role?: "primary" | "supplementary" }) => Promise<void>;
  addUrl: (payload: { url: string; label?: string; role?: "primary" | "supplementary" }) => Promise<{ title: string; charCount: number } | undefined>;
  removeSourceById: (sourceId: string) => Promise<void>;
  clearSourcesGroup: () => Promise<void>;
  startGeneration: (payload: GenerateRequestPayload) => Promise<string>;
  startRefine: (payload: RefineRequestPayload) => Promise<string>;
  cancelCurrentRun: () => Promise<void>;
  interruptCurrentAgent: () => Promise<void>;
  sendAgentFeedback: (message: string, jobId?: string) => Promise<void>;
  connect: (jobId: string, options?: { replayFromStart?: boolean }) => void;
  hydrateAgentHistory: (jobId: string, options?: { force?: boolean }) => Promise<void>;
  hydrateResult: (jobId: string) => Promise<void>;
  refreshHistoryStatuses: () => Promise<void>;
  resumeCurrentRun: (targetJobId?: string) => Promise<boolean>;
  selectSlide: (slide?: PreviewSlide) => void;
  syncHistory: (jobId?: string) => void;
  removeHistory: (jobId: string) => Promise<void>;
  reset: () => void;
  reportError: (message: string) => void;
  dismissError: () => void;
}

function appendSlide(slides: PreviewSlide[], slide: PreviewSlide): PreviewSlide[] {
  const remaining = slides.filter((item) => item.index !== slide.index);
  return [...remaining, slide].sort((left, right) => left.index - right.index);
}

function mergeSlidesByIndex(current: PreviewSlide[], incoming: PreviewSlide[]): PreviewSlide[] {
  if (!incoming.length) {
    return current;
  }
  const byIndex = new Map<number, PreviewSlide>();
  current.forEach((slide) => byIndex.set(slide.index, slide));
  incoming.forEach((slide) => byIndex.set(slide.index, slide));
  return Array.from(byIndex.values()).sort((left, right) => left.index - right.index);
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
  if (incoming.length === 0) {
    return false;
  }
  const currentByIndex = new Map(current.map((slide) => [slide.index, slide]));
  return incoming.some((slide) => {
    const existing = currentByIndex.get(slide.index);
    return !existing || existing.content !== slide.content || existing.name !== slide.name || existing.source !== slide.source;
  });
}

function upsertHistoryItem(history: GenerationHistoryItem[], item: GenerationHistoryItem): GenerationHistoryItem[] {
  return [item, ...history.filter((entry) => entry.jobId !== item.jobId)]
    .sort((left, right) =>
      (right.createdAt ?? right.updatedAt).localeCompare(left.createdAt ?? left.updatedAt),
    )
    .slice(0, HISTORY_STORAGE_LIMIT);
}

/** Summarize a multi-source group into a single display file name for the
 *  history list (e.g. "paper.pdf + 2 more"). Returns undefined when there's
 *  no group, so callers fall through to the legacy uploadSession/jobId. */
function deriveRunFileNameFromSources(group?: SourcesGroupResponse): string | undefined {
  if (!group || group.sources.length === 0) return undefined;
  const first = group.sources[0].label;
  if (group.sources.length === 1) return first;
  return `${first} +${group.sources.length - 1} more`;
}

/** Pick a representative source_type for a multi-source run. "mixed" when
 *  there's more than one distinct kind; otherwise the single kind. */
function deriveRunSourceTypeFromSources(group?: SourcesGroupResponse): SourceType | undefined {
  if (!group || group.sources.length === 0) return undefined;
  const kinds = new Set(group.sources.map((s) => s.kind));
  if (kinds.size === 1) return group.sources[0].kind;
  return "mixed";
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
    fileName:
      run.uploadSession?.file_info.name ??
      deriveRunFileNameFromSources(run.sourcesGroup) ??
      existing?.fileName ??
      run.jobId,
    sourceType:
      run.uploadSession?.file_info.source_type ??
      deriveRunSourceTypeFromSources(run.sourcesGroup) ??
      existing?.sourceType,
    status,
    slideCount,
    createdAt: existing?.createdAt ?? existing?.updatedAt ?? now,
    updatedAt: now,
    projectDir:
      run.result?.project_dir ??
      existing?.projectDir ??
      deriveProjectDirFromOutputPath(run.job?.output_path ?? run.result?.output_path ?? existing?.outputPath),
    outputPath: run.job?.output_path ?? run.result?.output_path ?? existing?.outputPath ?? null,
    provider: run.currentRunConfig?.provider ?? run.job?.provider ?? existing?.provider,
    model: run.currentRunConfig?.model ?? run.job?.model ?? existing?.model,
    baseUrl: run.currentRunConfig?.baseUrl ?? run.job?.base_url ?? existing?.baseUrl,
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
    provider: historyItem.provider,
    model: historyItem.model,
    base_url: historyItem.baseUrl,
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
    agentMessages?: GenerationAgentMessage[];
    criticEvents?: CriticEvent[];
    connectionStatus?: ConnectionStatus;
  },
): RunSnapshot {
  return {
    jobId,
    uploadSession: params?.uploadSession,
    sourcesGroup: params?.sourcesGroup,
    job: params?.job,
    slides: params?.slides ?? [],
    criticEvents: params?.criticEvents ?? [],
    logs: params?.logs ?? [],
    agentMessages: params?.agentMessages ?? [],
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

function isGenerationAgentMessage(value: unknown): value is GenerationAgentMessage {
  if (!value || typeof value !== "object") {
    return false;
  }
  const candidate = value as Partial<GenerationAgentMessage>;
  return (
    typeof candidate.id === "string" &&
    (candidate.role === "user" || candidate.role === "assistant" || candidate.role === "system") &&
    typeof candidate.content === "string" &&
    typeof candidate.created_at === "number" &&
    (!candidate.data || typeof candidate.data === "object")
  );
}

function agentMessageFromEvent(event: JobEvent, options: { includeFeedback?: boolean } = {}): GenerationAgentMessage | null {
  if (event.stage !== "agent") {
    return null;
  }
  const includeFeedback = options.includeFeedback !== false;
  const data = event.data as Record<string, unknown>;
  const feedback = data.agent_feedback as { message?: unknown } | undefined;
  if (feedback && typeof feedback.message === "string") {
    if (!includeFeedback) {
      return null;
    }
    return {
      id: `${event.job_id}:feedback:${event.seq ?? event.ts ?? feedback.message}`,
      role: "user",
      content: feedback.message,
      created_at: event.ts ? event.ts * 1000 : Date.now(),
      kind: "message",
      data: feedback as Record<string, unknown>,
    };
  }
  const rawAgent = data.agent_event as Record<string, unknown> | undefined;
  const payload = (rawAgent?.data && typeof rawAgent.data === "object" ? rawAgent.data : data) as Record<string, unknown>;
  const eventType = typeof rawAgent?.type === "string" ? rawAgent.type : "";
  const eventStatus = typeof rawAgent?.status === "string" ? rawAgent.status : event.status;
  const rawMessage = event.message.trim();
  if (!rawMessage) {
    return null;
  }
  const tool = typeof payload.tool === "string" ? payload.tool : undefined;
  const kind: GenerationAgentMessage["kind"] =
    eventType === "usage"
      ? "usage"
      : eventType === "tool" || tool
        ? "tool"
        : eventType === "message"
          ? "message"
          : "status";
  const toolUseId = typeof payload.tool_use_id === "string" ? payload.tool_use_id : undefined;
  return {
    id: `${event.job_id}:${event.seq ?? `${event.ts ?? Date.now()}:${rawMessage}`}`,
    role: "assistant",
    content: rawMessage,
    created_at: event.ts ? event.ts * 1000 : Date.now(),
    kind,
    status: eventStatus,
    tool,
    toolUseId,
    subagentId: isGenerationSubagentTool(tool) ? toolUseId || "subagent" : undefined,
    data: payload,
  };
}

function isGenerationSubagentTool(tool: string | undefined): boolean {
  if (!tool) return false;
  const normalized = tool.toLowerCase();
  return normalized === "task" || normalized.endsWith(":task");
}

function hasHydratedAgentHistory(run?: RunSnapshot): boolean {
  return Boolean(
    run &&
      typeof run.lastSeq === "number" &&
      run.agentMessages.some((message) => message.role === "assistant" || message.role === "system"),
  );
}

function applyRunToCurrent(run?: RunSnapshot) {
  if (!run) {
    return {};
  }
  const slides = Array.isArray(run.slides) ? run.slides : [];
  const logs = Array.isArray(run.logs) ? run.logs : [];
  const agentMessages = Array.isArray(run.agentMessages) ? run.agentMessages : [];
  const criticEvents = Array.isArray(run.criticEvents) ? run.criticEvents : [];
  return {
    uploadSession: run.uploadSession,
    sourcesGroup: run.sourcesGroup,
    jobId: run.jobId,
    job: run.job,
    slides,
    logs,
    agentMessages,
    criticEvents,
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

function serializeRunsForStorage(
  runs: Record<string, RunSnapshot>,
  history: GenerationHistoryItem[],
  currentJobId?: string,
  activeJobId?: string,
) {
  const keepIds = new Set(history.slice(0, HISTORY_STORAGE_LIMIT).map((entry) => entry.jobId));
  if (currentJobId) keepIds.add(currentJobId);
  if (activeJobId) keepIds.add(activeJobId);
  return Object.fromEntries(
    Object.entries(runs).filter(([jobId]) => keepIds.has(jobId)).map(([jobId, run]) => [
      jobId,
      {
        jobId,
        uploadSession: run.uploadSession,
        sourcesGroup: run.sourcesGroup,
        job: run.job,
        logs: run.logs.slice(-PERSISTED_RUN_LOG_LIMIT),
        agentMessages: run.agentMessages.slice(-PERSISTED_RUN_LOG_LIMIT),
        // Critic details and SVG previews can be large. They are recoverable
        // from backend files/endpoints, so localStorage keeps only run metadata.
        criticEvents: [],
        error: run.error,
        currentRunConfig: run.currentRunConfig,
        connectionStatus: "disconnected" as const,
        lastSeq: run.lastSeq,
      } satisfies StoredRunSnapshot,
    ]),
  );
}

function normalizeStoredRun(jobId: string, run?: Partial<RunSnapshot>): RunSnapshot {
  return createRunSnapshot(jobId, {
    uploadSession: run?.uploadSession,
    sourcesGroup: run?.sourcesGroup,
    job: run?.job,
    slides: [],
    logs: Array.isArray(run?.logs)
      ? run.logs.filter((log): log is string => typeof log === "string").slice(-PERSISTED_RUN_LOG_LIMIT)
      : [],
    agentMessages: Array.isArray(run?.agentMessages)
      ? run.agentMessages.filter(isGenerationAgentMessage).slice(-PERSISTED_RUN_LOG_LIMIT)
      : [],
    criticEvents: [],
    selectedSlide: undefined,
    error: run?.error,
    result: undefined,
    currentRunConfig: run?.currentRunConfig,
    connectionStatus: "disconnected",
    lastSeq: run?.lastSeq,
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

function normalizePersistedGenerationFields(persistedState: unknown): Partial<GenerationState> {
  const state = (persistedState ?? {}) as Partial<GenerationState> & {
    runs?: Record<string, StoredRunSnapshot>;
  };
  const runs = normalizeStoredRuns(state.runs);
  const currentRun = state.jobId ? runs[state.jobId] : undefined;

  // Drop ``paper-ppt-live-job:*`` markers for jobs that no longer appear in
  // our persisted run map. This also protects same-version v2 storage written
  // before run snapshots were normalized on hydration.
  clearOrphanedLiveMarkers(new Set(Object.keys(runs)));

  return {
    uploadSession: state.uploadSession,
    sourcesGroup: state.sourcesGroup,
    jobId: state.jobId,
    job: state.job,
    error: state.error,
    activeJobId: state.activeJobId,
    currentRunConfig: state.currentRunConfig,
    slides: currentRun?.slides ?? [],
    logs: currentRun?.logs ?? [],
    agentMessages: currentRun?.agentMessages ?? [],
    criticEvents: currentRun?.criticEvents ?? [],
    enrichmentStats: currentRun?.enrichmentStats,
    selectedSlide: currentRun?.selectedSlide,
    history: Array.isArray(state.history) ? state.history : [],
    runs,
    connectionStatus: "disconnected" as const,
    backendStatus: "connecting" as const,
    socketsByJob: {},
  };
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

async function fetchBackendHealthWithTimeout(timeoutMs = BACKEND_HEALTH_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetchBackendHealth({ signal: controller.signal });
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
      agentMessages: [],
      criticEvents: [],
      connectionStatus: "disconnected",
      backendStatus: "connecting",
      history: [],
      runs: {},
      socketsByJob: {},
      async checkBackendStatus() {
        set((state) => ({
          backendStatus: state.backendStatus === "connected" ? "connected" : "connecting",
        }));
        try {
          const response = await fetchBackendHealthWithTimeout();
          backendHealthFailures = 0;
          set({
            backendStatus:
              response.status === "ok"
                ? "connected"
                : hasActiveRecoverableRun(get().runs)
                ? "connecting"
                : "disconnected",
          });
        } catch {
          backendHealthFailures += 1;
          set((state) => ({
            backendStatus:
              hasActiveRecoverableRun(state.runs) || backendHealthFailures < BACKEND_HEALTH_FAILURE_LIMIT
                ? "connecting"
                : "disconnected",
          }));
        }
      },
      async loadProviders() {
        try {
          const response = await fetchProviders();
          backendHealthFailures = 0;
          set({ providers: response.providers, backendStatus: "connected" });
        } catch (error) {
          backendHealthFailures += 1;
          set((state) => ({
            backendStatus:
              hasActiveRecoverableRun(state.runs) || backendHealthFailures < BACKEND_HEALTH_FAILURE_LIMIT
                ? "connecting"
                : "disconnected",
          }));
          throw error;
        }
      },
      dismissError() {
        // Clear the global error so the floating banner closes. Per-page
        // local error state (loadError / refineError on ResultPage) is
        // managed via local component state and is unaffected.
        set({ error: undefined });
      },
      reportError(message) {
        set({ error: message || undefined });
      },
      async uploadFile(file) {
        const uploadSession = await uploadPaper(file);
        set({ uploadSession, error: undefined });
      },
      async clearUploadSession() {
        const sessionId = get().uploadSession?.session_id;
        if (sessionId) {
          await deleteSession(sessionId).catch(() => undefined);
        }
        set({ uploadSession: undefined, error: undefined });
      },
      // ── Multi-source actions ──────────────────────────────────────────────
      // Each action keeps sourcesGroup in sync with the backend. The group is
      // created lazily on first add so an empty panel doesn't allocate a
      // session until the user actually adds something.
      async initSourcesGroup() {
        let group = get().sourcesGroup;
        if (!group) {
          group = await createSourcesGroup();
          set({ sourcesGroup: group, error: undefined });
        }
        return group.session_id;
      },
      async addFiles(files) {
        const sessionId = await get().initSourcesGroup();
        const group = await addFileSources(sessionId, files);
        set({ sourcesGroup: group, error: undefined });
      },
      async addText(payload) {
        const sessionId = await get().initSourcesGroup();
        const group = await addTextSource(sessionId, payload);
        set({ sourcesGroup: group, error: undefined });
      },
      async addUrl(payload) {
        const sessionId = await get().initSourcesGroup();
        try {
          const result = await addUrlSource(sessionId, payload);
          // addUrlSource returns the full group via the response's source, but
          // refresh the whole group so ordering/status is consistent.
          const group = await getSourcesGroup(sessionId);
          set({ sourcesGroup: group, error: undefined });
          return { title: result.title, charCount: result.char_count };
        } catch (err) {
          // Re-throw so the panel can show the backend's reason (e.g. bad URL).
          throw err;
        }
      },
      async removeSourceById(sourceId) {
        const group = get().sourcesGroup;
        if (!group) return;
        const updated = await removeSource(group.session_id, sourceId);
        set({ sourcesGroup: updated, error: undefined });
      },
      async clearSourcesGroup() {
        const sessionId = get().sourcesGroup?.session_id;
        if (sessionId) {
          await deleteSession(sessionId).catch(() => undefined);
        }
        set({ sourcesGroup: undefined, error: undefined });
      },
      async startGeneration(payload) {
        const response = await generatePresentation(payload);
        const initialJob = await fetchJobStatus(response.job_id).catch(() => undefined);
        const run = createRunSnapshot(response.job_id, {
          uploadSession: get().uploadSession,
          sourcesGroup: get().sourcesGroup,
          job: {
            status: initialJob?.status ?? "parsing",
            progress: initialJob?.progress ?? 0,
            message: initialJob?.message ?? "Parsing paper...",
            slides_completed: initialJob?.slides_completed ?? 0,
            total_slides: initialJob?.total_slides ?? 0,
            output_path: initialJob?.output_path,
            error: initialJob?.error,
            provider: initialJob?.provider,
            model: initialJob?.model,
            base_url: initialJob?.base_url,
          },
          logs: ["[parsing] Parsing paper..."],
          agentMessages: [],
          currentRunConfig: {
            provider:
              payload.options.generation_backend === "agent"
                ? initialJob?.provider ?? `agent:${payload.options.agent_config?.runtime ?? "claude_code"}`
                : payload.model_config?.provider ?? "unknown",
            model:
              payload.options.generation_backend === "agent"
                ? (initialJob?.model ?? payload.options.agent_config?.model ?? "agent-default")
                : payload.model_config?.model ?? "unknown",
            baseUrl: initialJob?.base_url ?? payload.model_config?.base_url,
            options: payload.options,
            parentJobId: null,
          },
          enrichmentStats:
            payload.options.generation_backend === "agent"
              ? undefined
              : buildEnrichmentPlaceholder(payload.options.research_config),
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
        // Carry the parent run's multi-source group forward so a refined run
        // still shows its full source list. Falls back to reconstructing a
        // legacy single-file uploadSession from history metadata.
        const parentRun = get().runs[payload.job_id];
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
          sourcesGroup: parentRun?.sourcesGroup,
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
          enrichmentStats:
            payload.options.generation_backend === "agent"
              ? undefined
              : buildEnrichmentPlaceholder(payload.options.research_config),
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
          provider: currentRun.job?.provider,
          model: currentRun.job?.model,
          base_url: currentRun.job?.base_url,
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
      async interruptCurrentAgent() {
        const jobId = get().jobId;
        if (!jobId) {
          return;
        }
        const currentRun = get().runs[jobId] ?? createRunSnapshot(jobId);
        const status = currentRun.job?.status;
        if (status && FINAL_JOB_STATUSES.has(status)) {
          return;
        }
        const shouldCancelAgent = status === "paused" || status === "pausing";

        const nextJob: JobStatus = {
          status: shouldCancelAgent ? "cancelling" : "pausing",
          progress: currentRun.job?.progress ?? 0,
          message: shouldCancelAgent ? "Cancelling Agent..." : "Pausing Agent...",
          slides_completed: currentRun.job?.slides_completed ?? currentRun.slides.length,
          total_slides: currentRun.job?.total_slides ?? currentRun.slides.length,
          output_path: currentRun.job?.output_path,
          error: undefined,
          provider: currentRun.job?.provider,
          model: currentRun.job?.model,
          base_url: currentRun.job?.base_url,
        };
        const pausedRun: RunSnapshot = {
          ...currentRun,
          job: nextJob,
          error: undefined,
        };
        set((state) => ({
          ...applyRunToCurrent(pausedRun),
          runs: {
            ...state.runs,
            [jobId]: pausedRun,
          },
        }));
        get().syncHistory(jobId);

        try {
          const response = await interruptGenerationAgent(jobId);
          if (response.status === "cancelled") {
            const latestRun = get().runs[jobId] ?? pausedRun;
            const cancelledJob: JobStatus = {
              ...(latestRun.job ?? nextJob),
              status: "cancelled",
              message: shouldCancelAgent
                ? "Agent task cancelled."
                : "Agent job stopped before a live session was available.",
              error: undefined,
            };
            const cancelledRun: RunSnapshot = {
              ...latestRun,
              job: cancelledJob,
              error: undefined,
            };
            set((state) => ({
              ...applyRunToCurrent(cancelledRun),
              runs: {
                ...state.runs,
                [jobId]: cancelledRun,
              },
            }));
            get().syncHistory(jobId);
          }
        } catch (error) {
          set({
            error: error instanceof Error ? error.message : "Failed to pause Agent",
          });
        }
      },
      async sendAgentFeedback(message, targetJobId) {
        const text = message.trim();
        const jobId = targetJobId ?? get().jobId;
        if (!jobId || !text) {
          return;
        }
        const now = Date.now();
        const optimistic: GenerationAgentMessage = {
          id: `${jobId}:user:${now}`,
          role: "user",
          content: text,
          created_at: now,
          kind: "message",
        };
        set((state) => {
          const currentRun = state.runs[jobId] ?? createRunSnapshot(jobId);
          const updatedRun = {
            ...currentRun,
            agentMessages: [...currentRun.agentMessages, optimistic].slice(-PERSISTED_LOG_LIMIT),
          };
          return {
            ...(state.jobId === jobId ? applyRunToCurrent(updatedRun) : {}),
            runs: {
              ...state.runs,
              [jobId]: updatedRun,
            },
          };
        });
        try {
          const response = await sendGenerationAgentFeedback(jobId, text);
          if (response.status === "injected") {
            set((state) => {
              const currentRun = state.runs[jobId] ?? createRunSnapshot(jobId);
              if (currentRun.job?.status !== "paused") {
                return {};
              }
              const updatedRun: RunSnapshot = {
                ...currentRun,
                job: {
                  ...currentRun.job,
                  status: "generation",
                  message: "Agent is applying guidance...",
                  error: undefined,
                },
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
            if (!get().socketsByJob[jobId]?.isOpen()) {
              get().connect(jobId);
            }
            return;
          }
          if (response.status === "queued") {
            const terminalSocket = get().socketsByJob[jobId];
            terminalSocket?.close();
            set((state) => {
              const currentRun = state.runs[jobId] ?? createRunSnapshot(jobId);
              const nextSockets = { ...state.socketsByJob };
              if (nextSockets[jobId] === terminalSocket) {
                delete nextSockets[jobId];
              }
              const nextJob = currentRun.job
                ? {
                    ...currentRun.job,
                    status: "pending",
                    message: "Queued for Agent feedback revision",
                    progress: Math.max(0.05, currentRun.job.progress ?? 0),
                    error: null,
                  }
                : currentRun.job;
              const updatedRun: RunSnapshot = {
                ...currentRun,
                job: nextJob,
                connectionStatus: "connecting",
                error: undefined,
              };
              return {
                ...(state.jobId === jobId ? applyRunToCurrent(updatedRun) : {}),
                activeJobId: jobId,
                runs: {
                  ...state.runs,
                  [jobId]: updatedRun,
                },
                socketsByJob: nextSockets,
              };
            });
            get().syncHistory(jobId);
            get().connect(jobId);
          }
        } catch (error) {
          set((state) => {
            const currentRun = state.runs[jobId] ?? createRunSnapshot(jobId);
            const failed: GenerationAgentMessage = {
              id: `${jobId}:system:${Date.now()}`,
              role: "system",
              content: error instanceof Error ? error.message : "Failed to send guidance.",
              created_at: Date.now(),
              kind: "status",
            };
            const updatedRun = {
              ...currentRun,
              agentMessages: [...currentRun.agentMessages, failed].slice(-PERSISTED_LOG_LIMIT),
              error: failed.content,
            };
            return {
              ...(state.jobId === jobId ? applyRunToCurrent(updatedRun) : {}),
              runs: {
                ...state.runs,
                [jobId]: updatedRun,
              },
            };
          });
          throw error;
        }
      },
      connect(jobId, options) {
        const existingSocket = get().socketsByJob[jobId];
        const existingRun = get().runs[jobId];
        const replayFromStart = options?.replayFromStart === true;
        if (replayFromStart) {
          seenSeqByJob.delete(jobId);
        }
        const initialSeq = replayFromStart
          ? 0
          : Math.max(existingRun?.lastSeq ?? 0, seenSeqByJob.get(jobId) ?? 0);
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
              const isAgentConsoleEvent =
                event.stage === "agent" && currentRun.currentRunConfig?.provider?.startsWith("agent:");
              let logs =
                !isSnapshot && !isAgentConsoleEvent && event.message && !currentRun.logs.includes(logLine)
                  ? [...currentRun.logs, logLine].slice(-PERSISTED_LOG_LIMIT)
                  : currentRun.logs;
              for (const extra of isAgentConsoleEvent ? [] : buildExtraLogs(event)) {
                if (!isSnapshot && !logs.includes(extra)) {
                  logs = [...logs, extra].slice(-PERSISTED_LOG_LIMIT);
                }
              }
              let agentMessages = currentRun.agentMessages;
              const agentMessage = !isSnapshot ? agentMessageFromEvent(event, { includeFeedback: false }) : null;
              if (agentMessage && !agentMessages.some((item) => item.id === agentMessage.id)) {
                agentMessages = [...agentMessages, agentMessage].slice(-PERSISTED_LOG_LIMIT);
              }

              const completedFromData =
                typeof event.data.completed_count === "number" ? event.data.completed_count : undefined;
              const rawSlidesCompleted = Math.max(event.slides_completed, completedFromData ?? 0);
              const slidesCompleted =
                event.total_slides > 0 ? Math.min(rawSlidesCompleted, event.total_slides) : rawSlidesCompleted;
              const displayStage =
                event.stage === "agent" && currentRun.currentRunConfig?.provider?.startsWith("agent:")
                  ? currentRun.job?.status === "paused" || currentRun.job?.status === "pausing"
                    ? currentRun.job.status
                    : "generation"
                  : event.stage;
              const agentLifecycleStatus =
                event.data.agent_paused === true
                  ? "paused"
                  : event.data.agent_resumed === true
                    ? "generation"
                    : event.data.agent_interrupt === true
                      ? "pausing"
                      : undefined;
              const terminalStatus =
                event.type === "complete" || (event.stage === "export" && event.status === "complete")
                  ? "complete"
                  : event.type === "error" || event.status === "error"
                    ? event.stage === "cancelled"
                      ? "cancelled"
                      : "error"
                    : undefined;
              const stickyLifecycleStatus =
                currentRun.job?.status === "pausing" ||
                currentRun.job?.status === "paused" ||
                currentRun.job?.status === "cancelling"
                  ? currentRun.job.status
                  : undefined;
              const nextJob: JobStatus = {
                status:
                  terminalStatus ?? agentLifecycleStatus ?? stickyLifecycleStatus ?? displayStage,
                progress: event.progress,
                message: event.message,
                slides_completed: slidesCompleted,
                total_slides: event.total_slides,
                output_path:
                  typeof event.data.output_path === "string" ? event.data.output_path : currentRun.job?.output_path,
                error: terminalStatus === "error" ? event.message : undefined,
                provider: currentRun.job?.provider,
                model: currentRun.job?.model,
                base_url: currentRun.job?.base_url,
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
                  const eventKey = (e: CriticEvent) => `${e.page}-${e.attempt}-${e.source ?? "static"}-${e.repair_prompt ? "repair" : "check"}`;
                  const existing = new Set(criticEvents.map(eventKey));
                  const toAdd = newEvents.filter((e) => !existing.has(eventKey(e)));
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
                agentMessages,
                error: terminalStatus === "error" ? event.message : undefined,
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
            scheduleHistorySync(jobId, () => get().syncHistory(jobId), isTerminalJobEvent(event));

            const hasInlineSlideSvg = event.type === "slide_ready" && typeof event.data.svg === "string";
            const shouldRefreshPreview =
              (event.type === "slide_ready" && !hasInlineSlideSvg) ||
              (event.stage === "postprocess" && event.status === "complete");
            if (shouldRefreshPreview) {
              const existingTimer = previewRefreshTimers.get(jobId);
              if (existingTimer) {
                clearTimeout(existingTimer);
              }
              const timer = setTimeout(() => {
                previewRefreshTimers.delete(jobId);
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
                    const mergedSlides = mergeSlidesByIndex(currentRun.slides, preview.slides);
                    const mergedSlideCount = mergedSlides.length;
                    const totalSlides = Math.max(currentRun.job?.total_slides ?? 0, 0);
                    const job = currentRun.job
                      ? {
                          ...currentRun.job,
                          slides_completed: totalSlides > 0
                            ? Math.min(Math.max(currentRun.job.slides_completed, mergedSlideCount), totalSlides)
                            : Math.max(currentRun.job.slides_completed, mergedSlideCount),
                        }
                      : currentRun.job;
                    const updatedRun: RunSnapshot = {
                      ...currentRun,
                      job,
                      result: { ...preview, slides: mergedSlides },
                      slides: mergedSlides,
                      selectedSlide: pickLiveSelectedSlide(currentRun.slides, mergedSlides, currentRun.selectedSlide),
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
              }, PREVIEW_REFRESH_DEBOUNCE_MS);
              previewRefreshTimers.set(jobId, timer);
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
              if (!willReconnect && nextSockets[jobId] === socket) {
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
      async hydrateAgentHistory(jobId, options) {
        const existingRun = get().runs[jobId];
        if (!options?.force && hasHydratedAgentHistory(existingRun)) {
          return;
        }
        const response = await fetchJobEvents(jobId, 0);
        const nextMessagesById = new Map<string, GenerationAgentMessage>();
        for (const message of existingRun?.agentMessages ?? []) {
          nextMessagesById.set(message.id, message);
        }
        let lastSeq = Math.max(existingRun?.lastSeq ?? 0, response.last_seq ?? 0);
        for (const event of response.events) {
          const seq = typeof event.seq === "number" ? event.seq : undefined;
          if (typeof seq === "number" && seq > lastSeq) {
            lastSeq = seq;
          }
          const message = agentMessageFromEvent(event);
          if (message) {
            nextMessagesById.set(message.id, message);
          }
        }
        const agentMessages = Array.from(nextMessagesById.values())
          .sort((left, right) => left.created_at - right.created_at)
          .slice(-PERSISTED_LOG_LIMIT);
        if (lastSeq > 0) {
          seenSeqByJob.set(jobId, lastSeq);
        }
        set((state) => {
          const currentRun = state.runs[jobId] ?? createRunSnapshot(jobId);
          const updatedRun: RunSnapshot = {
            ...currentRun,
            agentMessages,
            lastSeq: lastSeq || currentRun.lastSeq,
          };
          return {
            ...(state.jobId === jobId ? applyRunToCurrent(updatedRun) : {}),
            runs: {
              ...state.runs,
              [jobId]: updatedRun,
            },
          };
        });
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
        const previewTimer = previewRefreshTimers.get(jobId);
        if (previewTimer) {
          clearTimeout(previewTimer);
          previewRefreshTimers.delete(jobId);
        }
        clearHistorySyncTimer(jobId);
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
                  agentMessages: [],
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
        const { jobId, socketsByJob, uploadSession, sourcesGroup } = get();
        // Only delete a source session that has never been used to start a
        // generation job. Completed/history runs retain their session and
        // source snapshot; "new task" merely detaches the current editor.
        if (!jobId) {
          const sessionIds = new Set(
            [uploadSession?.session_id, sourcesGroup?.session_id].filter(
              (sessionId): sessionId is string => Boolean(sessionId),
            ),
          );
          for (const sessionId of sessionIds) {
            void deleteSession(sessionId).catch(() => undefined);
          }
        }
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
            const previewTimer = previewRefreshTimers.get(jobId);
            if (previewTimer) {
              clearTimeout(previewTimer);
              previewRefreshTimers.delete(jobId);
            }
            clearHistorySyncTimer(jobId);
          }
          return {
            uploadSession: undefined,
            sourcesGroup: undefined,
            jobId: undefined,
            job: undefined,
            slides: [],
            logs: [],
            agentMessages: [],
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
      version: 2,
      storage: createJSONStorage(() => {
        clearLegacyGenerationStorage();
        return window.localStorage;
      }),
      migrate: (persistedState) => {
        return normalizePersistedGenerationFields(persistedState);
      },
      merge: (persistedState, currentState) =>
        ({
          ...currentState,
          ...normalizePersistedGenerationFields(persistedState),
        }) satisfies GenerationState,
      partialize: (state) => ({
        uploadSession: state.uploadSession,
        sourcesGroup: state.sourcesGroup,
        jobId: state.jobId,
        job: state.job,
        connectionStatus: "disconnected",
        backendStatus: "connecting",
        error: state.error,
        history: state.history,
        runs: serializeRunsForStorage(state.runs, state.history, state.jobId, state.activeJobId),
        activeJobId: state.activeJobId,
        currentRunConfig: state.currentRunConfig,
      }),
    },
  ),
);
