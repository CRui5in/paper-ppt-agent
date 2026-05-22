import type {
  CancelJobResponse,
  DeckCheckResult,
  DeckImageAsset,
  DeckScene,
  GenerateRequestPayload,
  GenerateResponse,
  ImageApplyRequest,
  ImageApplyResponse,
  ImageSearchRequest,
  ImageSearchResponse,
  ImageUndoResponse,
  ImportStartResponse,
  ImportStatus,
  JobStatus,
  PreviewResponse,
  PreviewSlide,
  PptistDeckPayload,
  PptistSaveResult,
  SlideDocument,
  SlideScene,
  SlideSceneOperation,
  ProvidersResponse,
  ReexportResponse,
  RefineRequestPayload,
  RefineResponse,
  TemplateReview,
  TemplateReviewDraft,
  TemplateInfo,
  TemplateAgentConfig,
  TemplateAgentClaudeCodeStatus,
  TemplateAgentStartResponse,
  TemplateAgentStatus,
  TemplateImportFileList,
  TemplatePreview,
  TemplatePageType,
  UpdateFontsRequest,
  UpdateFontsResponse,
  UploadResponse,
  UserAnnotation,
  UserTemplateItem,
  VersionDetailResponse,
  VersionsResponse,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

export interface UsageSummaryResponse {
  total_calls: number;
  total_prompt: number;
  total_completion: number;
  total_tokens: number;
}

export interface UsageDailyRowResponse {
  day: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface UsageModelRowResponse {
  model: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface UsageStageRowResponse {
  stage: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface UsageRecordResponse {
  ts: string;
  day: string;
  provider: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  job_id: string | null;
  stage: string | null;
  page: number | null;
  attempt: number;
  duration_ms: number;
}

export interface UsageSnapshotResponse {
  summary: UsageSummaryResponse;
  daily: UsageDailyRowResponse[];
  by_model: UsageModelRowResponse[];
  by_stage: UsageStageRowResponse[];
  recent: UsageRecordResponse[];
}

export interface HealthResponse {
  status: string;
}

/**
 * Error thrown for non-2xx HTTP responses.
 *
 * Carries the HTTP ``status`` so callers can distinguish "the resource
 * is gone" (404 — likely server restart or job GC) from transient
 * network/server errors and degrade the UI accordingly.
 */
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function isNotFoundError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    cache: "no-store",
    ...init,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new ApiError(detail || `Request failed: ${response.status}`, response.status);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export async function uploadPaper(file: File): Promise<UploadResponse> {
  const formData = new FormData();
  formData.append("file", file);
  return request<UploadResponse>("/api/upload", {
    method: "POST",
    body: formData,
  });
}

export async function fetchProviders(): Promise<ProvidersResponse> {
  return request<ProvidersResponse>("/api/providers");
}

export async function fetchBackendHealth(init?: RequestInit): Promise<HealthResponse> {
  return request<HealthResponse>("/healthz", init);
}

export async function fetchTemplates(): Promise<TemplateInfo[]> {
  return request<TemplateInfo[]>("/api/templates");
}

export async function uploadTemplatePptx(
  file: File,
  modelConfig: GenerateRequestPayload["model_config"] | undefined,
  collaborationMode: "classic" | "agent" | "direct" = "direct",
): Promise<ImportStartResponse> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("collaboration_mode", collaborationMode);
  if (modelConfig) {
    formData.append("model_config", JSON.stringify(modelConfig));
  }
  return request<ImportStartResponse>("/api/templates/upload", {
    method: "POST",
    body: formData,
  });
}

export async function fetchTemplateAgentClaudeCodeStatus(): Promise<TemplateAgentClaudeCodeStatus> {
  return request<TemplateAgentClaudeCodeStatus>("/api/templates/agent/claude-code/status");
}

export async function fetchImportStatus(importId: string): Promise<ImportStatus> {
  return request<ImportStatus>(`/api/templates/import/${importId}`);
}

export async function fetchTemplateReview(importId: string): Promise<TemplateReview> {
  return request<TemplateReview>(`/api/templates/import/${importId}/review`);
}

export async function updateTemplateReview(
  importId: string,
  draft: TemplateReviewDraft,
): Promise<TemplateReview> {
  return request<TemplateReview>(`/api/templates/import/${importId}/review`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(draft),
  });
}

export async function assistTemplateImport(
  importId: string,
  modelConfig: GenerateRequestPayload["model_config"],
): Promise<TemplateReview> {
  return request<TemplateReview>(`/api/templates/import/${importId}/assist`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model_config: modelConfig }),
  });
}

