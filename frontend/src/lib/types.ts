export type SourceType = "pdf" | "latex";

export interface FileInfo {
  name: string;
  size: number;
  source_type: SourceType;
}

export interface UploadResponse {
  session_id: string;
  file_info: FileInfo;
}

export interface ProviderModel {
  id: string;
  display_name: string;
  supports_vision: boolean;
}

export interface ProviderListItem {
  name: string;
  display_name: string;
  default_base_url?: string | null;
  models: ProviderModel[];
}

export interface ProvidersResponse {
  providers: ProviderListItem[];
}

export interface StyleOverridesPayload {
  palette?: string[];
  font?: string;
  font_heading?: string;
  font_body?: string;
  cjk_heading?: string;
  cjk_body?: string;
  density?: "compact" | "normal" | "spacious";
}

export interface TemplateInfo {
  template_id: string;
  label: string;
  summary: string;
  tone: string;
  theme_mode: string;
  category: string;
  keywords: string[];
}

export interface ResearchConfig {
  arxiv_search_enabled?: boolean;
  semantic_scholar_enabled?: boolean;
  web_search_enabled?: boolean;
  semantic_scholar_api_key?: string;
  web_search_provider?: "tavily" | "serpapi";
  tavily_api_key?: string;
  serpapi_key?: string;
  max_results_per_source?: number;
  relevance_filter?: boolean;
}

export interface ResearchFinding {
  source: string;
  title: string;
  abstract?: string;
  authors?: string[];
  year?: number | null;
  citation_count?: number | null;
  url?: string;
  relevance_note?: string;
}

export interface ResearchEnrichmentStats {
  phase?: "querying";
  arxiv?: { found: number; error?: string; findings?: ResearchFinding[] };
  semantic_scholar?: { found: number; error?: string; findings?: ResearchFinding[] };
  web?: { found: number; error?: string; provider?: string; findings?: ResearchFinding[] };
  total_findings?: number;
  filtered_findings?: number;
}

export interface GenerationOptions {
  canvas_format: string;
  style: string;
  num_pages?: number;
  language: string;
  detail_level: string;
  timeout_seconds?: number;
  max_critic_attempts?: number;
  style_overrides?: StyleOverridesPayload;
  enable_deep_research?: boolean;
  enable_visual_critic?: boolean;
  visual_qa_max_attempts?: number;
  enable_icon?: boolean;
  enable_icon_rag?: boolean;
  gemini_api_key?: string;
  template_id?: string;
  research_config?: ResearchConfig;
}

export interface ImportStartResponse {
  import_id: string;
  status: string;
  template_id?: string | null;
}

export interface ImportStatus {
  import_id: string;
  status: "processing" | "complete" | "error";
  template_id?: string | null;
  label?: string | null;
  slide_count?: number;
  export_mode?: string;
  theme_colors?: string[];
  error?: string | null;
}

export interface TemplatePreview {
  template_id: string;
  label: string;
  cover_svg?: string;
  content_svg?: string;
  theme_colors?: string[];
}

export interface UserTemplateItem {
  template_id: string;
  label: string;
  summary?: string;
  slide_count?: number;
}

export interface DeepSeekSettings {
  thinking_enabled: boolean;
  reasoning_effort: "high" | "max";
}

export interface OpenAISettings {
  reasoning_effort: "none" | "low" | "medium" | "high" | "xhigh";
  verbosity: "low" | "medium" | "high";
}

export interface GenerateRequestPayload {
  session_id: string;
  instruction: string;
  model_config: {
    provider: string;
    model: string;
    api_key: string;
    base_url?: string;
    deepseek_settings?: DeepSeekSettings;
    openai_settings?: OpenAISettings;
  };
  options: GenerationOptions;
}

export interface GenerateResponse {
  job_id: string;
  status: string;
}

export interface JobStatus {
  status: string;
  progress: number;
  message: string;
  slides_completed: number;
  total_slides: number;
  output_path?: string | null;
  error?: string | null;
}

export interface CancelJobResponse {
  job_id: string;
  status: string;
}

