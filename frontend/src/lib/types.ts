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
  source?: "builtin" | "user";
  editable?: boolean;
  slide_count?: number;
  has_cover?: boolean;
  has_chapter?: boolean;
  has_content?: boolean;
  has_ending?: boolean;
  has_toc?: boolean;
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
  generation_mode?: "sequential" | "chapter_parallel" | "page_parallel";
  parallel_concurrency?: number;
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
  collaboration_mode?: "classic" | "agent";
}

export interface ImportStatus {
  import_id: string;
  status: "processing" | "review_required" | "complete" | "error";
  stage?: string;
  progress?: number;
  message?: string;
  steps?: Array<{ id: string; label: string; status: string }>;
  review_required?: boolean;
  template_id?: string | null;
  label?: string | null;
  slide_count?: number;
  export_mode?: string;
  theme_colors?: string[];
  error?: string | null;
  collaboration_mode?: "classic" | "agent";
}

export interface TemplatePreview {
  template_id: string;
  label: string;
  cover_svg?: string;
  toc_svg?: string;
  chapter_svg?: string;
  content_svg?: string;
  ending_svg?: string;
  design_spec?: string;
  theme_colors?: string[];
}

export interface UserTemplateItem {
  template_id: string;
  label: string;
  summary?: string;
  slide_count?: number;
}

/**
 * A user-drawn rectangular annotation on a slide preview. Coordinates are
 * normalized to ``[0, 1]`` against the slide canvas; see the
 * `SlideStage` coordinate-contract comment for the full spec.
 */
export interface UserAnnotation {
  annotation_id: string;
  slide_index: number;
  bbox_norm: { x: number; y: number; width: number; height: number };
  note: string;
  linked_element_id?: string | null;
  created_at: number;
  resolved?: boolean;
}

export type TemplateAssetRole = "logo" | "background" | "decoration" | "content_image" | "ignore";
export type TemplatePageType = "cover" | "toc" | "chapter" | "content" | "ending";

export interface TemplateImportSlide {
  index: number;
  page_type: TemplatePageType;
  text_samples?: string[];
  preview_svg?: string;
  preview_svg_url?: string;
}

export interface TemplateImportAsset {
  asset_id: string;
  file_name: string;
  image_size?: { width: number; height: number };
  preview_data_uri?: string;
  preview_url?: string;
  usage_count: number;
  pages: number[];
  position_stable: boolean;
  recommended_role: TemplateAssetRole;
  recommendation_source?: "rule" | "llm";
  llm_reason?: string;
  llm_confidence?: number;
  role: TemplateAssetRole;
  name: string;
  occurrences: Array<{
    slide_index: number;
    x: number;
    y: number;
    width: number;
    height: number;
  }>;
}

export interface TemplateReviewDraft {
  label?: string;
  page_selections?: Partial<Record<TemplatePageType, number | null>>;
  assets?: Record<string, { role?: TemplateAssetRole; name?: string | null }>;
  preserve_texts?: string[];
  placeholder_hints?: Partial<Record<TemplatePageType, Record<string, string>>>;
  element_actions?: Array<{
    page_type: TemplatePageType;
    element_id: string;
    action: "keep" | "remove" | "replace_with_placeholder";
    placeholder?: string;
    reason?: string;
  }>;
  design_spec?: string;
  annotations?: UserAnnotation[];
}

