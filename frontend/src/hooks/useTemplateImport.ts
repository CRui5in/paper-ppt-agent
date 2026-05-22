import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  addAnnotation as addAnnotationApi,
  assistTemplateImport,
  cancelTemplateImportAgent,
  confirmTemplateImport,
  fetchImportStatus,
  fetchTemplateReview,
  generateTemplateImportDirectDesignSpec,
  optimizeTemplateImportWithFeedback,
  previewTemplateImportDraft,
  removeAnnotation as removeAnnotationApi,
  startTemplateImportAgent,
  updateAnnotation as updateAnnotationApi,
  updateTemplateAgentTemplateSvg,
  updateTemplateReview,
  uploadTemplatePptx,
} from "../lib/api";
import { openTemplateAgentSocket, type ReconnectingSocket } from "../lib/ws";
import type {
  GenerateRequestPayload,
  ImportStartResponse,
  ImportStatus,
  TemplateAgentConfig,
  TemplateAgentEvent,
  TemplateAgentStatus,
  TemplatePreview,
  TemplateReview,
  TemplateReviewDraft,
  TemplatePageType,
  UserAnnotation,
} from "../lib/types";

/** Snapshot of preview SVGs returned by `POST /preview`. */
export type DraftPreviewSnapshot = TemplatePreview;

/** Result returned from `confirm()` — alias for the final ImportStatus. */
export type ImportResult = ImportStatus;

type ModelConfig = GenerateRequestPayload["model_config"];

/**
 * Optional knobs for `useTemplateImport`. The design.md hook contract is
 *   useTemplateImport(importId?: string) -> { ... }
 * but the backend `assist` and `feedback` endpoints require a `model_config`.
 * Callers that need LLM-driven assist/feedback pass it via `options`.
 */
export interface UseTemplateImportOptions {
  /** Polling interval while status === "processing". Default 1500ms. */
  pollIntervalMs?: number;
  /** Debounce window for draft → server sync. Default 800ms. */
  debounceMs?: number;
  /** Required by the assist/feedback endpoints. */
  modelConfig?: ModelConfig;
  /** UI copy provider for local activity/error messages. */
  t?: (key: string) => string;
  /** Called when a restored import id no longer exists on the backend. */
  onMissingImport?: (importId: string) => void;
}

export interface UseTemplateImportReturn {
  status: ImportStatus | null;
  review: TemplateReview | null;
  draft: TemplateReviewDraft;
  preview: DraftPreviewSnapshot | null;
  loading: boolean;
  error: string | null;
  /** Upload a .pptx and start an import. Returns the new import_id. */
  upload(
    file: File,
    collaborationMode: "classic" | "agent" | "direct",
    modelConfig?: ModelConfig,
  ): Promise<string>;
  /** Force-refresh status from the backend. */
  refreshStatus(): Promise<void>;
  /** Force-refresh review (reinitialises draft to the server copy). */
  refreshReview(): Promise<TemplateReview | undefined>;
  /** Merge a patch into the local draft (debounced PUT + preview). */
  updateDraft(patch: Partial<TemplateReviewDraft>): void;
  /** Bypass the debounce timer and sync the current draft now. */
  flushDraft(): Promise<void>;
  /** Ask the LLM to refine the draft using `feedback` (no-op if no model). */
  assist(feedback?: string): Promise<void>;
  /** Confirm the draft and persist it as a user template. */
  confirm(): Promise<ImportResult>;
  /** Direct mode: generate design_spec.md from the edited PPT before final confirmation. */
  generateDirectDesignSpec(): Promise<TemplateReview | undefined>;
  /** Re-run a failed pipeline step. Resumes polling on success. */
  retryStep(stepId: string): Promise<void>;
  /** Persist a manually edited Agent template SVG and refresh the preview. */
  saveAgentTemplateSvg(pageType: TemplatePageType, svg: string): Promise<void>;
  /** Run the optional high-autonomy Claude Agent collaboration mode. */
  runAgent(
    feedback: string,
    config: TemplateAgentConfig,
    options?: { silent?: boolean; preview?: boolean; planning?: boolean },
  ): Promise<void>;
  /** Stop the currently running Template Agent job, if any. */
  cancelAgent(): Promise<void>;
  llmEvents: TemplateAgentEvent[];
  agentEvents: TemplateAgentEvent[];
  agentStatus: TemplateAgentStatus | null;
  agentCancelPending: boolean;
}