export interface ReexportResponse {
  job_id: string;
  status: string;
  output_path: string;
}

export interface PreviewSlide {
  index: number;
  name: string;
  source: string;
  content: string;
}

export interface PreviewResponse {
  job_id: string;
  project_dir?: string | null;
  slides: PreviewSlide[];
  output_path?: string | null;
  status: string;
}

export interface GenerationHistoryItem {
  jobId: string;
  fileName: string;
  sourceType?: SourceType;
  status: string;
  slideCount: number;
  createdAt?: string;
  updatedAt: string;
  projectDir?: string | null;
  outputPath?: string | null;
  provider?: string;
  model?: string;
  baseUrl?: string;
  options?: GenerationOptions;
  parentJobId?: string | null;
  // Last error message for this run, persisted so the result page can
  // surface it later (otherwise navigating into a failed history entry
  // would only ever show "Job not found." even though we know the real
  // failure reason from the original WebSocket / pipeline event).
  error?: string | null;
}

export interface JobEvent {
  type: "progress" | "slide_ready" | "complete" | "error";
  job_id: string;
  stage: string;
  status: string;
  message: string;
  progress: number;
  slides_completed: number;
  total_slides: number;
  data: Record<string, unknown>;
  // Server-assigned monotonic id within a job. Used by the WebSocket
  // client to dedupe replayed events and to ask for replay starting from
  // ``since_seq`` after a reconnect. Older servers may omit this field.
  seq?: number;
  ts?: number;
  // Snapshot frames carry the latest known seq so the client can ask for
  // replays from the right point even when no event has been delivered yet.
  last_seq?: number;
}

export interface CriticViolation {
  rule: string;
  severity: "error" | "warning";
  detail: string;
  element?: string | null;
  bbox?: number[] | null;
}

export interface CriticReport {
  passed: boolean;
  error_count: number;
  warning_count: number;
  canvas?: number[] | null;
  violations: CriticViolation[];
}

export interface CriticEvent {
  page: number;
  attempt: number;
  report: CriticReport;
  repair_prompt?: string;
  archive_path?: string;
}

/** Heartbeat ping emitted by the server every ~20s of silence. */
export interface JobPingEvent {
  type: "ping";
  ts: number;
}

export type JobSocketMessage = JobEvent | JobPingEvent;

export interface RefineRequestPayload {
  job_id: string;
  feedback: string;
  model_config: {
    provider: string;
    model: string;
    api_key: string;
    base_url?: string;
    deepseek_settings?: DeepSeekSettings;
    openai_settings?: OpenAISettings;
  };
  options: GenerationOptions;
  target_pages?: number[];
  allow_structure_changes?: boolean;
}

export interface RefineResponse {
  job_id: string;
  status: string;
}

export interface VersionItem {
  round: number;
  name: string;
  path: string;
  slide_count: number;
  created_at: number;
}

export interface VersionsResponse {
  job_id: string;
  project_dir?: string | null;
  current_slide_count: number;
  versions: VersionItem[];
}

export interface VersionSlide {
  index: number;
  name: string;
  content: string;
}

export interface VersionDetailResponse {
  job_id: string;
  round: number;
  name: string;
  path: string;
  slides: VersionSlide[];
}

// ── Font update ────────────────────────────────────────────────────────────

export interface UpdateFontsRequest {
  western_heading?: string | null;
  western_body?: string | null;
  cjk_heading?: string | null;
  cjk_body?: string | null;
}

export interface UpdateFontsResponse {
  svg_fonts_replaced: number;
  status: string;
}

// ── Image Search ─────────────────────────────────────────────────────────

export interface ImageSearchResultItem {
  url: string;
  thumbnail: string;
  description: string;
  source: string;
}

export interface ImageSearchRequest {
  query: string;
  slide_index?: number;
  max_results?: number;
}

export interface ImageSearchResponse {
  results: ImageSearchResultItem[];
}

export interface ImageApplyRequest {
  image_url: string;
  slide_index: number;
  target_element?: string;
}

export interface ImageApplyResponse {
  status: string;
  local_path?: string;
  svg_updated: boolean;
}