export async function optimizeTemplateImportWithFeedback(
  importId: string,
  modelConfig: GenerateRequestPayload["model_config"],
  feedback: string,
  draft?: TemplateReviewDraft,
): Promise<TemplateReview> {
  return request<TemplateReview>(`/api/templates/import/${importId}/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model_config: modelConfig, feedback, draft }),
  });
}

export async function generateTemplateImportDirectDesignSpec(
  importId: string,
  modelConfig: GenerateRequestPayload["model_config"],
): Promise<TemplateReview> {
  return request<TemplateReview>(`/api/templates/import/${importId}/direct-design-spec`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model_config: modelConfig }),
  });
}

export async function startTemplateImportAgent(
  importId: string,
  payload: {
    feedback: string;
    config: TemplateAgentConfig;
    draft?: TemplateReviewDraft;
    /** If true, the backend will not append ``feedback`` to the visible
     * chat conversation. Used for the auto-kickoff template-ization run. */
    silent?: boolean;
    /** False for the automatic read-only inspection run. */
    planning?: boolean;
    pptist_version?: string | null;
  },
): Promise<TemplateAgentStartResponse> {
  return request<TemplateAgentStartResponse>(`/api/templates/import/${importId}/agent`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function fetchTemplateImportAgentStatus(
  importId: string,
  agentJobId: string,
): Promise<TemplateAgentStatus> {
  return request<TemplateAgentStatus>(
    `/api/templates/import/${importId}/agent/${agentJobId}`,
  );
}

export async function cancelTemplateImportAgent(
  importId: string,
  agentJobId: string,
): Promise<TemplateAgentStatus> {
  return request<TemplateAgentStatus>(
    `/api/templates/import/${importId}/agent/${agentJobId}/cancel`,
    { method: "POST" },
  );
}

export async function fetchTemplateImportFiles(
  importId: string,
  path = "",
): Promise<TemplateImportFileList> {
  const query = path ? `?path=${encodeURIComponent(path)}` : "";
  return request<TemplateImportFileList>(`/api/templates/import/${importId}/files${query}`);
}

export async function previewTemplateImportDraft(
  importId: string,
  draft?: TemplateReviewDraft,
): Promise<TemplatePreview> {
  return request<TemplatePreview>(`/api/templates/import/${importId}/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(draft ?? {}),
  });
}

export async function confirmTemplateImport(importId: string): Promise<ImportStatus> {
  return request<ImportStatus>(`/api/templates/import/${importId}/confirm`, {
    method: "POST",
  });
}

export async function fetchTemplateImportPptistDeck(importId: string): Promise<PptistDeckPayload> {
  return request<PptistDeckPayload>(`/api/templates/import/${importId}/pptist/deck`);
}

