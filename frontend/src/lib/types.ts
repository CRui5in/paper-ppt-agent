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
  import_mode?: "builtin" | "direct" | "agent" | "llm" | "classic" | string;
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
  generation_backend?: "provider" | "agent";
  agent_config?: AgentGenerationConfig;
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
  template_id?: string;
  research_config?: ResearchConfig;
}

export interface AgentGenerationConfig {
  runtime: "claude_code" | "codex";
  model?: string;
  api_key?: string;
  auth_token?: string;
  base_url?: string;
  reasoning_effort?: "low" | "medium" | "high" | "xhigh";
  max_turns?: number;
  load_project_settings?: boolean;
  allow_external_research?: boolean;
  allow_deep_research?: boolean;
  enable_visual_qa?: boolean;
  reply_language?: "zh" | "en";
}

export interface ImportStartResponse {
  import_id: string;
  status: string;
  template_id?: string | null;
  collaboration_mode?: "classic" | "agent" | "direct";
}

export interface ImportStatus {
  import_id: string;
  status: "processing" | "review_required" | "complete" | "error";
  stage?: string;
  progress?: number;
  message?: string;
  steps?: Array<{
    id: string;
    label: string;
    status: string;
    message?: string;
    error?: string;
    started_at?: number;
    ended_at?: number;
    duration_ms?: number;
  }>;
  review_required?: boolean;
  template_id?: string | null;
  label?: string | null;
  slide_count?: number;
  export_mode?: string;
  theme_colors?: string[];
  error?: string | null;
  collaboration_mode?: "classic" | "agent" | "direct";
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

export interface PptistBootstrapSource {
  kind?: "preview" | "templateImport";
  id?: string;
  source_pptx_url?: string | null;
  source_pptx_path?: string | null;
  fallback_slides?: Array<Record<string, unknown>>;
  saved_deck?: boolean;
  deck_source?: unknown;
}

export interface PptistDeckPayload {
  title: string;
  width: number;
  height: number;
  theme?: Record<string, unknown> | null;
  slides: Array<Record<string, unknown>>;
  source?: PptistBootstrapSource | Record<string, unknown> | string | null;
  updated_at?: string | null;
}

export interface PptistSaveResult {
  status: string;
  output_path?: string | null;
  export_path?: string | null;
  slide_count?: number;
  updated_at?: string | null;
  warnings?: string[];
}

export interface UserTemplateItem {
  template_id: string;
  label: string;
  summary?: string;
  slide_count?: number;
}

/**
 * A user-drawn rectangular annotation on a slide preview. Coordinates are
 * normalized to ``[0, 1]`` against the slide canvas.
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
  preview_image_url?: string;
  render_url?: string;
  edit_base_url?: string;
  scene_url?: string;
  scene_version?: number;
  edit_capabilities?: Record<string, unknown>;
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
  pptist_version?: string | null;
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
    templateized?: boolean;
    templateized_at?: number;
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
  model_config?: {
    provider: string;
    model: string;
    api_key: string;
    base_url?: string;
    artifact_thinking_mode?: "disabled" | "default";
    deepseek_settings?: DeepSeekSettings;
    openai_settings?: OpenAISettings;
  };
  options: GenerationOptions;
}

export interface GenerationAgentFeedbackResponse {
  job_id: string;
  status: string;
  path?: string | null;
}

export interface GenerationAgentMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: number;
  kind?: "message" | "tool" | "status" | "usage";
  status?: string;
  tool?: string;
  toolUseId?: string;
  subagentId?: string;
  data?: Record<string, unknown>;
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
    | "artifact_updated"
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

export interface TemplateAgentClaudeCodeStatus {
  available: boolean;
  cli_path?: string | null;
  sdk_available: boolean;
  sdk_error?: string | null;
  message: string;
  default_model?: string | null;
  available_models?: string[];
  configured_models?: Record<string, string>;
  provider_config?: {
    base_url?: string | null;
    api_key?: string;
    auth_token?: string;
    oauth_token?: string;
    has_api_key?: boolean;
    has_auth_token?: boolean;
    has_oauth_token?: boolean;
  };
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
  provider?: string | null;
  model?: string | null;
  base_url?: string | null;
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
  svg_valid?: boolean;
  svg_error?: string | null;
  notes?: string;
  document?: SlideDocument | null;
  render_url?: string | null;
  edit_base_url?: string | null;
  scene_url?: string | null;
  scene_version?: number | null;
  edit_capabilities?: Record<string, unknown> | null;
}

export interface SlideSceneRun {
  text: string;
  fontSize?: number;
  bold?: boolean;
  italic?: boolean;
  fill?: string;
  fontFamily?: string;
}

export interface SlideSceneElement {
  id: string;
  shape_id?: string;
  name?: string;
  type: "text" | "image" | "rect" | "ellipse" | "line" | "shape" | "table" | "graphic" | "group" | "unknown";
  z?: number;
  bbox: { x: number; y: number; width: number; height: number; rotation?: number };
  rotation?: number;
  locked?: boolean;
  visible?: boolean;
  capabilities?: string[];
  text?: string;
  runs?: SlideSceneRun[];
  textBox?: { insetLeft?: number; insetRight?: number; insetTop?: number; insetBottom?: number };
  style?: { fill?: string; stroke?: string; strokeWidth?: number };
  geometry?: string;
  media_name?: string;
  image_url?: string;
  cells?: string[][];
  children?: SlideSceneElement[];
  [key: string]: unknown;
}

export interface SlideScene {
  version: number;
  slide_index: number;
  width: number;
  height: number;
  width_emu?: number;
  height_emu?: number;
  scene_version?: number;
  render_url?: string;
  edit_base_url?: string;
  elements: SlideSceneElement[];
}

export interface DeckScene {
  source: "preview" | "template" | string;
  id: string;
  scene_version?: number;
  slides: SlideScene[];
}

export interface EditorSelection {
  ids: string[];
  primaryId?: string;
  selectedType?: SlideSceneElement["type"];
  bounds?: { x: number; y: number; width: number; height: number };
}

export interface ScenePatchResult {
  scene: SlideScene;
  scene_version?: number;
  conflicts?: string[];
}

export interface DeckImageAsset {
  asset_id: string;
  media_name: string;
  mime_type: string;
  size: number;
}

export interface DeckCheckAction {
  label: string;
  action?: "select" | "replaceImage" | string;
  element_id?: string;
  operation?: SlideSceneOperation;
}

export interface DeckCheckIssue {
  id: string;
  rule: string;
  severity: "error" | "warning";
  slide_index: number;
  element_ids: string[];
  bbox?: { x: number; y: number; width: number; height: number } | null;
  detail: string;
  recommended_actions: DeckCheckAction[];
}

export interface DeckCheckResult {
  passed: boolean;
  error_count: number;
  warning_count: number;
  issues: DeckCheckIssue[];
}

export type SlideSceneOperation =
  | { type: "move" | "groupMove"; id: string; x: number; y: number }
  | { type: "resize"; id: string; x: number; y: number; width: number; height: number }
  | { type: "rotate"; id: string; rotation: number }
  | { type: "reorder"; id: string; direction: "front" | "back" | "forward" | "backward" }
  | { type: "updateText"; id: string; text: string }
  | { type: "updateStyle"; id: string; fill?: string; stroke?: string; strokeWidth?: number }
  | { type: "updateTextStyle"; id: string; fontSize?: number; fontFamily?: string; fill?: string; color?: string; bold?: boolean; italic?: boolean; align?: "left" | "center" | "right" | "justify" }
  | { type: "updateImage"; id: string; src?: string; media_name?: string; asset_id?: string }
  | { type: "updateTableCell"; id: string; row: number; col: number; text: string }
  | { type: "addText"; x: number; y: number; width: number; height: number; text: string; fontSize?: number; fontFamily?: string; color?: string }
  | { type: "addShape"; x: number; y: number; width: number; height: number; geometry?: string; fill?: string; stroke?: string; strokeWidth?: number }
  | { type: "addImage"; x: number; y: number; width: number; height: number; src?: string; media_name?: string; asset_id?: string }
  | { type: "addTable"; x: number; y: number; width: number; height: number; rows?: number; cols?: number; header?: string }
  | { type: "align"; ids: string[]; mode: "left" | "center" | "right" | "top" | "middle" | "bottom"; scope?: "selection" | "slide" }
  | { type: "distribute"; ids: string[]; axis: "horizontal" | "vertical" }
  | { type: "delete"; id: string }
  | { type: "duplicate"; id: string; x?: number; y?: number };

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

export interface JobEventsResponse {
  job_id: string;
  last_seq: number;
  events: JobEvent[];
}

export interface RefineRequestPayload {
  job_id: string;
  feedback: string;
  model_config: {
    provider: string;
    model: string;
    api_key: string;
    base_url?: string;
    artifact_thinking_mode?: "disabled" | "default";
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
