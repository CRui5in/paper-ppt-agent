import type {
  CancelJobResponse,
  GenerateRequestPayload,
  GenerateResponse,
  ImportStartResponse,
  ImportStatus,
  JobStatus,
  PreviewResponse,
  ProvidersResponse,
  ReexportResponse,
  RefineRequestPayload,
  RefineResponse,
  TemplateInfo,
  TemplatePreview,
  UpdateFontsRequest,
  UpdateFontsResponse,
  UploadResponse,
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
  const response = await fetch(`${API_BASE}${path}`, init);
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

export async function fetchTemplates(): Promise<TemplateInfo[]> {
  return request<TemplateInfo[]>("/api/templates");
}

export async function uploadTemplatePptx(file: File): Promise<ImportStartResponse> {
  const formData = new FormData();
  formData.append("file", file);
  return request<ImportStartResponse>("/api/templates/upload", {
    method: "POST",
    body: formData,
  });
}

export async function fetchImportStatus(importId: string): Promise<ImportStatus> {
  return request<ImportStatus>(`/api/templates/import/${importId}`);
}

export async function fetchTemplatePreview(templateId: string): Promise<TemplatePreview> {
  return request<TemplatePreview>(`/api/templates/${templateId}/preview`);
}

export async function fetchImportedTemplates(): Promise<UserTemplateItem[]> {
  return request<UserTemplateItem[]>("/api/templates/imported");
}

export async function deleteTemplate(templateId: string): Promise<void> {
  await request<void>(`/api/templates/${templateId}`, { method: "DELETE" });
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

export async function fetchProjectPreview(projectDir: string): Promise<PreviewResponse> {
  const params = new URLSearchParams({ project_dir: projectDir });
  return request<PreviewResponse>(`/api/preview-project?${params.toString()}`);
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