const DEFAULT_POLL_MS = 1500;
const DEFAULT_DEBOUNCE_MS = 800;

const EMPTY_DRAFT: TemplateReviewDraft = {};

/** Map a server `TemplateReview` into the editable draft shape. */
function deriveDraft(review: TemplateReview): TemplateReviewDraft {
  const src = review.draft ?? {};
  // The server stores `assets` keyed by asset_id with non-optional role/name;
  // the draft type expects them optional, which is a strict subset.
  return {
    label: src.label ?? review.label,
    page_selections: src.page_selections,
    assets: src.assets,
    preserve_texts: src.preserve_texts,
    placeholder_hints: src.placeholder_hints,
    element_actions: src.element_actions,
    design_spec: src.design_spec,
    annotations: review.annotations ?? [],
  };
}

/** Diff two annotation arrays and return the (additions, removals). */
function diffAnnotations(prev: UserAnnotation[], next: UserAnnotation[]): {
  additions: UserAnnotation[];
  updates: UserAnnotation[];
  removalIds: string[];
} {
  const prevById = new Map(prev.map((a) => [a.annotation_id, a]));
  const prevIds = new Set(prev.map((a) => a.annotation_id).filter(Boolean));
  const nextIds = new Set(next.map((a) => a.annotation_id).filter(Boolean));
  const additions = next.filter(
    (a) =>
      !a.annotation_id ||
      a.annotation_id.startsWith("pending-") ||
      !prevIds.has(a.annotation_id),
  );
  const updates = next.filter((a) => {
    if (!a.annotation_id || a.annotation_id.startsWith("pending-")) return false;
    const prior = prevById.get(a.annotation_id);
    if (!prior) return false;
    return JSON.stringify(annotationPatchView(prior)) !== JSON.stringify(annotationPatchView(a));
  });
  const removalIds = prev
    .filter(
      (a) =>
        a.annotation_id &&
        !a.annotation_id.startsWith("pending-") &&
        !nextIds.has(a.annotation_id),
    )
    .map((a) => a.annotation_id);
  return { additions, updates, removalIds };
}

function annotationPatchView(annotation: UserAnnotation) {
  return {
    bbox_norm: annotation.bbox_norm,
    note: annotation.note,
    linked_element_id: annotation.linked_element_id ?? null,
    resolved: Boolean(annotation.resolved),
  };
}

function describeError(e: unknown): string {
  if (e instanceof ApiError) return `${e.status}: ${e.message}`;
  if (e instanceof Error) return e.message;
  return String(e);
}