export interface TemplateReview {
  import_id: string;
  template_id: string;
  label: string;
  status: string;
  export_mode?: string;
  slide_count?: number;
  page_types: TemplatePageType[];
  asset_roles: TemplateAssetRole[];
  page_type_candidates: Partial<Record<TemplatePageType, number[]>>;
  slides: TemplateImportSlide[];
  assets: TemplateImportAsset[];
  draft: {
    label?: string;
    page_selections?: Partial<Record<TemplatePageType, number | null>>;
    assets?: Record<string, { role: TemplateAssetRole; name: string }>;
    preserve_texts?: string[];
    placeholder_hints?: Partial<Record<TemplatePageType, Record<string, string>>>;
    element_actions?: TemplateReviewDraft["element_actions"];
    design_spec?: string;
  };
  theme_colors?: string[];
  text_candidates?: Array<{ text: string; pages: number[]; page_count: number }>;
  feedback_history?: Array<{ feedback: string; created_at?: number }>;
  annotations?: UserAnnotation[];
  conversation?: Array<{
    role: "user" | "assistant" | string;
    content: string;
    created_at?: number;
    meta?: Record<string, unknown>;
  }>;
  llm_trace?: {
    iteration?: number;
    updated_at?: number;
    user_feedback?: string;
    changed?: boolean;
    retried_no_change?: boolean;
    rule_patches?: string[];
    input?: unknown;
    action_plan?: unknown;
  };
  llm?: {
    enabled?: boolean;
    status?: "not_run" | "missing_config" | "complete" | "error" | string;
    provider?: string;
    model?: string;
    agent?: boolean;
    error?: string;
    notes?: string[];
    changed?: boolean;
    retried_no_change?: boolean;
    rule_patches?: string[];
  };
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

export type TemplateImportModelConfig = GenerateRequestPayload["model_config"];

export type TemplateAgentConfigMode = "claude_code" | "custom";

export interface TemplateAgentConfig {
  mode: TemplateAgentConfigMode;
  api_key?: string;
  auth_token?: string;
  base_url?: string;
  model?: string;
  custom_model_option?: string;
  load_project_settings?: boolean;
  max_turns?: number;
  reply_language?: "zh" | "en";
}

export interface TemplateAgentStartResponse {
  agent_job_id: string;
  import_id: string;
  status: string;
}

export interface TemplateAgentStatus {
  agent_job_id: string;
  import_id: string;
  status: "queued" | "running" | "complete" | "error" | "cancelled" | string;
  message?: string;
  error?: string | null;
  created_at?: number;
  updated_at?: number;
  started_at?: number | null;
  completed_at?: number | null;
}

export interface TemplateAgentEvent {
  type:
    | "snapshot"
    | "status"
    | "message"
    | "tool"
    | "stderr"
    | "system"
    | "result"
    | "usage"
    | "llm_step"
    | "complete"
    | "cancelled"
    | "error"
    | "ping";
  agent_job_id?: string;
  import_id?: string;
  stage?: string;
  status?: string;
  message?: string;
  error?: string | null;
  data?: Record<string, unknown> | unknown;
  seq?: number;
  ts?: number;
  last_seq?: number;
}

export interface TemplateImportFileItem {
  name: string;
  path: string;
  type: "file" | "directory";
  size?: number | null;
  image?: boolean;
  preview_url?: string | null;
}

export interface TemplateImportFileList {
  cwd: string;
  parent?: string | null;
  items: TemplateImportFileItem[];
}

/** Aggregated cost / usage snapshot derived from agent ``usage`` events. */
export interface TemplateAgentUsage {
  model?: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  total_cost_usd: number;
  num_turns: number;
  duration_ms: number;
  model_usage?: Record<string, Record<string, number | string>> | null;
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
  fallback_slides?: number[];
  warnings?: string[];
}

export type SlideDocumentElementType = "text" | "rect" | "image" | "path" | "table";

export interface SlideDocumentElement {
  id: string;
  type: SlideDocumentElementType;
  x: number;
  y: number;
  width: number;
  height: number;
  rotation?: number;
  sourceTag?: string;
  sourceIndex?: number;
  committed?: boolean;
  [key: string]: unknown;
}

export interface SlideDocument {
  version: number;
  width: number;
  height: number;
  backgroundSvg: string;
  speakerNotes?: string;
  elements: SlideDocumentElement[];
}

export interface PreviewSlide {
  index: number;
  name: string;
  source: string;
  content: string;
  notes?: string;
  document?: SlideDocument | null;
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
  source?: "static" | "visual";
  rendered?: boolean;
  media_type?: string | null;
  rendered_image_path?: string | null;
  skipped_reason?: string | null;
  raw_response_excerpt?: string | null;
  repair_prompt?: string;
  archive_path?: string;
  before_archive_path?: string;
  after_archive_path?: string;
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
  tavily_api_key?: string;
  serpapi_key?: string;
}

export interface ImageSearchResponse {
  results: ImageSearchResultItem[];
}

export interface ImageApplyRequest {
  image_url: string;
  slide_index: number;
  target_element?: string;
  image_description?: string;
  api_key?: string;
  provider?: string;
  model?: string;
  base_url?: string;
}

export interface ImageApplyResponse {
  status: string;
  local_path?: string;
  svg_updated: boolean;
  action: string;
}

export interface ImageUndoResponse {
  status: string;
  svg_restored: boolean;
}