export async function saveTemplateImportPptistDeck(
  importId: string,
  payload: PptistDeckPayload,
): Promise<PptistSaveResult> {
  return request<PptistSaveResult>(`/api/templates/import/${importId}/pptist/deck`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function addAnnotation(
  importId: string,
  payload: {
    slide_index: number;
    bbox_norm: { x: number; y: number; width: number; height: number };
    note: string;
    linked_element_id?: string | null;
  },
): Promise<{ annotation_id: string; annotations: UserAnnotation[] }> {
  return request<{ annotation_id: string; annotations: UserAnnotation[] }>(
    `/api/templates/import/${importId}/annotation`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export async function removeAnnotation(
  importId: string,
  annotationId: string,
): Promise<{ deleted: boolean; annotations: UserAnnotation[] }> {
  return request<{ deleted: boolean; annotations: UserAnnotation[] }>(
    `/api/templates/import/${importId}/annotation/${annotationId}`,
    { method: "DELETE" },
  );
}

export async function updateAnnotation(
  importId: string,
  annotationId: string,
  payload: Partial<Pick<UserAnnotation, "bbox_norm" | "note" | "linked_element_id" | "resolved">>,
): Promise<{ annotation: UserAnnotation; annotations: UserAnnotation[] }> {
  return request<{ annotation: UserAnnotation; annotations: UserAnnotation[] }>(
    `/api/templates/import/${importId}/annotation/${annotationId}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export async function fetchTemplatePreview(templateId: string): Promise<TemplatePreview> {
  return request<TemplatePreview>(`/api/templates/${templateId}/preview`);
}

export async function generateTemplateDesignSpec(
  templateId: string,
  modelConfig: GenerateRequestPayload["model_config"],
  feedback?: string,
): Promise<TemplatePreview> {
  return request<TemplatePreview>(`/api/templates/${templateId}/design-spec`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model_config: modelConfig, feedback: feedback ?? "" }),
  });
}

export async function fetchImportedTemplates(): Promise<UserTemplateItem[]> {
  return request<UserTemplateItem[]>("/api/templates/imported");
}

export async function deleteTemplate(templateId: string): Promise<void> {
  await request<void>(`/api/templates/${templateId}`, { method: "DELETE" });
}

export async function renameTemplate(templateId: string, label: string): Promise<UserTemplateItem> {
  return request<UserTemplateItem>(`/api/templates/${templateId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label }),
  });
}

export async function generatePresentation(
  payload: GenerateRequestPayload,
): Promise<GenerateResponse> {
  return request<GenerateResponse>("/api/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function fetchJobStatus(jobId: string, init?: RequestInit): Promise<JobStatus> {
  return request<JobStatus>(`/api/status/${jobId}`, init);
}

export async function cancelJob(jobId: string): Promise<CancelJobResponse> {
  return request<CancelJobResponse>(`/api/status/${jobId}/cancel`, {
    method: "POST",
  });
}

export async function deleteJob(jobId: string): Promise<void> {
  await request<void>(`/api/status/${jobId}`, { method: "DELETE" });
}

export async function reexportPresentation(jobId: string): Promise<ReexportResponse> {
  return request<ReexportResponse>(`/api/download/${jobId}/reexport`, {
    method: "POST",
  });
}

export async function fetchPreview(jobId: string): Promise<PreviewResponse> {
  return request<PreviewResponse>(`/api/preview/${jobId}`);
}

export async function fetchPreviewPptistDeck(jobId: string): Promise<PptistDeckPayload> {
  return request<PptistDeckPayload>(`/api/pptist/preview/${jobId}/deck`);
}

export async function savePreviewPptistDeck(
  jobId: string,
  payload: PptistDeckPayload,
): Promise<PptistSaveResult> {
  return request<PptistSaveResult>(`/api/pptist/preview/${jobId}/deck`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function fetchProjectPreview(
  projectDir: string,
  options: { lastSlideOnly?: boolean; signal?: AbortSignal } = {},
): Promise<PreviewResponse> {
  const params = new URLSearchParams({ project_dir: projectDir });
  if (options.lastSlideOnly) {
    params.set("last_slide_only", "1");
  }
  return request<PreviewResponse>(
    `/api/preview-project?${params.toString()}`,
    options.signal ? { signal: options.signal } : undefined,
  );
}

export async function updatePreviewSlide(jobId: string, slideIndex: number, content: string, document?: SlideDocument, notes?: string): Promise<PreviewSlide> {
  return request<PreviewSlide>(`/api/preview/${jobId}/slides/${slideIndex}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, document, notes }),
  });
}

export async function fetchPreviewSlideScene(jobId: string, slideIndex: number): Promise<SlideScene> {
  return request<SlideScene>(`/api/preview/${jobId}/slides/${slideIndex}/scene`);
}

export async function fetchPreviewDeckScene(jobId: string): Promise<DeckScene> {
  return request<DeckScene>(`/api/preview/${jobId}/deck/scene`);
}

export async function patchPreviewSlideScene(
  jobId: string,
  slideIndex: number,
  operations: SlideSceneOperation[],
  sceneVersion?: number | null,
): Promise<SlideScene> {
  return request<SlideScene>(`/api/preview/${jobId}/slides/${slideIndex}/scene`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ operations, scene_version: sceneVersion ?? undefined }),
  });
}

export async function patchPreviewDeckScene(
  jobId: string,
  slideIndex: number,
  operations: SlideSceneOperation[],
  sceneVersion?: number | null,
  mode: "commit" | "preview" = "commit",
): Promise<SlideScene> {
  return request<SlideScene>(`/api/preview/${jobId}/deck/operations`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ slide_index: slideIndex, operations, base_scene_version: sceneVersion ?? undefined, mode }),
  });
}

export async function checkPreviewDeck(jobId: string): Promise<DeckCheckResult> {
  return request<DeckCheckResult>(`/api/preview/${jobId}/deck/check`, { method: "POST" });
}

export async function uploadPreviewDeckImage(jobId: string, file: File): Promise<DeckImageAsset> {
  const formData = new FormData();
  formData.append("file", file);
  return request<DeckImageAsset>(`/api/preview/${jobId}/assets/images`, {
    method: "POST",
    body: formData,
  });
}

export async function stagePreviewDeckImageFromUrl(jobId: string, imageUrl: string, filename?: string): Promise<DeckImageAsset> {
  return request<DeckImageAsset>(`/api/preview/${jobId}/assets/images/from-url`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ image_url: imageUrl, filename }),
  });
}

export async function duplicatePreviewSlide(jobId: string, slideIndex: number): Promise<PreviewResponse> {
  return request<PreviewResponse>(`/api/preview/${jobId}/slides/${slideIndex}/duplicate`, {
    method: "POST",
  });
}

export async function reorderPreviewSlides(jobId: string, slideIndexes: number[]): Promise<PreviewResponse> {
  return request<PreviewResponse>(`/api/preview/${jobId}/slides/reorder`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ slide_indexes: slideIndexes }),
  });
}

export async function fetchTemplateSlideScene(importId: string, slideIndex: number): Promise<SlideScene> {
  return request<SlideScene>(`/api/templates/import/${importId}/slides/${slideIndex}/scene`);
}

export async function fetchTemplateDeckScene(importId: string): Promise<DeckScene> {
  return request<DeckScene>(`/api/templates/import/${importId}/deck/scene`);
}

export async function patchTemplateSlideScene(
  importId: string,
  slideIndex: number,
  operations: SlideSceneOperation[],
  sceneVersion?: number | null,
): Promise<SlideScene> {
  return request<SlideScene>(`/api/templates/import/${importId}/slides/${slideIndex}/scene`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ operations, scene_version: sceneVersion ?? undefined }),
  });
}

export async function patchTemplateDeckScene(
  importId: string,
  slideIndex: number,
  operations: SlideSceneOperation[],
  sceneVersion?: number | null,
  mode: "commit" | "preview" = "commit",
): Promise<SlideScene> {
  return request<SlideScene>(`/api/templates/import/${importId}/deck/operations`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ slide_index: slideIndex, operations, base_scene_version: sceneVersion ?? undefined, mode }),
  });
}

export async function checkTemplateDeck(importId: string): Promise<DeckCheckResult> {
  return request<DeckCheckResult>(`/api/templates/import/${importId}/deck/check`, { method: "POST" });
}

export async function uploadTemplateDeckImage(importId: string, file: File): Promise<DeckImageAsset> {
  const formData = new FormData();
  formData.append("file", file);
  return request<DeckImageAsset>(`/api/templates/import/${importId}/assets/images`, {
    method: "POST",
    body: formData,
  });
}

export async function stageTemplateDeckImageFromUrl(importId: string, imageUrl: string, filename?: string): Promise<DeckImageAsset> {
  return request<DeckImageAsset>(`/api/templates/import/${importId}/assets/images/from-url`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ image_url: imageUrl, filename }),
  });
}

export async function duplicateTemplateImportSlide(importId: string, slideIndex: number): Promise<TemplateReview> {
  return request<TemplateReview>(`/api/templates/import/${importId}/slides/${slideIndex}/duplicate`, {
    method: "POST",
  });
}

export async function deleteTemplateImportSlide(importId: string, slideIndex: number): Promise<TemplateReview> {
  return request<TemplateReview>(`/api/templates/import/${importId}/slides/${slideIndex}`, {
    method: "DELETE",
  });
}

export async function reorderTemplateImportSlides(importId: string, slideIndexes: number[]): Promise<TemplateReview> {
  return request<TemplateReview>(`/api/templates/import/${importId}/slides/reorder`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ slide_indexes: slideIndexes }),
  });
}

export async function updateTemplateAgentTemplateSvg(
  importId: string,
  pageType: TemplatePageType,
  svg: string,
): Promise<TemplatePreview> {
  return request<TemplatePreview>(
    `/api/templates/import/${importId}/agent-template/${pageType}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ svg }),
    },
  );
}

export async function createPreviewSlide(jobId: string, content?: string, document?: SlideDocument, notes?: string): Promise<PreviewSlide> {
  return request<PreviewSlide>(`/api/preview/${jobId}/slides`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, document, notes }),
  });
}