export function useTemplateImport(
  importId?: string,
  options: UseTemplateImportOptions = {},
): UseTemplateImportReturn {
  const {
    pollIntervalMs = DEFAULT_POLL_MS,
    debounceMs = DEFAULT_DEBOUNCE_MS,
    modelConfig,
    t,
    onMissingImport,
  } = options;

  const [status, setStatus] = useState<ImportStatus | null>(null);
  const [review, setReview] = useState<TemplateReview | null>(null);
  const [draft, setDraft] = useState<TemplateReviewDraft>(EMPTY_DRAFT);
  const [preview, setPreview] = useState<DraftPreviewSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [llmEvents, setLlmEvents] = useState<TemplateAgentEvent[]>([]);
  const [agentEvents, setAgentEvents] = useState<TemplateAgentEvent[]>([]);
  const [agentStatus, setAgentStatus] = useState<TemplateAgentStatus | null>(null);
  const [agentCancelPending, setAgentCancelPending] = useState(false);

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const agentSocketRef = useRef<ReconnectingSocket | null>(null);
  const agentJobIdRef = useRef<string | null>(null);
  const localEventSeqRef = useRef(0);
  const idRef = useRef<string | undefined>(importId);
  idRef.current = importId;
  const draftRef = useRef<TemplateReviewDraft>(draft);
  draftRef.current = draft;
  const reviewRef = useRef<TemplateReview | null>(review);
  reviewRef.current = review;
  const modelConfigRef = useRef<ModelConfig | undefined>(modelConfig);
  modelConfigRef.current = modelConfig;
  const tRef = useRef<typeof t>(t);
  tRef.current = t;
  const statusRef = useRef<ImportStatus | null>(status);
  statusRef.current = status;
  const agentPreviewEnabledRef = useRef(false);
  const onMissingImportRef = useRef<typeof onMissingImport>(onMissingImport);
  onMissingImportRef.current = onMissingImport;
  const annotationSyncRef = useRef<Promise<void>>(Promise.resolve());
  // Track whether we've fetched the review snapshot for the current import,
  // so a transient `review_required` poll doesn't keep refetching it.
  const reviewLoadedForRef = useRef<string | null>(null);

  const waitForAnnotationSync = useCallback(async () => {
    await annotationSyncRef.current.catch(() => undefined);
  }, []);

  const msg = useCallback((key: string, fallback: string) => {
    const translated = tRef.current?.(key);
    return translated && translated !== key ? translated : fallback;
  }, []);

  const closeAgentSocket = useCallback(() => {
    const socket = agentSocketRef.current as ReconnectingSocket | null;
    socket?.close();
    agentSocketRef.current = null;
  }, []);

  const pushLlmEvent = useCallback((message: string, status = "running", stage = "llm") => {
    localEventSeqRef.current += 1;
    const event: TemplateAgentEvent = {
      type: "llm_step",
      seq: localEventSeqRef.current,
      ts: Date.now(),
      stage,
      status,
      message,
    };
    setLlmEvents((prev) => [...prev, event].slice(-60));
  }, []);

  const markImportMissing = useCallback((id: string, e: unknown) => {
    setStatus(null);
    setReview(null);
    setDraft(EMPTY_DRAFT);
    setPreview(null);
    agentPreviewEnabledRef.current = false;
    reviewLoadedForRef.current = null;
    setError(describeError(e));
    onMissingImportRef.current?.(id);
  }, []);

  // ── upload ────────────────────────────────────────────────────────────────
  const upload = useCallback(async (
    file: File,
    collaborationMode: "classic" | "agent" | "direct",
    mc?: ModelConfig,
  ): Promise<string> => {
    if (!file.name.toLowerCase().endsWith(".pptx")) {
      throw new Error(msg("template.invalidFileType", "Only .pptx files are supported."));
    }
    setLoading(true);
    setError(null);
    try {
      const res: ImportStartResponse = await uploadTemplatePptx(file, mc, collaborationMode);
      return res.import_id;
    } catch (e) {
      setError(describeError(e));
      throw e;
    } finally {
      setLoading(false);
    }
  }, []);

  // ── refresh helpers ───────────────────────────────────────────────────────
  const refreshStatus = useCallback(async () => {
    const id = idRef.current;
    if (!id) return;
    try {
      const next = await fetchImportStatus(id);
      setStatus(next);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        markImportMissing(id, e);
        return;
      }
      setError(describeError(e));
    }
  }, [markImportMissing]);

  const refreshReview = useCallback(async () => {
    const id = idRef.current;
    if (!id) return undefined;
    try {
      const next = await fetchTemplateReview(id);
      const nextDraft = deriveDraft(next);
      setReview(next);
      reviewRef.current = next;
      setDraft(nextDraft);
      draftRef.current = nextDraft;
      reviewLoadedForRef.current = id;
      const isAgentImport = statusRef.current?.collaboration_mode === "agent";
      const agentHasPlanned =
        next.llm?.agent === true && next.llm?.status === "complete" && next.llm?.templateized === true;
      if (!isAgentImport || agentHasPlanned || agentPreviewEnabledRef.current) {
        // Eagerly fetch the templated preview so the PPTist review surface
        // can reuse the latest templated output. In Agent mode, keep
        // the starter draft hidden until the user starts an Agent run; then
        // refresh during the run so the preview follows review.json edits.
        try {
          const initialPreview = await previewTemplateImportDraft(
            id,
            nextDraft,
          );
          setPreview(initialPreview);
        } catch {
          /* preview is best-effort; ignore failures here */
        }
      } else {
        setPreview(null);
      }
      return next;
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        markImportMissing(id, e);
        return undefined;
      }
      setError(describeError(e));
      return undefined;
    }
  }, [markImportMissing]);

  // ── poll while processing ─────────────────────────────────────────────────
  useEffect(() => {
    if (!importId) {
      setStatus(null);
      setReview(null);
      setDraft(EMPTY_DRAFT);
      setPreview(null);
      agentPreviewEnabledRef.current = false;
      setLlmEvents([]);
      setAgentEvents([]);
      setAgentStatus(null);
      setAgentCancelPending(false);
      closeAgentSocket();
      reviewLoadedForRef.current = null;
      return;
    }
    // Reset state when switching imports.
    if (reviewLoadedForRef.current !== importId) {
      setReview(null);
      setDraft(EMPTY_DRAFT);
      setPreview(null);
      agentPreviewEnabledRef.current = false;
      setLlmEvents([]);
      setAgentEvents([]);
      setAgentStatus(null);
      setAgentCancelPending(false);
      closeAgentSocket();
      reviewLoadedForRef.current = null;
    }

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      if (cancelled) return;
      try {
        const next = await fetchImportStatus(importId);
        if (cancelled) return;
        setStatus(next);
        if (next.status === "processing") {
          timer = setTimeout(tick, pollIntervalMs);
        }
        // For "review_required" / "complete" / "error" we stop polling.
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 404) {
          markImportMissing(importId, e);
          return;
        }
        setError(describeError(e));
        // Back off and retry on transient failures.
        timer = setTimeout(tick, pollIntervalMs);
      }
    };
    void tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [importId, pollIntervalMs, markImportMissing]);

  // ── review / preview fetches ──────────────────────────────────────────────
  useEffect(() => {
    if (!importId) return;
    const stage = status?.stage ?? "";
    const hasLoadedCurrentImport = reviewLoadedForRef.current === importId;
    const shouldLoadBaseline =
      status?.status === "processing" &&
      ["baseline_preview", "llm_review"].includes(stage) &&
      !hasLoadedCurrentImport;
    const shouldLoadFinalReview =
      status?.status === "review_required" &&
      (!hasLoadedCurrentImport || review?.llm?.status !== "complete");
    if (!shouldLoadBaseline && !shouldLoadFinalReview) return;
    void refreshReview();
  }, [importId, status?.status, status?.stage, review?.llm?.status, refreshReview]);

  // ── debounced draft sync ──────────────────────────────────────────────────
  const sendDraft = useCallback(async (next: TemplateReviewDraft) => {
    const id = idRef.current;
    if (!id) return;
    // Annotations are dispatched immediately via the dedicated CRUD path —
    // exclude them from the debounced PUT/preview to avoid races.
    const { annotations: _annotations, ...rest } = next;
    void _annotations;
    try {
      const updated = await updateTemplateReview(id, rest);
      setReview(updated);
      const previewSnapshot = await previewTemplateImportDraft(id, rest);
      setPreview(previewSnapshot);
    } catch (e) {
      setError(describeError(e));
    }
  }, []);

  const updateDraft = useCallback(
    (patch: Partial<TemplateReviewDraft>) => {
      // Annotations are explicit user actions, not debounced text input —
      // dispatch CRUD calls immediately when the array changes. The
      // backend response (which carries the canonical annotation list) is
      // mirrored back into local state so the bbox_norm round-trip stays
      // tight.
      const patchKeys = Object.keys(patch);
      const annotationsOnly = patchKeys.length === 1 && Array.isArray(patch.annotations);
      if (Array.isArray(patch.annotations)) {
        const id = idRef.current;
        const prev = draftRef.current.annotations ?? [];
        const next = patch.annotations;
        const { additions, updates, removalIds } = diffAnnotations(prev, next);
        if (id && (additions.length || updates.length || removalIds.length)) {
          annotationSyncRef.current = annotationSyncRef.current
            .catch(() => undefined)
            .then(async () => {
            try {
              let latest: UserAnnotation[] = next;
              for (const removalId of removalIds) {
                const res = await removeAnnotationApi(id, removalId);
                latest = res.annotations;
              }
              for (const update of updates) {
                const res = await updateAnnotationApi(id, update.annotation_id, annotationPatchView(update));
                latest = res.annotations;
              }
              for (const addition of additions) {
                const res = await addAnnotationApi(id, {
                  slide_index: addition.slide_index,
                  bbox_norm: addition.bbox_norm,
                  note: addition.note,
                  linked_element_id: addition.linked_element_id ?? null,
                });
                latest = res.annotations;
              }
              setDraft((cur) => ({ ...cur, annotations: latest }));
              draftRef.current = { ...draftRef.current, annotations: latest };
            } catch (e) {
              setError(describeError(e));
            }
          });
        }
      }
      setDraft((prev) => {
        const next: TemplateReviewDraft = { ...prev, ...patch };
        draftRef.current = next;
        if (!annotationsOnly) {
          if (debounceRef.current) clearTimeout(debounceRef.current);
          debounceRef.current = setTimeout(() => {
            debounceRef.current = null;
            void sendDraft(draftRef.current);
          }, debounceMs);
        }
        return next;
      });
    },
    [sendDraft, debounceMs],
  );

  const flushDraft = useCallback(async () => {
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
      debounceRef.current = null;
    }
    await waitForAnnotationSync();
    await sendDraft(draftRef.current);
  }, [sendDraft, waitForAnnotationSync]);

  // ── assist / confirm / retry ──────────────────────────────────────────────
  const assist = useCallback(
    async (feedback?: string) => {
      const id = idRef.current;
      if (!id) return;
      const mc = modelConfigRef.current;
      if (!mc) {
        setError(msg("template.error.modelConfigRequired", "LLM assist requires a model configuration."));
        return;
      }
      setLoading(true);
      setError(null);
      setLlmEvents([]);
      try {
        if (feedback?.trim()) {
          pushLlmEvent(feedback.trim(), "complete", "user");
        }
        pushLlmEvent(msg("template.activity.syncDraft", "Syncing current draft"), "running", "draft");
        await flushDraft();
        pushLlmEvent(msg("template.activity.callLlm", "Calling LLM to optimize template"), "running", "llm");
        const next = feedback
          ? await optimizeTemplateImportWithFeedback(id, mc, feedback, draftRef.current)
          : await assistTemplateImport(id, mc);
        pushLlmEvent(msg("template.activity.llmReturned", "LLM returned an action plan"), "complete", "llm");
        const nextDraft = deriveDraft(next);
        setReview(next);
        setDraft(nextDraft);
        draftRef.current = nextDraft;
        reviewLoadedForRef.current = id;
        try {
          pushLlmEvent(msg("template.activity.refreshPreview", "Refreshing template preview"), "running", "preview");
          const previewSnapshot = await previewTemplateImportDraft(id, nextDraft);
          setPreview(previewSnapshot);
          pushLlmEvent(msg("template.activity.previewUpdated", "Preview updated"), "complete", "preview");
        } catch (previewError) {
          pushLlmEvent(msg("template.activity.previewFailed", "Preview refresh failed"), "error", "preview");
          setError(describeError(previewError));
        }
      } catch (e) {
        pushLlmEvent(describeError(e), "error", "llm");
        setError(describeError(e));
      } finally {
        setLoading(false);
      }
    },
    [flushDraft, msg, pushLlmEvent],
  );

  const confirm = useCallback(async (): Promise<ImportResult> => {
    const id = idRef.current;
    if (!id) throw new Error(msg("template.error.noImportToConfirm", "No template import is active."));
    await flushDraft();
    setLoading(true);
    try {
      const result = await confirmTemplateImport(id);
      setStatus(result);
      return result;
    } catch (e) {
      setError(describeError(e));
      throw e;
    } finally {
      setLoading(false);
    }
  }, [flushDraft, msg]);

  const generateDirectDesignSpec = useCallback(async (): Promise<TemplateReview | undefined> => {
    const id = idRef.current;
    if (!id) throw new Error(msg("template.error.noImportToConfirm", "No template import is active."));
    const mc = modelConfigRef.current;
    if (!mc) {
      setError(msg("template.error.modelConfigRequired", "LLM assist requires a model configuration."));
      return undefined;
    }
    setLoading(true);
    setError(null);
    setLlmEvents([]);
    try {
      pushLlmEvent(msg("template.activity.callLlm", "Calling LLM to optimize template"), "running", "llm");
      const next = await generateTemplateImportDirectDesignSpec(id, mc);
      pushLlmEvent(msg("templates.designSpec.generated", "design_spec.md has been generated and saved."), "complete", "llm");
      const nextDraft = deriveDraft(next);
      setReview(next);
      setDraft(nextDraft);
      draftRef.current = nextDraft;
      reviewLoadedForRef.current = id;
      try {
        const previewSnapshot = await previewTemplateImportDraft(id, nextDraft);
        setPreview(previewSnapshot);
      } catch {
        /* preview is best-effort after design_spec generation */
      }
      return next;
    } catch (e) {
      pushLlmEvent(describeError(e), "error", "llm");
      setError(describeError(e));
      throw e;
    } finally {
      setLoading(false);
    }
  }, [msg, pushLlmEvent]);

  const retryStep = useCallback(
    async (stepId: string) => {
      const id = idRef.current;
      if (!id) return;
      try {
        const res = await fetch(`/api/templates/import/${id}/retry`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ step_id: stepId }),
        });
        // Older backends without a /retry endpoint return 404 — degrade silently.
        if (!res.ok && res.status !== 404) {
          throw new ApiError(`retry failed: ${res.status}`, res.status);
        }
        // Resume polling: a fresh status read will reactivate the poll loop
        // if the backend flipped back to "processing".
        await refreshStatus();
      } catch (e) {
        setError(describeError(e));
      }
    },
    [refreshStatus],
  );

  const saveAgentTemplateSvg = useCallback(async (pageType: TemplatePageType, svg: string) => {
    const id = idRef.current;
    if (!id) return;
    setError(null);
    try {
      const nextPreview = await updateTemplateAgentTemplateSvg(id, pageType, svg);
      setPreview(nextPreview);
    } catch (e) {
      setError(describeError(e));
      throw e;
    }
  }, []);

  const runAgent = useCallback(
    async (
      feedback: string,
      config: TemplateAgentConfig,
      options?: { silent?: boolean; preview?: boolean; planning?: boolean },
    ) => {
      const id = idRef.current;
      if (!id) return;
      const text = feedback.trim();
      if (!text) return;
      setLoading(true);
      setError(null);
      setAgentEvents([]);
      setAgentStatus(null);
      setAgentCancelPending(false);
      closeAgentSocket();
      agentPreviewEnabledRef.current = options?.preview !== false;
      try {
        await flushDraft();
        const start = await startTemplateImportAgent(id, {
          feedback: text,
          config,
          draft: draftRef.current,
          silent: options?.silent,
          planning: options?.planning !== false,
          pptist_version: reviewRef.current?.pptist_version ?? null,
        });
        setAgentStatus({
          agent_job_id: start.agent_job_id,
          import_id: start.import_id,
          status: start.status,
        });
        agentJobIdRef.current = start.agent_job_id;
        await new Promise<void>((resolve, reject) => {
          let socket: ReconnectingSocket | null = null;
          let lastPreviewRefresh = 0;
          let previewRefreshInFlight = false;
          const refreshPreviewSoon = (force = false) => {
            if (options?.preview === false || options?.planning === false) return;
            const now = Date.now();
            if (!force && (previewRefreshInFlight || now - lastPreviewRefresh < 8000)) return;
            lastPreviewRefresh = now;
            previewRefreshInFlight = true;
            void refreshReview().finally(() => {
              previewRefreshInFlight = false;
            });
          };
          socket = openTemplateAgentSocket(
            id,
            start.agent_job_id,
            (event) => {
              if (event.type !== "snapshot") {
                setAgentEvents((prev) => [...prev, event].slice(-80));
              }
              if (event.status || event.message || event.error) {
                setAgentStatus((prev) => ({
                  agent_job_id: event.agent_job_id ?? start.agent_job_id,
                  import_id: event.import_id ?? id,
                  status: event.status ?? prev?.status ?? start.status,
                  message: event.message ?? prev?.message,
                  error: event.error ?? prev?.error,
                  updated_at: event.ts ?? prev?.updated_at,
                }));
              }
              if (event.type === "complete" || event.type === "error" || event.type === "cancelled") {
                setAgentCancelPending(false);
              }
              if (event.type === "artifact_updated") {
                refreshPreviewSoon(true);
              }
              if (event.type === "result" || event.type === "complete") {
                refreshPreviewSoon(true);
              }
              if (event.type === "complete" || event.type === "cancelled") {
                socket?.close();
                resolve();
              }
              if (event.type === "error") {
                socket?.close();
                reject(new Error(event.message || event.error || msg("template.error.agentTaskFailed", "Agent task failed.")));
              }
            },
            undefined,
            undefined,
            () => reject(new Error(msg("template.error.agentStreamDisconnected", "Agent stream disconnected."))),
          );
          agentSocketRef.current = socket;
        });
        await refreshReview();
      } catch (e) {
        setError(describeError(e));
      } finally {
        setLoading(false);
        setAgentCancelPending(false);
        closeAgentSocket();
        agentJobIdRef.current = null;
      }
    },
    [flushDraft, msg, refreshReview],
  );

  const cancelAgent = useCallback(async () => {
    const id = idRef.current;
    const agentJobId = agentStatus?.agent_job_id ?? agentJobIdRef.current;
    if (!id || !agentJobId) return;
    setAgentCancelPending(true);
    try {
      const next = await cancelTemplateImportAgent(id, agentJobId);
      setAgentStatus(next);
    } catch (e) {
      setAgentCancelPending(false);
      setError(describeError(e));
      throw e;
    }
  }, [agentStatus?.agent_job_id]);

  // Cleanup any pending debounce on unmount.
  useEffect(() => {
    return () => {
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
        debounceRef.current = null;
      }
      closeAgentSocket();
    };
  }, []);

  return useMemo(
    () => ({
      status,
      review,
      draft,
      preview,
      loading,
      error,
      upload,
      refreshStatus,
      refreshReview,
      updateDraft,
      flushDraft,
      assist,
      confirm,
      generateDirectDesignSpec,
      retryStep,
      saveAgentTemplateSvg,
      runAgent,
      cancelAgent,
      llmEvents,
      agentEvents,
      agentStatus,
      agentCancelPending,
    }),
    [
      status,
      review,
      draft,
      preview,
      loading,
      error,
      upload,
      refreshStatus,
      refreshReview,
      updateDraft,
      flushDraft,
      assist,
      confirm,
      generateDirectDesignSpec,
      retryStep,
      saveAgentTemplateSvg,
      runAgent,
      cancelAgent,
      llmEvents,
      agentEvents,
      agentStatus,
      agentCancelPending,
    ],
  );
}