export async function deletePreviewSlide(jobId: string, slideIndex: number): Promise<PreviewResponse> {
  return request<PreviewResponse>(`/api/preview/${jobId}/slides/${slideIndex}`, {
    method: "DELETE",
  });
}

export async function refinePresentation(
  payload: RefineRequestPayload,
): Promise<RefineResponse> {
  return request<RefineResponse>("/api/refine", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function deleteSession(sessionId: string): Promise<void> {
  await request<void>(`/api/session/${sessionId}`, { method: "DELETE" });
}

export async function listVersions(jobId: string): Promise<VersionsResponse> {
  return request<VersionsResponse>(`/api/versions/${jobId}`);
}

export async function fetchVersion(jobId: string, roundName: string): Promise<VersionDetailResponse> {
  return request<VersionDetailResponse>(`/api/versions/${jobId}/${roundName}`);
}

export async function deleteVersion(jobId: string, roundName: string): Promise<void> {
  await request<void>(`/api/versions/${jobId}/${roundName}`, { method: "DELETE" });
}

export async function fetchCriticHistory(jobId: string): Promise<{ events: import("./types").CriticEvent[] }> {
  return request<{ events: import("./types").CriticEvent[] }>(`/api/critic/${jobId}`);
}

export async function updateSvgFonts(
  jobId: string,
  config: UpdateFontsRequest,
): Promise<UpdateFontsResponse> {
  return request<UpdateFontsResponse>(`/api/status/${jobId}/update-fonts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export async function searchImages(
  jobId: string,
  payload: ImageSearchRequest,
  init?: RequestInit,
): Promise<ImageSearchResponse> {
  return request<ImageSearchResponse>(`/api/image-search/${jobId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal: init?.signal,
  });
}

export async function applySearchImage(
  jobId: string,
  payload: ImageApplyRequest,
): Promise<ImageApplyResponse> {
  return request<ImageApplyResponse>(`/api/image-search/${jobId}/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function undoSearchImage(jobId: string): Promise<ImageUndoResponse> {
  return request<ImageUndoResponse>(`/api/image-search/${jobId}/undo`, {
    method: "POST",
  });
}

export async function fetchUsageSnapshot(): Promise<UsageSnapshotResponse> {
  const [summary, daily, byModel, byStage, records] = await Promise.all([
    request<UsageSummaryResponse>("/api/usage/summary"),
    request<{ rows: UsageDailyRowResponse[] }>("/api/usage/daily"),
    request<{ rows: UsageModelRowResponse[] }>("/api/usage/by-model"),
    request<{ rows: UsageStageRowResponse[] }>("/api/usage/by-stage"),
    request<{ rows: UsageRecordResponse[] }>("/api/usage/records?limit=50"),
  ]);
  return {
    summary,
    daily: daily.rows ?? [],
    by_model: byModel.rows ?? [],
    by_stage: byStage.rows ?? [],
    recent: records.rows ?? [],
  };
}

export function getDownloadUrl(jobId: string): string {
  return `${API_BASE}/api/download/${jobId}`;
}

export function getDownloadUrlForOutput(outputPath: string): string {
  const params = new URLSearchParams({ output_path: outputPath });
  return `${API_BASE}/api/download-file?${params.toString()}`;
}
