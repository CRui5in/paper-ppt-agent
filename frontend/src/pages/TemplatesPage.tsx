import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import {
  Bookmark,
  Bot,
  FileCheck2,
  Inbox,
  Layers,
  Library,
  Loader2,
  MessageSquarePlus,
  Pencil,
  Sparkles,
  Trash2,
  UploadCloud,
  Wand2,
  X,
} from "lucide-react";

import { Layout } from "../components/layout/Layout";
import { useLocale } from "../i18n";
import { useGeneration } from "../hooks/useGeneration";
import { useTemplateImport } from "../hooks/useTemplateImport";
import { translateTemplateImportMessage } from "../lib/i18nStatus";
import {
  deleteTemplate,
  fetchTemplateAgentClaudeCodeStatus,
  fetchTemplatePreview,
  fetchTemplates,
  generateTemplateDesignSpec,
  renameTemplate,
} from "../lib/api";
import type {
  DeepSeekSettings,
  OpenAISettings,
  TemplateAgentConfig,
  TemplateImportSlide,
  TemplateImportModelConfig,
  TemplateInfo,
  TemplatePageType,
  TemplatePreview,
  UserAnnotation,
} from "../lib/types";
import { ProgressView } from "../components/template/ProgressView";
import { AgentImportingView } from "../components/template/AgentImportingView";
import { CollabPanel } from "../components/template/CollabPanel";
import { buildAgentActivityEvents } from "../components/template/agentActivity";
import {
  BigPreview,
  MiddleEmptyState,
  TemplateImportingState,
} from "../components/template/MiddlePagePreview";
import { detectUserLanguage } from "../components/template/detectUserLanguage";
import { HoverTooltip } from "../components/common/HoverTooltip";
import { PptistStudioHost } from "../components/pptist/PptistStudioHost";

const ROUTING_PROFILE_STORAGE_KEY = "paper-ppt-agent-routing-profiles-v1";
const TEMPLATE_AGENT_CONFIG_STORAGE_KEY = "paper-ppt-agent-template-agent-config-v1";
const ACTIVE_TEMPLATE_IMPORT_STORAGE_KEY = "paper-ppt-agent-active-template-import-v1";
const TEMPLATE_UPLOAD_MODE_STORAGE_KEY = "paper-ppt-agent-template-upload-mode-v1";

type PageSelectionChange = {
  pageType: TemplatePageType;
  from: number | null;
  to: number;
};

function uniqueStrings(values: string[]): string[] {
  const out: string[] = [];
  for (const value of values) {
    const item = value.trim();
    if (item && !out.includes(item)) out.push(item);
  }
  return out;
}

interface RoutingProfile {
  model: string;
  baseUrl: string;
  apiKey: string;
  artifactThinkingMode?: "disabled" | "default";
  deepseekSettings?: DeepSeekSettings;
  openaiSettings?: OpenAISettings;
}

type RoutingProfileMap = Record<string, RoutingProfile>;

const PAGE_TYPES: TemplatePageType[] = ["cover", "toc", "chapter", "content", "ending"];
const DIRECT_PLACEHOLDER_RULES: Record<TemplatePageType, string[]> = {
  cover: ["{{TITLE}} / {{PAGE_TITLE}}", "{{SUBTITLE}}", "{{AUTHOR}} / {{DATE}}", "{{LOGO_HEADER}} / {{LOGO_FOOTER}}"],
  toc: ["{{PAGE_TITLE}}", "{{TOC_LIST}} / {{TOC_ITEM_1}} - {{TOC_ITEM_5}}", "{{LOGO_HEADER}} / {{LOGO_FOOTER}}"],
  chapter: ["{{CHAPTER_NUM}} / {{CHAPTER_NUMBER}}", "{{CHAPTER_TITLE}}", "{{LOGO_HEADER}} / {{LOGO_FOOTER}}"],
  content: ["{{PAGE_TITLE}}", "{{CONTENT_AREA}}", "{{LOGO_HEADER}} / {{LOGO_FOOTER}}"],
  ending: ["{{ENDING_TITLE}}", "{{ENDING_MESSAGE}}", "{{LOGO_HEADER}} / {{LOGO_FOOTER}}"],
};

type LibraryFilter = "all" | "builtin" | "user";
type CollabMode = "agent" | "direct";
type UploadGuideDismissals = Partial<Record<CollabMode, boolean>>;
let uploadGuideDismissalsThisSession: UploadGuideDismissals = {};

function readModelConfig(): TemplateImportModelConfig | undefined {
  try {
    const raw = window.localStorage.getItem(ROUTING_PROFILE_STORAGE_KEY);
    if (!raw) return undefined;
    const map = JSON.parse(raw) as RoutingProfileMap;
    if (!map || typeof map !== "object") return undefined;
    for (const [providerName, profile] of Object.entries(map)) {
      if (profile?.apiKey && profile?.model) {
        return {
          provider: providerName,
          model: profile.model,
          api_key: profile.apiKey,
          base_url: profile.baseUrl || undefined,
          artifact_thinking_mode: profile.artifactThinkingMode ?? "disabled",
          deepseek_settings: providerName === "deepseek" ? profile.deepseekSettings : undefined,
          openai_settings: providerName === "openai" ? profile.openaiSettings : undefined,
        };
      }
    }
  } catch {
    /* noop */
  }
  return undefined;
}

function readTemplateAgentConfig(): TemplateAgentConfig {
  try {
    const raw = window.localStorage.getItem(TEMPLATE_AGENT_CONFIG_STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as TemplateAgentConfig;
      if (parsed) {
        return {
          mode: parsed.base_url ? "custom" : "claude_code",
          api_key: typeof parsed.api_key === "string" ? parsed.api_key : undefined,
          auth_token: typeof parsed.auth_token === "string" ? parsed.auth_token : undefined,
          base_url: typeof parsed.base_url === "string" ? parsed.base_url : undefined,
          model: typeof parsed.model === "string" ? parsed.model : undefined,
          load_project_settings: parsed.load_project_settings ?? true,
          max_turns:
            typeof parsed.max_turns === "number" && parsed.max_turns > 0 && parsed.max_turns !== 16
              ? parsed.max_turns
              : undefined,
        };
      }
    }
  } catch {
    /* noop */
  }
  return {
    mode: "claude_code",
    load_project_settings: true,
  };
}

function readActiveTemplateImportId(): string | undefined {
  try {
    const value = window.localStorage.getItem(ACTIVE_TEMPLATE_IMPORT_STORAGE_KEY);
    return value?.trim() || undefined;
  } catch {
    return undefined;
  }
}

function readTemplateUploadMode(): CollabMode {
  try {
    const value = window.localStorage.getItem(TEMPLATE_UPLOAD_MODE_STORAGE_KEY);
    return value === "agent" || value === "direct" ? value : "direct";
  } catch {
    return "direct";
  }
}

function writeTemplateUploadMode(mode: CollabMode): void {
  try {
    window.localStorage.setItem(TEMPLATE_UPLOAD_MODE_STORAGE_KEY, mode);
  } catch {
    /* noop */
  }
}

function readTemplateUploadGuideDismissals(): UploadGuideDismissals {
  return uploadGuideDismissalsThisSession;
}

function writeTemplateUploadGuideDismissal(mode: CollabMode, dismissed: boolean): UploadGuideDismissals {
  uploadGuideDismissalsThisSession = { ...uploadGuideDismissalsThisSession, [mode]: dismissed };
  return uploadGuideDismissalsThisSession;
}

function writeActiveTemplateImportId(importId: string | undefined): void {
  try {
    if (importId) {
      window.localStorage.setItem(ACTIVE_TEMPLATE_IMPORT_STORAGE_KEY, importId);
    } else {
      window.localStorage.removeItem(ACTIVE_TEMPLATE_IMPORT_STORAGE_KEY);
    }
  } catch {
    /* noop */
  }
}

function sanitizeSvg(svg: string): string {
  return (svg ?? "")
    .replace(/<\s*(script|foreignObject|iframe|object|embed|link|meta|base)\b[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi, "")
    .replace(/<\s*(script|foreignObject|iframe|object|embed|link|meta|base)\b[^>]*\/\s*>/gi, "")
    .replace(/\son[a-z0-9:_-]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, "")
    .replace(/\s+(href|xlink:href)\s*=\s*(?:"\s*javascript:[^"]*"|'\s*javascript:[^']*'|javascript:[^\s>]+)/gi, ' href="#"');
}

function pickPreviewSvg(preview: TemplatePreview, pt: TemplatePageType): string | undefined {
  switch (pt) {
    case "cover":
      return preview.cover_svg;
    case "toc":
      return preview.toc_svg;
    case "chapter":
      return preview.chapter_svg;
    case "content":
      return preview.content_svg;
    case "ending":
      return preview.ending_svg;
  }
}

function directImportReviewSignature(
  draft: {
    page_selections?: Partial<Record<TemplatePageType, number | null>>;
    assets?: unknown;
    preserve_texts?: unknown;
    placeholder_hints?: unknown;
    element_actions?: unknown;
    annotations?: unknown;
  },
  pptistVersion?: string | null,
): string {
  return JSON.stringify({
    pptist_version: pptistVersion ?? "",
    page_selections: draft.page_selections ?? {},
    assets: draft.assets ?? {},
    preserve_texts: draft.preserve_texts ?? [],
    placeholder_hints: draft.placeholder_hints ?? {},
    element_actions: draft.element_actions ?? [],
    annotations: draft.annotations ?? [],
  });
}

export function TemplatesPage() {
  const { t, locale } = useLocale();
  const navigate = useNavigate();
  const reportGlobalError = useGeneration((state) => state.reportError);

  // ── Library state ─────────────────────────────────────────────────────
  const [templates, setTemplates] = useState<TemplateInfo[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(false);
  const [libraryError, setLibraryError] = useState<string | null>(null);
  const [filter, setFilter] = useState<LibraryFilter>("all");
  const [selectedTemplateId, setSelectedTemplateId] = useState<string | null>(null);
  const [preview, setPreview] = useState<TemplatePreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [focusedPageType, setFocusedPageType] = useState<TemplatePageType>("cover");
  const [workspaceSideTab, setWorkspaceSideTab] = useState<"sources" | "config">("sources");

  // ── Import state ──────────────────────────────────────────────────────
  const [modelConfig] = useState<TemplateImportModelConfig | undefined>(readModelConfig);
  const [importId, setImportId] = useState<string | undefined>(readActiveTemplateImportId);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [checkingAgentRuntime, setCheckingAgentRuntime] = useState(false);
  const [confirmingFlag, setConfirmingFlag] = useState(false);
  const [autoSelectedFor, setAutoSelectedFor] = useState<string | null>(null);
  const [selectedSlideIndex, setSelectedSlideIndex] = useState<number | null>(null);
  const [pendingAgentSelectionChanges, setPendingAgentSelectionChanges] = useState<PageSelectionChange[]>([]);
  const selectionBaselineRef = useRef<Partial<Record<TemplatePageType, number | null>>>({});
  const autoInspectionRef = useRef<string | null>(null);
  const [annotationMode, setAnnotationMode] = useState(false);
  const [annotationDraft, setAnnotationDraft] = useState<{
    startX: number;
    startY: number;
    x: number;
    y: number;
    width: number;
    height: number;
  } | null>(null);
  const [uploadMode, setUploadMode] = useState<CollabMode>(readTemplateUploadMode);
  const [collabMode, setCollabMode] = useState<CollabMode>(readTemplateUploadMode);
  const [directConversation, setDirectConversation] = useState<Array<{ role: string; content: string; created_at?: number; meta?: Record<string, unknown> }>>([]);
  const [directDesignSpec, setDirectDesignSpec] = useState<string>("");
  const [showDirectDesignSpecConfirm, setShowDirectDesignSpecConfirm] = useState(false);
  const directDesignSpecSignatureRef = useRef<string | null>(null);
  const [agentConfig, setAgentConfig] = useState<TemplateAgentConfig>(readTemplateAgentConfig);
  const [agentModelOptions, setAgentModelOptions] = useState<string[]>([]);

  const handleMissingImport = useCallback(
    (missingImportId: string) => {
      setImportId((current) => (current === missingImportId ? undefined : current));
      writeActiveTemplateImportId(undefined);
      setUploadError(t("template.importMissing"));
    },
    [t],
  );

  const {
    status,
    review,
    draft,
    preview: importPreview,
    loading: importLoading,
    error: importError,
    upload,
    updateDraft,
    assist,
    runAgent,
    cancelAgent,
    llmEvents,
    agentEvents,
    agentStatus,
    agentCancelPending,
    confirm,
    generateDirectDesignSpec,
    refreshReview,
    retryStep,
  } = useTemplateImport(importId, { modelConfig, onMissingImport: handleMissingImport, t });

  const currentDirectImportSignature = useMemo(
    () => directImportReviewSignature(draft, review?.pptist_version),
    [draft, review?.pptist_version],
  );

  const modelConfigured = Boolean(modelConfig?.api_key && modelConfig?.model);
  const agentConfigured = true;

  const setTemplateError = useCallback(
    (message: string | null) => {
      setUploadError(message);
      reportGlobalError(message ?? "");
    },
    [reportGlobalError],
  );

  useEffect(() => {
    writeActiveTemplateImportId(importId);
    setDirectDesignSpec("");
    setShowDirectDesignSpecConfirm(false);
    directDesignSpecSignatureRef.current = null;
  }, [importId]);

  useEffect(() => {
    if (collabMode !== "direct" || !directDesignSpec) return;
    if (directDesignSpecSignatureRef.current === currentDirectImportSignature) return;
    directDesignSpecSignatureRef.current = null;
    setDirectDesignSpec("");
    setDirectConversation([]);
  }, [collabMode, currentDirectImportSignature, directDesignSpec]);

  useEffect(() => {
    if (importError) reportGlobalError(importError);
  }, [importError, reportGlobalError]);

  useEffect(() => {
    if (libraryError) reportGlobalError(libraryError);
  }, [libraryError, reportGlobalError]);

  useEffect(() => {
    writeTemplateUploadMode(uploadMode);
    if (!importId) {
      setCollabMode(uploadMode);
    }
  }, [importId, uploadMode]);

  useEffect(() => {
    if (status?.collaboration_mode === "agent" || status?.collaboration_mode === "direct") {
      setCollabMode(status.collaboration_mode);
    } else if (status?.collaboration_mode === "classic") {
      setCollabMode("direct");
    }
  }, [status?.collaboration_mode]);

  useEffect(() => {
    try {
      window.localStorage.setItem(
        TEMPLATE_AGENT_CONFIG_STORAGE_KEY,
        JSON.stringify(agentConfig),
      );
    } catch {
      /* noop */
    }
  }, [agentConfig]);

  useEffect(() => {
    let cancelled = false;
    void fetchTemplateAgentClaudeCodeStatus()
      .then((runtime) => {
        if (cancelled) return;
        setAgentModelOptions(uniqueStrings([
          ...(runtime.available_models ?? []),
          ...Object.values(runtime.configured_models ?? {}),
        ]));
        const provider = runtime.provider_config;
        setAgentConfig((current) => {
          const next: TemplateAgentConfig = { ...current };
          if (provider?.base_url) {
            next.base_url = provider.base_url;
            next.mode = "custom";
            next.model = runtime.default_model || undefined;
            next.api_key = provider.api_key || undefined;
            next.auth_token = provider.api_key
              ? undefined
              : provider.auth_token || provider.oauth_token || undefined;
          } else {
            next.mode = "claude_code";
            next.base_url = undefined;
            next.api_key = undefined;
            next.auth_token = undefined;
          }
          if (runtime.default_model && !next.model?.trim()) {
            next.model = runtime.default_model;
          }
          return next;
        });
      })
      .catch(() => {
        if (!cancelled) setAgentModelOptions([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // ── Load library ──────────────────────────────────────────────────────
  const loadTemplates = useCallback(async () => {
    setTemplatesLoading(true);
    setLibraryError(null);
    try {
      setTemplates(await fetchTemplates());
    } catch (err) {
      setLibraryError(err instanceof Error ? err.message : t("templates.error.loadFailed"));
    } finally {
      setTemplatesLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void loadTemplates();
  }, [loadTemplates]);

  // ── Load preview when selecting a template ───────────────────────────
  useEffect(() => {
    if (!selectedTemplateId) {
      setPreview(null);
      return;
    }
    let cancelled = false;
    setPreviewLoading(true);
    setFocusedPageType("cover");
    fetchTemplatePreview(selectedTemplateId)
      .then((p) => {
        if (!cancelled) setPreview(p);
      })
      .catch((err) => {
        if (!cancelled) {
          setLibraryError(err instanceof Error ? err.message : t("templates.error.previewFailed"));
        }
      })
      .finally(() => {
        if (!cancelled) setPreviewLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedTemplateId, t]);

  // ── Auto-select after import completes ───────────────────────────────
  useEffect(() => {
    if (!status) return;
    if (status.status !== "complete") return;
    const tid = status.template_id;
    if (!tid || autoSelectedFor === tid) return;
    setAutoSelectedFor(tid);
    setImportId(undefined);
    writeActiveTemplateImportId(undefined);
    void loadTemplates();
    setSelectedTemplateId(tid);
  }, [status, autoSelectedFor, loadTemplates]);

  // ── Initialise selected slide when review arrives ─────────────────────
  useEffect(() => {
    if (review && selectedSlideIndex == null) {
      setSelectedSlideIndex(review.slides[0]?.index ?? 1);
    }
    if (!review) {
      setSelectedSlideIndex(null);
      selectionBaselineRef.current = {};
    }
  }, [review, selectedSlideIndex]);

  useEffect(() => {
    if (!review || pendingAgentSelectionChanges.length > 0) return;
    selectionBaselineRef.current = { ...(draft.page_selections ?? {}) };
  }, [draft.page_selections, pendingAgentSelectionChanges.length, review]);

  // ── Filtering ─────────────────────────────────────────────────────────
  const filteredTemplates = useMemo(() => {
    let list = [...templates];
    if (filter === "builtin") {
      list = list.filter((tmpl) => tmpl.source !== "user");
    } else if (filter === "user") {
      list = list.filter((tmpl) => tmpl.source === "user");
    }
    return list.sort((a, b) => {
      const sourceOrder = (b.source === "user" ? 1 : 0) - (a.source === "user" ? 1 : 0);
      if (sourceOrder) return sourceOrder;
      return (a.label || a.template_id).localeCompare(b.label || b.template_id);
    });
  }, [templates, filter]);

  const selectedTemplate = useMemo(
    () => templates.find((tmpl) => tmpl.template_id === selectedTemplateId) ?? null,
    [templates, selectedTemplateId],
  );

  // ── Handlers ──────────────────────────────────────────────────────────
  const handleUpload = useCallback(
    async (file: File) => {
      setTemplateError(null);
      try {
        if (uploadMode === "agent") {
          setCheckingAgentRuntime(true);
          const runtime = await fetchTemplateAgentClaudeCodeStatus();
          if (!runtime.available) {
            const detail = runtime.message ? ` ${runtime.message}` : "";
            setTemplateError(`${t("template.agentClaudeCodeMissing")}${detail}`);
            return;
          }
        }
        const id = await upload(
          file,
          uploadMode,
          undefined,
        );
        writeActiveTemplateImportId(id);
        setImportId(id);
        setCollabMode(uploadMode);
        setSelectedTemplateId(null);
        setSelectedSlideIndex(null);
        setDirectDesignSpec("");
        setDirectConversation([]);
        autoInspectionRef.current = null;
        setAnnotationMode(false);
        setAnnotationDraft(null);
      } catch (err) {
        setTemplateError(err instanceof Error ? err.message : t("template.uploadFailed"));
      } finally {
        setCheckingAgentRuntime(false);
      }
    },
    [setTemplateError, upload, uploadMode, t],
  );

  const handleGenerateDirectDesignSpec = useCallback(async () => {
    setShowDirectDesignSpecConfirm(false);
    setConfirmingFlag(true);
    try {
      const next = await generateDirectDesignSpec();
      const spec = next?.draft?.design_spec?.trim() || "";
      const nextDraft = next?.draft ?? draft;
      directDesignSpecSignatureRef.current = directImportReviewSignature(nextDraft, next?.pptist_version ?? review?.pptist_version);
      setDirectDesignSpec(spec);
      if (spec) {
        setDirectConversation([
          {
            role: "assistant",
            content: spec,
            created_at: Date.now() / 1000,
            meta: { mode: "direct", design_spec_preview: true },
          },
        ]);
      }
    } finally {
      setConfirmingFlag(false);
    }
  }, [draft, generateDirectDesignSpec, review]);

  const handleConfirm = useCallback(async () => {
    if (collabMode === "direct") {
      if (!modelConfig) {
        setTemplateError(t("template.error.modelConfigRequired"));
        return;
      }
      const currentSlideCount = review?.slide_count ?? review?.slides?.length ?? 0;
      if (currentSlideCount !== 5) {
        setTemplateError(t("template.directFiveSlidesRequired"));
        return;
      }
      setShowDirectDesignSpecConfirm(true);
      return;
    }
    setConfirmingFlag(true);
    try {
      await confirm();
    } finally {
      setConfirmingFlag(false);
    }
  }, [collabMode, confirm, modelConfig, review, setTemplateError, t]);

  const handleConfirmDirectImport = useCallback(async () => {
    if (
      collabMode === "direct" &&
      directDesignSpec &&
      directDesignSpecSignatureRef.current !== currentDirectImportSignature
    ) {
      directDesignSpecSignatureRef.current = null;
      setDirectDesignSpec("");
      setDirectConversation([]);
      setTemplateError(t("template.directReviewStale"));
      return;
    }
    setConfirmingFlag(true);
    try {
      await confirm();
    } finally {
      setConfirmingFlag(false);
    }
  }, [collabMode, confirm, currentDirectImportSignature, directDesignSpec, setTemplateError, t]);

  const handleEditDirectImport = useCallback(() => {
    setDirectDesignSpec("");
    setDirectConversation([]);
  }, []);

  const handleCancelImport = useCallback(() => {
    writeActiveTemplateImportId(undefined);
    setImportId(undefined);
    setCollabMode(uploadMode);
    setAutoSelectedFor(null);
    setSelectedSlideIndex(null);
    setPendingAgentSelectionChanges([]);
    setDirectDesignSpec("");
    setDirectConversation([]);
    setShowDirectDesignSpecConfirm(false);
    autoInspectionRef.current = null;
    setAnnotationMode(false);
    setAnnotationDraft(null);
  }, [uploadMode]);

  const handleAssignPageTypeToSlide = useCallback(
    (pageType: TemplatePageType, slideIndex: number) => {
      const reviewing = Boolean(importId) && status?.status === "review_required" && Boolean(review);
      if (!reviewing) {
        setFocusedPageType(pageType);
        return;
      }
      const currentSelections = draft.page_selections ?? {};
      const previous = currentSelections[pageType] ?? null;
      setFocusedPageType(pageType);
      if (previous === slideIndex) {
        return;
      }
      updateDraft({
        page_selections: {
          ...currentSelections,
          [pageType]: slideIndex,
        },
      });
      setSelectedSlideIndex(slideIndex);
      if (collabMode === "agent") {
        setPendingAgentSelectionChanges((prev) => {
          const withoutType = prev.filter((item) => item.pageType !== pageType);
          const baseline = selectionBaselineRef.current[pageType] ?? null;
          if (baseline === slideIndex) return withoutType;
          return [...withoutType, { pageType, from: baseline, to: slideIndex }];
        });
      }
    },
    [collabMode, draft.page_selections, importId, review, status?.status, updateDraft],
  );

  const handleReviewPageTypeClick = useCallback(
    (pageType: TemplatePageType) => {
      setFocusedPageType(pageType);
      const assignedSlide = draft.page_selections?.[pageType];
      if (typeof assignedSlide === "number") {
        setSelectedSlideIndex(assignedSlide);
      }
    },
    [draft.page_selections],
  );

  const handleSelectTemplate = useCallback((tid: string) => {
    setSelectedTemplateId(tid);
  }, []);

  const handleDelete = useCallback(
    async (tmpl: TemplateInfo) => {
      if (!tmpl.editable) return;
      if (!window.confirm(t("template.deleteConfirm"))) return;
      try {
        await deleteTemplate(tmpl.template_id);
        await loadTemplates();
        if (selectedTemplateId === tmpl.template_id) {
          setSelectedTemplateId(null);
        }
      } catch (err) {
        setLibraryError(err instanceof Error ? err.message : t("templates.error.deleteFailed"));
      }
    },
    [loadTemplates, selectedTemplateId, t],
  );

  const handleRename = useCallback(
    async (tmpl: TemplateInfo) => {
      if (!tmpl.editable) return;
      const label = window.prompt(t("template.renamePrompt"), tmpl.label || tmpl.template_id);
      if (!label) return;
      try {
        await renameTemplate(tmpl.template_id, label);
        await loadTemplates();
      } catch (err) {
        setLibraryError(err instanceof Error ? err.message : t("templates.error.renameFailed"));
      }
    },
    [loadTemplates, t],
  );

  const handleUseForGeneration = useCallback(() => {
    if (!selectedTemplateId) return;
    try {
      const PRESENTATION_KEY = "paper-ppt-agent-presentation-settings-v1";
      const raw = window.localStorage.getItem(PRESENTATION_KEY);
      const draftSettings = raw ? JSON.parse(raw) : {};
      draftSettings.templateId = selectedTemplateId;
      window.localStorage.setItem(PRESENTATION_KEY, JSON.stringify(draftSettings));
    } catch {
      /* noop */
    }
    navigate("/generate");
  }, [navigate, selectedTemplateId]);

  // ── State decision ────────────────────────────────────────────────────
  const importStatus = status?.status;
  const isImporting =
    Boolean(importId) && importStatus !== "review_required" && importStatus !== "complete";
  const isReviewing = Boolean(importId) && importStatus === "review_required" && Boolean(review);
  const agentTemplateized =
    review?.llm?.agent === true &&
    review?.llm?.status === "complete" &&
    review?.llm?.templateized === true;
  const templateEditorRevision = agentTemplateized
    ? `clean:${review?.llm?.templateized_at ?? importPreview?.template_id ?? "ready"}`
    : `source:${review?.pptist_version ?? "draft"}`;

  const annotations: UserAnnotation[] = draft.annotations ?? review?.annotations ?? [];
  const activeAnnotations = useMemo(
    () => annotations.filter((annotation) => !annotation.resolved),
    [annotations],
  );

  const replyLanguage = useMemo<"zh" | "en">(() => {
    const fb = review?.feedback_history ?? [];
    const last = fb[fb.length - 1];
    return detectUserLanguage(last?.feedback ?? "");
  }, [review?.feedback_history]);

  // Confirm import gating follows the selected import contract.
  const directSlideCount = review?.slide_count ?? review?.slides?.length ?? 0;
  const directImportReady = collabMode !== "direct" || directSlideCount === 5;
  const representativePagesReady =
    collabMode === "direct" ||
    (Boolean(draft.page_selections?.cover) && Boolean(draft.page_selections?.content));
  const canConfirm =
    isReviewing &&
    Boolean(review) &&
    directImportReady &&
    representativePagesReady &&
    (collabMode !== "agent" || agentTemplateized) &&
    !(collabMode === "direct" && Boolean(directDesignSpec)) &&
    !confirmingFlag;
  const confirmDisabledHint =
    isReviewing && collabMode === "direct" && !directImportReady
      ? t("template.directFiveSlidesRequired")
      : isReviewing && collabMode === "direct" && directDesignSpec
        ? t("template.directReviewPending")
      : isReviewing && collabMode === "agent" && !agentTemplateized
        ? t("template.agentTemplateizationRequired")
        : "";

  const collabConversation = useMemo(() => {
    if (collabMode === "direct") return directConversation;
    const conversation = review?.conversation ?? [];
    const filtered = conversation.filter((message) => {
      const meta = message.meta ?? {};
      const isAgentMessage = meta.mode === "agent" || Boolean(meta.agent_job_id);
      return collabMode === "agent" ? isAgentMessage : !isAgentMessage;
    });
    return filtered;
  }, [collabMode, directConversation, review, review?.conversation]);

  const collabActivityEvents = useMemo(
    () =>
      buildAgentActivityEvents(status, review, draft, {
        mode: collabMode,
        agentEvents,
        llmEvents,
        t,
      }),
    [status, review, draft, collabMode, agentEvents, llmEvents, t],
  );

  const hasReadOnlyInspection = useMemo(
    () =>
      collabConversation.some((message) => {
        const meta = message.meta ?? {};
        return Boolean(meta.read_only) || meta.planning === false;
      }),
    [collabConversation],
  );

  const runReadOnlyInspection = useCallback(
    async (options: { silent?: boolean; reason?: string } = {}) => {
      if (collabMode !== "agent") return;
      const latestReview = await refreshReview();
      if (!latestReview?.pptist_version) {
        setTemplateError(t("template.agentSaveRequired"));
        return;
      }
      const selectionNote = formatPageSelectionChanges(pendingAgentSelectionChanges, t);
      await runAgent(
        [options.reason || t("template.agentGuide.recheckPrompt"), selectionNote].filter(Boolean).join("\n\n"),
        { ...agentConfig, reply_language: locale },
        { silent: options.silent, planning: false, preview: false },
      );
      if (selectionNote) setPendingAgentSelectionChanges([]);
    },
    [
      agentConfig,
      collabMode,
      locale,
      pendingAgentSelectionChanges,
      refreshReview,
      runAgent,
      setTemplateError,
      t,
    ],
  );

  const runTemplateizationFromConversation = useCallback(
    async (text: string) => {
      if (collabMode !== "agent") return;
      const latestReview = await refreshReview();
      if (!latestReview?.pptist_version) {
        setTemplateError(t("template.agentSaveRequired"));
        return;
      }
      const selectionNote = formatPageSelectionChanges(pendingAgentSelectionChanges, t);
      const userText = text.trim();
      const prompt = agentTemplateized
        ? [userText || t("template.agentGuide.startPrompt"), selectionNote]
        : [
            t("template.agentGuide.startPrompt"),
            userText ? `User request:\n${userText}` : "",
            selectionNote,
          ];
      await runAgent(
        prompt.filter(Boolean).join("\n\n"),
        { ...agentConfig, reply_language: locale },
        { planning: true, preview: true },
      );
      if (selectionNote) setPendingAgentSelectionChanges([]);
    },
    [
      agentConfig,
      agentTemplateized,
      collabMode,
      locale,
      pendingAgentSelectionChanges,
      refreshReview,
      runAgent,
      setTemplateError,
      t,
    ],
  );

  useEffect(() => {
    if (!isReviewing || collabMode !== "agent" || !importId || !review?.pptist_version) return;
    if (importLoading || hasReadOnlyInspection) return;
    const key = `${importId}:${review.pptist_version}`;
    if (autoInspectionRef.current === key) return;
    autoInspectionRef.current = key;
    void runReadOnlyInspection({
      silent: true,
      reason: t("template.agentGuide.autoInspect"),
    });
  }, [
    collabMode,
    hasReadOnlyInspection,
    importId,
    importLoading,
    isReviewing,
    review?.pptist_version,
    runReadOnlyInspection,
    t,
  ]);

  const selectedAnnotationSlide = selectedSlideIndex ?? draft.page_selections?.[focusedPageType] ?? 1;
  const clampUnit = (value: number) => Math.min(1, Math.max(0, value));
  const handleAnnotationPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (!annotationMode) return;
      const rect = event.currentTarget.getBoundingClientRect();
      const x = clampUnit((event.clientX - rect.left) / Math.max(1, rect.width));
      const y = clampUnit((event.clientY - rect.top) / Math.max(1, rect.height));
      event.currentTarget.setPointerCapture(event.pointerId);
      event.preventDefault();
      setAnnotationDraft({ startX: x, startY: y, x, y, width: 0, height: 0 });
    },
    [annotationMode],
  );
  const handleAnnotationPointerMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (!annotationMode || !annotationDraft) return;
      const rect = event.currentTarget.getBoundingClientRect();
      const currentX = clampUnit((event.clientX - rect.left) / Math.max(1, rect.width));
      const currentY = clampUnit((event.clientY - rect.top) / Math.max(1, rect.height));
      const x = Math.min(annotationDraft.startX, currentX);
      const y = Math.min(annotationDraft.startY, currentY);
      setAnnotationDraft({
        ...annotationDraft,
        x,
        y,
        width: Math.abs(currentX - annotationDraft.startX),
        height: Math.abs(currentY - annotationDraft.startY),
      });
    },
    [annotationDraft, annotationMode],
  );
  const handleAnnotationPointerUp = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (!annotationMode || !annotationDraft) return;
      event.preventDefault();
      const nextDraft = annotationDraft;
      setAnnotationDraft(null);
      if (nextDraft.width < 0.01 || nextDraft.height < 0.01) return;
      const note = window.prompt(t("template.annotations.prompt"));
      if (!note?.trim()) return;
      const now = Date.now() / 1000;
      const nextAnnotation: UserAnnotation = {
        annotation_id: `pending-${Date.now()}`,
        slide_index: selectedAnnotationSlide,
        bbox_norm: {
          x: clampUnit(nextDraft.x),
          y: clampUnit(nextDraft.y),
          width: clampUnit(nextDraft.width),
          height: clampUnit(nextDraft.height),
        },
        note: note.trim(),
        linked_element_id: null,
        created_at: now,
        resolved: false,
      };
      updateDraft({ annotations: [...annotations, nextAnnotation] });
      setAnnotationMode(false);
    },
    [annotationDraft, annotationMode, annotations, selectedAnnotationSlide, t, updateDraft],
  );

  const removeAnnotationById = useCallback(
    (annotationId: string) => {
      updateDraft({ annotations: annotations.filter((item) => item.annotation_id !== annotationId) });
    },
    [annotations, updateDraft],
  );

  return (
    <Layout showSidebar={false} contentClassName="studio-page templates-workspace-page">
      <section className="templates-workspace ti-surface" data-side-tab={workspaceSideTab}>
        <div className="workspace-side-tabs templates-side-tabs" role="tablist" aria-label={`${t("templates.libraryHeader")} / ${t("templates.collab.title")}`}>
          <button
            type="button"
            className={`workspace-side-tab ${workspaceSideTab === "sources" ? "workspace-side-tab-active" : ""}`}
            aria-selected={workspaceSideTab === "sources"}
            role="tab"
            onClick={() => setWorkspaceSideTab("sources")}
          >
            <Library size={16} />
            <span>{t("templates.libraryHeader")}</span>
          </button>
          <button
            type="button"
            className={`workspace-side-tab ${workspaceSideTab === "config" ? "workspace-side-tab-active" : ""}`}
            aria-selected={workspaceSideTab === "config"}
            role="tab"
            onClick={() => setWorkspaceSideTab("config")}
          >
            <Sparkles size={16} />
            <span>{t("templates.collab.title")}</span>
          </button>
        </div>
        {/* ───────── LEFT COLUMN: Library + upload ───────── */}
        <aside
          className="sources-panel flex flex-col gap-3 overflow-hidden"
          style={{ background: "var(--ti-surface)", gridArea: "sources" }}
        >
          <div className="workspace-panel-header" style={{ padding: "12px 14px" }}>
            <div className="workspace-panel-title">
              <Library size={18} />
              <span>{t("templates.libraryHeader")}</span>
            </div>
          </div>
          <div className="flex flex-1 flex-col gap-3 overflow-y-auto px-3 pb-3">
            <UploadCard
              onUpload={handleUpload}
              uploading={(importLoading && !importId) || checkingAgentRuntime}
              mode={uploadMode}
              onModeChange={setUploadMode}
              modelConfigured={modelConfigured}
              agentConfigured={agentConfigured}
              checkingAgentRuntime={checkingAgentRuntime}
              compact
            />
            {templates.length > 0 ? (
              <FilterChips filter={filter} onChange={setFilter} />
            ) : null}
            {libraryError ? (
              <p
                className="rounded-[var(--ti-radius-sm,6px)] border px-2 py-1 text-xs"
                style={{
                  borderColor: "color-mix(in srgb, var(--ti-danger) 50%, var(--ti-line))",
                  color: "var(--ti-danger)",
                  background: "color-mix(in srgb, var(--ti-danger) 8%, transparent)",
                }}
              >
                {libraryError}
              </p>
            ) : null}
            {templatesLoading ? (
              <div className="flex flex-col gap-1.5">
                <div className="h-14 animate-pulse rounded" style={{ background: "var(--ti-surface-inset)" }} />
                <div className="h-14 animate-pulse rounded" style={{ background: "var(--ti-surface-inset)" }} />
                <div className="h-14 animate-pulse rounded" style={{ background: "var(--ti-surface-inset)" }} />
              </div>
            ) : filteredTemplates.length === 0 ? (
              <EmptyLibraryHint />
            ) : (
              <ul className="flex flex-col gap-1.5">
                {filteredTemplates.map((tmpl) => (
                  <li key={tmpl.template_id}>
                    <LibraryRow
                      template={tmpl}
                      active={selectedTemplateId === tmpl.template_id}
                      onSelect={() => handleSelectTemplate(tmpl.template_id)}
                      onRename={() => void handleRename(tmpl)}
                      onDelete={() => void handleDelete(tmpl)}
                    />
                  </li>
                ))}
              </ul>
            )}
          </div>
        </aside>

        {/* ───────── MIDDLE COLUMN: header + stage + bottom rail ─────────
         * Mirrors the workspace shape from GeneratePage exactly:
         *   .slide-workspace-panel (header on top)
         *     └─ .slide-stage (vertical thumbnail rail + big canvas)
         *     └─ .templates-bottom-rail (5 page-type tile strip,
         *        replaces the workspace's .agent-monitor-panel slot).
         */}
        <main
          className="slide-workspace-panel templates-slide-panel"
          style={{ gridArea: "slides" }}
        >
          {selectedTemplate && !isReviewing && !isImporting ? (
          <div className="slide-workspace-header">
            <p>
              <span>{selectedTemplate.label || selectedTemplate.template_id}</span>
            </p>
            {selectedTemplate.editable ? (
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => void handleRename(selectedTemplate)}
                  className="ti-focusable inline-flex items-center gap-1 rounded-[var(--ti-radius-sm,6px)] border px-2.5 py-1 text-xs font-semibold"
                  style={{
                    borderColor: "var(--ti-line)",
                    background: "var(--ti-surface)",
                    color: "var(--ti-text)",
                  }}
                >
                  <Pencil size={12} />
                  {t("templates.actions.rename")}
                </button>
                <button
                  type="button"
                  onClick={() => void handleDelete(selectedTemplate)}
                  className="ti-focusable inline-flex items-center gap-1 rounded-[var(--ti-radius-sm,6px)] border px-2.5 py-1 text-xs font-semibold"
                  style={{
                    borderColor: "color-mix(in srgb, var(--ti-danger) 40%, var(--ti-line))",
                    background: "var(--ti-surface)",
                    color: "var(--ti-danger)",
                  }}
                >
                  <Trash2 size={12} />
                  {t("templates.actions.delete")}
                </button>
                <button
                  type="button"
                  onClick={handleUseForGeneration}
                  className="ti-focusable inline-flex items-center gap-1 rounded-[var(--ti-radius-sm,6px)] px-2.5 py-1 text-xs font-semibold"
                  style={{ background: "var(--ti-accent)", color: "var(--ti-accent-fg)" }}
                >
                  <Wand2 size={12} />
                  {t("templates.actions.useForGeneration")}
                </button>
              </div>
            ) : (
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={handleUseForGeneration}
                  className="ti-focusable inline-flex items-center gap-1 rounded-[var(--ti-radius-sm,6px)] px-2.5 py-1 text-xs font-semibold"
                  style={{ background: "var(--ti-accent)", color: "var(--ti-accent-fg)" }}
                >
                  <Wand2 size={12} />
                  {t("templates.actions.useForGeneration")}
                </button>
              </div>
            )}
          </div>
          ) : null}

          {isReviewing && importId ? (
            <div className="pptist-template-review-stack">
              <div className="ti-template-editor-wrap">
                <PptistStudioHost
                  source={{ kind: "templateImport", importId, revision: templateEditorRevision }}
                  className="pptist-template-host"
                  onSaved={() => {
                    setDirectDesignSpec("");
                    setDirectConversation([]);
                    if (collabMode !== "direct") void refreshReview();
                  }}
                  onConfirmImport={() => void handleConfirm()}
                  saveBeforeConfirmImport={collabMode === "direct"}
                  confirmImportDisabled={!canConfirm}
                  confirmImportHint={confirmDisabledHint}
                  onCancelImport={handleCancelImport}
                />
                {annotationMode ? (
                  <HoverTooltip content={t("template.annotations.dragHint")} className="ti-annotation-tooltip-trigger">
                    <div
                      className="ti-annotation-overlay"
                      onPointerDown={handleAnnotationPointerDown}
                      onPointerMove={handleAnnotationPointerMove}
                      onPointerUp={handleAnnotationPointerUp}
                    >
                      {annotationDraft ? (
                        <div
                          className="ti-annotation-draft-box"
                          style={{
                            left: `${annotationDraft.x * 100}%`,
                            top: `${annotationDraft.y * 100}%`,
                            width: `${annotationDraft.width * 100}%`,
                            height: `${annotationDraft.height * 100}%`,
                          }}
                        />
                      ) : (
                        <span>{t("template.annotations.dragHint")}</span>
                      )}
                    </div>
                  </HoverTooltip>
                ) : null}
              </div>
              {review ? (
                <div className="ti-review-bottom-tools">
                  <TemplateAnnotationTools
                    annotations={activeAnnotations}
                    annotationMode={annotationMode}
                    onToggleAnnotationMode={() => {
                      setAnnotationDraft(null);
                      setAnnotationMode((enabled) => !enabled);
                    }}
                    onDelete={removeAnnotationById}
                  />
                  {collabMode === "direct" ? (
                    <DirectImportContract slideCount={directSlideCount} />
                  ) : (
                    <ReviewPageAssignments
                      review={review}
                      selections={draft.page_selections ?? {}}
                      focusedPageType={focusedPageType}
                      layout="horizontal"
                      onFocus={handleReviewPageTypeClick}
                      onAssign={handleAssignPageTypeToSlide}
                    />
                  )}
                </div>
              ) : null}
            </div>
          ) : isImporting ? (
            <TemplateImportingState>
              <div className="templates-stage-importing">
                <div>
                  {collabMode === "agent" ? (
                    <AgentImportingView
                      message={translateTemplateImportMessage(status?.message, locale) || t("template.uploading")}
                      onCancel={handleCancelImport}
                    />
                  ) : (
                    <>
                      <ProgressView
                        status={
                          status ?? {
                            import_id: importId ?? "",
                            status: "processing",
                            progress: 0,
                            message: t("template.uploading"),
                          }
                        }
                        mode={collabMode === "direct" ? "direct" : "llm"}
                        onRetry={(stepId) => void retryStep(stepId)}
                      />
                    </>
                  )}
                </div>
              </div>
            </TemplateImportingState>
          ) : !(selectedTemplate && preview) ? (
            <MiddleEmptyState />
          ) : (
            <div className="slide-stage templates-slide-stage-grid">
              <aside
                className="thumbnail-rail templates-vertical-rail"
                aria-label={t("templates.thumbnailRail.empty")}
              >
                {!isImporting && selectedTemplate && preview ? (
                  PAGE_TYPES.map((pt) => (
                    <PageTypeThumb
                      key={pt}
                      pageType={pt}
                      svg={pickPreviewSvg(preview, pt)}
                      active={focusedPageType === pt}
                      onClick={() => setFocusedPageType(pt)}
                    />
                  ))
                ) : (
                  <EmptySlideThumb />
                )}
              </aside>

              <div className="templates-right-column">
                <div className="slide-canvas-area templates-canvas-area">
                  {selectedTemplate && preview ? (
                    previewLoading ? (
                      <div className="templates-big-preview">
                        <div className="templates-big-preview-frame">
                          <div className="templates-big-preview-empty">
                            <Loader2 size={20} className="animate-spin" />
                          </div>
                        </div>
                      </div>
                    ) : (
                      <BigPreview
                        svg={pickPreviewSvg(preview, focusedPageType)}
                        pageType={focusedPageType}
                      />
                    )
                  ) : null}
                </div>
                <BottomRail
                  mode={isReviewing ? "review" : isImporting ? "importing" : selectedTemplate && preview ? "browsing" : "empty"}
                  preview={preview}
                  importPreview={importPreview ?? null}
                  draftPageSelections={draft.page_selections}
                  focusedPageType={focusedPageType}
                  selectedSlideIndex={selectedSlideIndex}
                  onSelectPageType={setFocusedPageType}
                  onAssignPageType={handleAssignPageTypeToSlide}
                  onReviewPageTypeClick={handleReviewPageTypeClick}
                  slides={review?.slides ?? []}
                />
              </div>
            </div>
          )}
        </main>

        {/* ───────── RIGHT COLUMN: Collab ───────── */}
        <aside
          className="templates-config-panel"
          style={{ gridArea: "config" }}
        >
          <div className="templates-config-header">
            <div className="templates-config-header-title">
              <Sparkles size={16} />
              <span>{t("templates.collab.title")}</span>
            </div>
          </div>

          <div className="templates-config-scroll">
            <div className="templates-config-collab">
              <CollabPanel
                conversation={collabConversation}
                activityEvents={collabActivityEvents}
                agentEvents={agentEvents}
                replyLanguage={replyLanguage}
                loading={Boolean(importLoading)}
                mode={collabMode}
                onModeChange={(mode) => {
                  if (!importId) {
                    setUploadMode(mode);
                    setCollabMode(mode);
                  }
                }}
                modeLocked={Boolean(importId)}
                agentConfig={agentConfig}
                onAgentConfigChange={setAgentConfig}
                agentModelOptions={agentModelOptions}
                agentStatus={agentStatus}
                onSendFeedback={async (text) => {
                  if (collabMode === "agent") {
                    await runTemplateizationFromConversation(text);
                  } else if (collabMode === "direct") {
                    if (!selectedTemplateId || !modelConfig) return;
                    const now = Date.now() / 1000;
                    setDirectConversation((prev) => [
                      ...prev,
                      { role: "user", content: text || t("templates.designSpec.generate"), created_at: now, meta: { mode: "direct" } },
                    ]);
                    const nextPreview = await generateTemplateDesignSpec(selectedTemplateId, modelConfig, text);
                    setPreview(nextPreview);
                    setDirectConversation((prev) => [
                      ...prev,
                      { role: "assistant", content: t("templates.designSpec.generated"), created_at: Date.now() / 1000, meta: { mode: "direct" } },
                    ]);
                  } else {
                    await assist(text);
                  }
                }}
                onStopAgent={cancelAgent}
                importId={importId}
                contextAttachments={pendingAgentSelectionChanges.map((change) => {
                  const label = t(`templates.preview.tilelabel.${change.pageType}`);
                  const from = change.from ? String(change.from) : t("templates.chip.notAssigned");
                  return {
                    id: `selection:${change.pageType}`,
                    label: `${label}: ${from} -> ${change.to}`,
                    detail: t("templates.chip.pendingAgentChange"),
                  };
                })}
                modelConfigured={collabMode === "agent" ? agentConfigured : modelConfigured}
                annotationCount={activeAnnotations.length}
                modelLabel={
                  collabMode === "agent"
                    ? agentConfig.model?.trim() || t("template.collab.agentClaudeCodeDefault")
                    : modelConfig?.model
                }
                importStatus={status}
                review={review}
                draftState={draft}
                agentCancelPending={agentCancelPending}
                directDesignSpec={directDesignSpec}
                directImportBusy={confirmingFlag}
                onConfirmDirectImport={handleConfirmDirectImport}
                onEditDirectImport={handleEditDirectImport}
              />
            </div>
          </div>
        </aside>
      </section>
      {showDirectDesignSpecConfirm
        ? createPortal(
            <DirectDesignSpecGenerationConfirm
              busy={confirmingFlag}
              onClose={() => setShowDirectDesignSpecConfirm(false)}
              onConfirm={() => void handleGenerateDirectDesignSpec()}
            />,
            document.body,
          )
        : null}
    </Layout>
  );
}

function DirectImportContract({ slideCount }: { slideCount: number }) {
  const { t } = useLocale();
  const ready = slideCount === 5;
  return (
    <section className="ti-review-assignments" data-layout="horizontal">
      <div className="ti-review-assignments-title">
        <FileCheck2 size={14} />
        <span>{t("template.directContract.title")}</span>
      </div>
      <div className="ti-review-assignments-list">
        {PAGE_TYPES.map((pageType, index) => (
          <div
            key={pageType}
            className="ti-review-assignment-row"
            data-active={ready && index + 1 <= slideCount}
          >
            <span className="ti-review-assignment-label">
              {String(index + 1).padStart(2, "0")} · {t(`templates.preview.tilelabel.${pageType}`)}
            </span>
            <span
              className="ti-review-assignment-select"
              style={{
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                pointerEvents: "none",
              }}
            >
              {t("template.directContract.fixed")}
            </span>
          </div>
        ))}
      </div>
      {!ready ? (
        <p className="mt-2 text-xs" style={{ color: "var(--ti-danger)" }}>
          {t("template.directFiveSlidesRequired")}
        </p>
      ) : null}
    </section>
  );
}

function TemplateAnnotationTools({
  annotations,
  annotationMode,
  onToggleAnnotationMode,
  onDelete,
}: {
  annotations: UserAnnotation[];
  annotationMode: boolean;
  onToggleAnnotationMode: () => void;
  onDelete: (annotationId: string) => void;
}) {
  const { t } = useLocale();
  return (
    <section className="ti-review-annotations">
      <div className="ti-review-annotations-head">
        <div className="ti-review-annotations-title">
          <Bookmark size={14} />
          <span>{t("template.annotations.title")}</span>
          <strong>{annotations.length}</strong>
        </div>
        <button
          type="button"
          className="ti-focusable ti-review-annotation-button"
          data-active={annotationMode}
          onClick={onToggleAnnotationMode}
        >
          <MessageSquarePlus size={13} />
          <span>{annotationMode ? t("template.annotations.cancel") : t("template.annotations.add")}</span>
        </button>
      </div>
      <div className="ti-review-annotation-list">
        {annotations.length === 0 ? (
          <span className="ti-review-annotation-empty">{t("template.annotations.empty")}</span>
        ) : (
          annotations.slice(-3).map((annotation) => (
            <div className="ti-review-annotation-chip" key={annotation.annotation_id}>
              <span>
                {String(annotation.slide_index).padStart(2, "0")} · {annotation.note}
              </span>
              <button type="button" onClick={() => onDelete(annotation.annotation_id)}>
                {t("template.annotations.delete")}
              </button>
            </div>
          ))
        )}
      </div>
    </section>
  );
}

function ReviewPageAssignments({
  review,
  selections,
  focusedPageType,
  layout = "side",
  onFocus,
  onAssign,
}: {
  review: { slides: TemplateImportSlide[] };
  selections: Partial<Record<TemplatePageType, number | null>>;
  focusedPageType: TemplatePageType;
  layout?: "side" | "horizontal";
  onFocus?: (pageType: TemplatePageType) => void;
  onAssign: (pageType: TemplatePageType, slideIndex: number) => void;
}) {
  const { t } = useLocale();
  const slides = review.slides ?? [];
  return (
    <section className="ti-review-assignments" data-layout={layout}>
      <div className="ti-review-assignments-title">
        <FileCheck2 size={14} />
        <span>{t("template.assignments.title")}</span>
      </div>
      <div className="ti-review-assignments-list">
        {PAGE_TYPES.map((pageType) => {
          const selected = selections[pageType] ?? "";
          return (
            <div
              key={pageType}
              className="ti-review-assignment-row"
              data-active={focusedPageType === pageType}
            >
              <button
                type="button"
                className="ti-review-assignment-label ti-focusable"
                onClick={() => onFocus?.(pageType)}
              >
                {t(`templates.preview.tilelabel.${pageType}`)}
              </button>
              <select
                value={selected || ""}
                onChange={(event) => {
                  const value = Number(event.currentTarget.value);
                  if (value > 0) onAssign(pageType, value);
                }}
                className="ti-review-assignment-select ti-focusable"
              >
                <option value="">{t("templates.chip.notAssigned")}</option>
                {slides.map((slide) => (
                  <option key={slide.index} value={slide.index}>
                    {String(slide.index).padStart(2, "0")}
                  </option>
                ))}
              </select>
            </div>
          );
        })}
      </div>
    </section>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Left column: upload card + library row
// ─────────────────────────────────────────────────────────────────────────

interface UploadCardProps {
  onUpload: (file: File) => Promise<void>;
  uploading: boolean;
  mode: CollabMode;
  onModeChange: (mode: CollabMode) => void;
  modelConfigured: boolean;
  agentConfigured: boolean;
  checkingAgentRuntime: boolean;
  compact?: boolean;
}

function UploadCard({
  onUpload,
  uploading,
  mode,
  onModeChange,
  modelConfigured,
  agentConfigured,
  checkingAgentRuntime,
  compact,
}: UploadCardProps) {
  const { t } = useLocale();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [guideMode, setGuideMode] = useState<CollabMode | null>(null);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [dismissedGuides, setDismissedGuides] = useState<UploadGuideDismissals>(readTemplateUploadGuideDismissals);
  const [skipGuideNextTime, setSkipGuideNextTime] = useState(false);
  const ready = mode === "agent" ? agentConfigured : true;

  const handleFile = async (file: File) => {
    const isPptx =
      file.name.toLowerCase().endsWith(".pptx") ||
      file.type ===
        "application/vnd.openxmlformats-officedocument.presentationml.presentation";
    if (!isPptx) return;
    await onUpload(file);
  };

  useEffect(() => {
    setGuideMode(null);
    setPendingFile(null);
    setSkipGuideNextTime(false);
  }, [mode]);

  useEffect(() => {
    if (!uploading) return;
    setGuideMode(null);
    setPendingFile(null);
    setSkipGuideNextTime(false);
  }, [uploading]);

  const requestFileSelection = () => {
    if (dismissedGuides[mode]) {
      inputRef.current?.click();
      return;
    }
    setPendingFile(null);
    setSkipGuideNextTime(false);
    setGuideMode(mode);
  };

  const continueUpload = () => {
    if (guideMode && skipGuideNextTime) {
      setDismissedGuides(writeTemplateUploadGuideDismissal(guideMode, true));
    }
    setGuideMode(null);
    setSkipGuideNextTime(false);
    if (pendingFile) {
      const file = pendingFile;
      setPendingFile(null);
      window.setTimeout(() => {
        void handleFile(file);
      }, 0);
      return;
    }
    window.setTimeout(() => {
      inputRef.current?.click();
    }, 0);
  };

  const onDragOver = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(true);
  };
  const onDragLeave = () => setDragging(false);
  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (!file) return;
    if (dismissedGuides[mode]) {
      void handleFile(file);
      return;
    }
    setPendingFile(file);
    setSkipGuideNextTime(false);
    setGuideMode(mode);
  };

  return (
    <div className="flex flex-col gap-2">
      <div
        className="ti-upload-mode-switch"
        role="radiogroup"
        aria-label={t("templates.upload.mode.label")}
      >
        {(["direct", "agent"] as const).map((item) => {
          const active = mode === item;
          const label = item === "agent" ? "Agent" : t("templates.upload.mode.direct");
          const hint = item === "agent"
            ? t("templates.upload.mode.agentHint")
            : t("templates.upload.mode.directHint");
          return (
            <HoverTooltip key={item} content={hint} className="ti-upload-mode-tooltip-trigger">
              <button
                type="button"
                disabled={uploading}
                onClick={() => onModeChange(item)}
                className="ti-focusable ti-upload-mode-button"
                data-active={active}
                aria-checked={active}
                aria-label={label}
                role="radio"
              >
                {item === "agent" ? <Bot size={12} /> : <FileCheck2 size={12} />}
                <span className="ti-upload-mode-label">{label}</span>
              </button>
            </HoverTooltip>
          );
        })}
      </div>
      <div
        role="button"
        tabIndex={0}
        aria-busy={uploading}
        onClick={() => !uploading && requestFileSelection()}
        onKeyDown={(e) => {
          if ((e.key === "Enter" || e.key === " ") && !uploading) {
            e.preventDefault();
            requestFileSelection();
          }
        }}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        className={`ti-upload-card ti-focusable group ${dragging ? "ti-upload-card-dragging" : ""} ${compact ? "ti-upload-card-compact" : ""}`}
      >
        <span className="ti-upload-icon">
          {uploading ? <Loader2 size={40} className="animate-spin" /> : <UploadCloud size={40} strokeWidth={1.5} />}
        </span>
        <strong className="text-sm">{t("templates.upload.title")}</strong>
        <span className="text-xs" style={{ color: "var(--ti-muted)" }}>
          {checkingAgentRuntime ? t("templates.upload.checkingClaude") : t("templates.upload.hint")}
        </span>
        <input
          ref={inputRef}
          type="file"
          accept=".pptx,application/vnd.openxmlformats-officedocument.presentationml.presentation"
          className="hidden"
          onChange={(event) => {
            const file = event.currentTarget.files?.[0];
            if (file) void handleFile(file);
            event.currentTarget.value = "";
          }}
        />
      </div>
      {!ready ? (
        <div
          role="status"
          className="flex items-start gap-2 rounded-[var(--ti-radius-sm,6px)] border px-2 py-1.5 text-xs"
          style={{
            borderColor: "color-mix(in srgb, var(--ti-warning) 50%, var(--ti-line))",
            background: "color-mix(in srgb, var(--ti-warning) 10%, transparent)",
            color: "var(--ti-text)",
          }}
        >
          <span>
            {mode === "agent"
              ? t("template.agentClaudeCodeRequired")
              : ""}
          </span>
        </div>
      ) : null}
      {guideMode === "direct"
        ? createPortal(
            <DirectImportUploadGuide
              onClose={() => {
                setGuideMode(null);
                setPendingFile(null);
                setSkipGuideNextTime(false);
              }}
              onContinue={continueUpload}
              dontShowAgain={skipGuideNextTime}
              onDontShowAgainChange={setSkipGuideNextTime}
            />,
            document.body,
          )
        : null}
      {guideMode === "agent"
        ? createPortal(
            <AgentImportUploadGuide
              onClose={() => {
                setGuideMode(null);
                setPendingFile(null);
                setSkipGuideNextTime(false);
              }}
              onContinue={continueUpload}
              dontShowAgain={skipGuideNextTime}
              onDontShowAgainChange={setSkipGuideNextTime}
            />,
            document.body,
          )
        : null}
    </div>
  );
}

function AgentImportUploadGuide({
  onClose,
  onContinue,
  dontShowAgain,
  onDontShowAgainChange,
}: {
  onClose: () => void;
  onContinue: () => void;
  dontShowAgain: boolean;
  onDontShowAgainChange: (checked: boolean) => void;
}) {
  const { t } = useLocale();

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className="ti-direct-guide-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="ti-agent-upload-guide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="agent-import-guide-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="ti-direct-guide-header">
          <div>
            <p className="ti-direct-guide-kicker">Agent</p>
            <h2 id="agent-import-guide-title">{t("template.agentGuide.uploadTitle")}</h2>
            <p>{t("template.agentGuide.uploadDescription")}</p>
          </div>
          <button
            type="button"
            className="ti-focusable ti-direct-guide-close"
            onClick={onClose}
            aria-label={t("template.agentGuide.uploadClose")}
          >
            <X size={16} />
          </button>
        </header>
        <div className="ti-agent-upload-requirement">
          <Bot size={18} />
          <div>
            <strong>{t("template.agentGuide.uploadRequirement")}</strong>
            <p>{t("template.agentGuide.uploadRequirementDetail")}</p>
          </div>
        </div>
        <footer className="ti-agent-upload-actions">
          <UploadGuideOptOut checked={dontShowAgain} onChange={onDontShowAgainChange} />
          <div className="ti-upload-guide-buttons">
            <button type="button" className="ti-focusable ti-direct-guide-secondary" onClick={onClose}>
              {t("template.directGuide.cancel")}
            </button>
            <button type="button" className="ti-focusable ti-direct-guide-primary" onClick={onContinue}>
              <UploadCloud size={15} />
              {t("template.agentGuide.uploadContinue")}
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}

function DirectImportUploadGuide({
  onClose,
  onContinue,
  dontShowAgain,
  onDontShowAgainChange,
}: {
  onClose: () => void;
  onContinue: () => void;
  dontShowAgain: boolean;
  onDontShowAgainChange: (checked: boolean) => void;
}) {
  const { t } = useLocale();

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className="ti-direct-guide-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="ti-direct-guide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="direct-import-guide-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="ti-direct-guide-header">
          <div>
            <p className="ti-direct-guide-kicker">{t("templates.upload.mode.direct")}</p>
            <h2 id="direct-import-guide-title">{t("template.directGuide.title")}</h2>
            <p>{t("template.directGuide.description")}</p>
          </div>
          <button
            type="button"
            className="ti-focusable ti-direct-guide-close"
            onClick={onClose}
            aria-label={t("template.directGuide.close")}
          >
            <X size={16} />
          </button>
        </header>
        <div className="ti-direct-guide-notice">
          <FileCheck2 size={16} />
          <span>{t("template.directGuide.format")}</span>
        </div>
        <div className="ti-direct-guide-grid" aria-label={t("template.directGuide.orderTitle")}>
          {PAGE_TYPES.map((pageType, index) => (
            <div className="ti-direct-guide-page" key={pageType}>
              <div className="ti-direct-guide-page-title">
                <b>{String(index + 1).padStart(2, "0")}</b>
                <strong>{t(`template.page.${pageType}`)}</strong>
              </div>
              <p>{DIRECT_PLACEHOLDER_RULES[pageType].join("  ")}</p>
            </div>
          ))}
        </div>
        <div className="ti-direct-guide-rules">
          <h3>{t("template.directGuide.placeholderTitle")}</h3>
          <ul>
            <li>{t("template.directGuide.placeholderRuleSyntax")}</li>
            <li>{t("template.directGuide.placeholderRuleUnique")}</li>
            <li>{t("template.directGuide.placeholderRuleFixed")}</li>
            <li>{t("template.directGuide.placeholderRuleModel")}</li>
          </ul>
        </div>
        <footer className="ti-direct-guide-actions">
          <UploadGuideOptOut checked={dontShowAgain} onChange={onDontShowAgainChange} />
          <div className="ti-upload-guide-buttons">
            <button type="button" className="ti-focusable ti-direct-guide-secondary" onClick={onClose}>
              {t("template.directGuide.cancel")}
            </button>
            <button type="button" className="ti-focusable ti-direct-guide-primary" onClick={onContinue}>
              <UploadCloud size={15} />
              {t("template.directGuide.continue")}
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}

function UploadGuideOptOut({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  const { t } = useLocale();
  return (
    <label className="ti-upload-guide-optout">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.currentTarget.checked)}
      />
      <span>{t("template.uploadGuide.dontShowAgain")}</span>
    </label>
  );
}

function DirectDesignSpecGenerationConfirm({
  busy,
  onClose,
  onConfirm,
}: {
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const { t } = useLocale();

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, onClose]);

  return (
    <div
      className="ti-direct-guide-backdrop"
      role="presentation"
      onMouseDown={() => {
        if (!busy) onClose();
      }}
    >
      <section
        className="ti-agent-upload-guide ti-direct-spec-confirm"
        role="dialog"
        aria-modal="true"
        aria-labelledby="direct-spec-confirm-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="ti-direct-guide-header">
          <div>
            <p className="ti-direct-guide-kicker">{t("templates.upload.mode.direct")}</p>
            <h2 id="direct-spec-confirm-title">{t("template.directSpecConfirm.title")}</h2>
            <p>{t("template.directSpecConfirm.description")}</p>
          </div>
          <button
            type="button"
            className="ti-focusable ti-direct-guide-close"
            onClick={onClose}
            disabled={busy}
            aria-label={t("template.directSpecConfirm.close")}
          >
            <X size={16} />
          </button>
        </header>
        <div className="ti-direct-spec-info">
          <FileCheck2 size={18} />
          <div>
            <p>{t("template.directSpecConfirm.whatItDoes")}</p>
            <p>{t("template.directSpecConfirm.secondStep")}</p>
          </div>
        </div>
        <p className="ti-direct-spec-cost">{t("template.directSpecConfirm.cost")}</p>
        <footer className="ti-agent-upload-actions ti-direct-spec-actions">
          <div className="ti-upload-guide-buttons">
            <button type="button" className="ti-focusable ti-direct-guide-secondary" onClick={onClose} disabled={busy}>
              {t("template.directSpecConfirm.cancel")}
            </button>
            <button type="button" className="ti-focusable ti-direct-guide-primary" onClick={onConfirm} disabled={busy}>
              {busy ? <Loader2 size={15} className="animate-spin" /> : <Sparkles size={15} />}
              {t("template.directSpecConfirm.confirm")}
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}

function FilterChips({
  filter,
  onChange,
}: {
  filter: LibraryFilter;
  onChange: (f: LibraryFilter) => void;
}) {
  const { t } = useLocale();
  const opts: Array<{ id: LibraryFilter; label: string }> = [
    { id: "all", label: t("templates.filter.all") },
    { id: "builtin", label: t("templates.filter.builtin") },
    { id: "user", label: t("templates.filter.user") },
  ];
  return (
    <div className="flex items-center gap-1">
      {opts.map((o) => {
        const active = filter === o.id;
        return (
          <button
            key={o.id}
            type="button"
            onClick={() => onChange(o.id)}
            className="ti-focusable rounded-full border px-2.5 py-0.5 text-[11px] font-semibold"
            style={{
              borderColor: active ? "var(--ti-accent)" : "var(--ti-line)",
              background: active
                ? "color-mix(in srgb, var(--ti-accent) 12%, var(--ti-surface))"
                : "var(--ti-surface)",
              color: active ? "var(--ti-accent)" : "var(--ti-muted)",
            }}
            aria-pressed={active}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

function LibraryRow({
  template,
  active,
  onSelect,
  onRename,
  onDelete,
}: {
  template: TemplateInfo;
  active: boolean;
  onSelect: () => void;
  onRename: () => void;
  onDelete: () => void;
}) {
  const { t } = useLocale();
  const isUser = template.source === "user";
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      className="ti-focusable group relative flex items-center gap-2 overflow-hidden rounded-[var(--ti-radius-sm,6px)] border py-2 pl-3 pr-2 text-sm"
      style={{
        borderColor: active ? "var(--ti-accent)" : "var(--ti-line)",
        background: active ? "var(--ti-surface-inset)" : "var(--ti-surface)",
        color: "var(--ti-text)",
      }}
    >
      {active ? (
        <span
          aria-hidden="true"
          className="absolute left-0 top-0 h-full w-[3px]"
          style={{ background: "var(--ti-accent)" }}
        />
      ) : null}
      <ThumbBadge template={template} />
      <div className="min-w-0 flex-1">
        <strong className="block truncate text-sm">
          {template.label || template.template_id}
        </strong>
        <span className="templates-library-source">
          {isUser ? t("templates.badge.user") : t("templates.badge.builtin")}
        </span>
      </div>
      {template.editable ? (
        <div className="flex items-center gap-0.5 opacity-0 transition group-hover:opacity-100">
          <button
            type="button"
            aria-label={t("templates.actions.rename")}
            onClick={(e) => {
              e.stopPropagation();
              onRename();
            }}
            className="ti-focusable inline-flex h-6 w-6 items-center justify-center rounded"
            style={{ color: "var(--ti-muted)" }}
          >
            <Pencil size={11} />
          </button>
          <button
            type="button"
            aria-label={t("templates.actions.delete")}
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
            className="ti-focusable inline-flex h-6 w-6 items-center justify-center rounded"
            style={{ color: "var(--ti-danger)" }}
          >
            <Trash2 size={11} />
          </button>
        </div>
      ) : null}
    </div>
  );
}

function ThumbBadge({ template }: { template: TemplateInfo }) {
  const tone = template.source === "user" ? "var(--ti-success)" : "var(--ti-accent)";
  return (
    <div
      aria-hidden="true"
      className="flex h-9 w-12 flex-shrink-0 items-center justify-center rounded-[4px] border"
      style={{
        borderColor: "var(--ti-line)",
        background: `color-mix(in srgb, ${tone} 10%, var(--ti-surface-inset))`,
        color: tone,
      }}
    >
      <Layers size={16} />
    </div>
  );
}

function EmptyLibraryHint() {
  const { t } = useLocale();
  return (
    <div
      className="flex flex-col items-center gap-2 rounded-[var(--ti-radius-md,10px)] border p-4 text-center"
      style={{
        borderStyle: "dashed",
        borderColor: "var(--ti-line)",
        background: "var(--ti-surface-inset)",
      }}
    >
      <Inbox size={20} style={{ color: "var(--ti-muted)" }} />
      <strong className="text-xs" style={{ color: "var(--ti-text)" }}>
        {t("templates.empty.title")}
      </strong>
      <span className="text-[11px]" style={{ color: "var(--ti-muted)" }}>
        {t("templates.empty.hint")}
      </span>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Middle column thumbnails
// ─────────────────────────────────────────────────────────────────────────

function PageTypeThumb({
  pageType,
  svg,
  active,
  onClick,
}: {
  pageType: TemplatePageType;
  svg: string | undefined;
  active: boolean;
  onClick: () => void;
}) {
  const { t } = useLocale();
  return (
    <button
      type="button"
      onClick={onClick}
      className={`ti-focusable templates-rail-tile ${active ? "templates-rail-tile-active" : ""}`}
      aria-pressed={active}
    >
      <div className="templates-rail-thumb">
        {svg ? (
          <div dangerouslySetInnerHTML={{ __html: sanitizeSvg(svg) }} />
        ) : null}
      </div>
      <span className="templates-rail-label">
        {t(`templates.preview.tilelabel.${pageType}`)}
      </span>
    </button>
  );
}

function SlideThumb({
  index,
  svg,
  imageUrl,
  active,
  onClick,
}: {
  index: number;
  svg: string | undefined;
  imageUrl?: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rail-slide ${active ? "rail-slide-active" : ""}`}
      aria-pressed={active}
    >
      <span>{index}</span>
      <div>
        {imageUrl ? (
          <img src={imageUrl} alt="" draggable={false} />
        ) : svg ? (
          <div dangerouslySetInnerHTML={{ __html: sanitizeSvg(svg) }} />
        ) : null}
      </div>
    </button>
  );
}

function EmptySlideThumb() {
  return (
    <div className="rail-slide rail-slide-empty rail-slide-active" aria-hidden="true">
      <span>1</span>
      <div className="rail-empty-frame" />
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Bottom rail — replaces the workspace's `.agent-monitor-panel` slot
// ─────────────────────────────────────────────────────────────────────────

type BottomRailMode = "review" | "browsing" | "importing" | "empty";

interface BottomRailProps {
  mode: BottomRailMode;
  preview: TemplatePreview | null;
  importPreview: TemplatePreview | null;
  draftPageSelections: Partial<Record<TemplatePageType, number | null>> | undefined;
  focusedPageType: TemplatePageType;
  selectedSlideIndex: number | null;
  onSelectPageType: (pt: TemplatePageType) => void;
  onAssignPageType: (pt: TemplatePageType, slideIndex: number) => void;
  onReviewPageTypeClick: (pt: TemplatePageType) => void;
  slides: TemplateImportSlide[];
}

function BottomRail({
  mode,
  preview,
  importPreview,
  draftPageSelections,
  focusedPageType,
  selectedSlideIndex,
  onSelectPageType,
  onAssignPageType,
  onReviewPageTypeClick,
  slides,
}: BottomRailProps) {
  const { t } = useLocale();
  const [selectionMenu, setSelectionMenu] = useState<TemplatePageType | null>(null);
  const [selectionMenuPos, setSelectionMenuPos] = useState<{ left: number; top: number; maxHeight: number } | null>(null);
  const menuPositionFromRect = (rect: DOMRect) => {
    const width = 160;
    const margin = 8;
    const maxHeight = Math.min(420, Math.max(180, window.innerHeight - margin * 2));
    const belowTop = rect.bottom + 6;
    const top = belowTop + maxHeight > window.innerHeight - margin
      ? Math.max(margin, rect.top - maxHeight - 6)
      : belowTop;
    return {
      left: Math.min(Math.max(margin, rect.right - width), window.innerWidth - width - margin),
      top,
      maxHeight: Math.min(maxHeight, window.innerHeight - top - margin),
    };
  };
  const openSelectionMenu = (pageType: TemplatePageType, event: ReactMouseEvent<HTMLElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    setSelectionMenu((current) => (current === pageType ? null : pageType));
    setSelectionMenuPos(menuPositionFromRect(rect));
  };
  return (
    <section className="templates-bottom-rail" role="tablist" aria-label={t("templates.preview.header")}>
      {PAGE_TYPES.map((pt) => {
        const label = t(`templates.preview.tilelabel.${pt}`);

        if (mode === "review") {
          const assignedSlide = draftPageSelections?.[pt];
          const isAssigned = typeof assignedSlide === "number";
          const isActive = isAssigned && assignedSlide === selectedSlideIndex;
          const svg =
            isAssigned && importPreview ? pickPreviewSvg(importPreview, pt) : undefined;
          const mappedChip = isAssigned
            ? t("templates.chip.assignedToPage").replace("{n}", String(assignedSlide))
            : t("templates.chip.notAssigned");
          return (
            <button
              key={pt}
              type="button"
              role="tab"
              aria-selected={isActive}
              onClick={() => onReviewPageTypeClick(pt)}
              className={`ti-focusable templates-bottom-tile ${
                isActive ? "templates-bottom-tile-active" : ""
              } ${!isAssigned ? "templates-bottom-tile-empty" : ""}`}
            >
              <div className="templates-bottom-thumb">
                {svg ? (
                  <div dangerouslySetInnerHTML={{ __html: sanitizeSvg(svg) }} />
                ) : null}
                {slides.length > 0 ? (
                  <HoverTooltip content={t("templates.chip.chooseReference")} className="templates-bottom-edit-tooltip-trigger">
                    <span
                      role="button"
                      tabIndex={0}
                      className="templates-bottom-edit"
                      onClick={(event) => {
                        event.stopPropagation();
                        openSelectionMenu(pt, event);
                      }}
                      onKeyDown={(event) => {
                        if (event.key !== "Enter" && event.key !== " ") return;
                        event.preventDefault();
                        event.stopPropagation();
                        const rect = event.currentTarget.getBoundingClientRect();
                        setSelectionMenu((current) => (current === pt ? null : pt));
                        setSelectionMenuPos(menuPositionFromRect(rect));
                      }}
                    >
                      <Pencil size={12} />
                    </span>
                  </HoverTooltip>
                ) : null}
                {selectionMenu === pt && selectionMenuPos ? createPortal(
                  <div
                    className="templates-bottom-page-menu"
                    style={{
                      left: selectionMenuPos.left,
                      top: selectionMenuPos.top,
                      maxHeight: selectionMenuPos.maxHeight,
                    }}
                    onClick={(event) => event.stopPropagation()}
                  >
                    <strong>{t("templates.chip.chooseReference")}</strong>
                    <div>
                      {slides.map((slide) => (
                        <button
                          key={slide.index}
                          type="button"
                          data-active={assignedSlide === slide.index ? "true" : "false"}
                          onClick={() => {
                            onAssignPageType(pt, slide.index);
                            setSelectionMenu(null);
                            setSelectionMenuPos(null);
                          }}
                        >
                          {t("templates.chip.assignedToPage").replace("{n}", String(slide.index))}
                        </button>
                      ))}
                    </div>
                  </div>,
                  document.body,
                ) : null}
              </div>
              <span className="templates-bottom-label">
                <span>{label}</span>
                <span className="templates-bottom-mapped">{mappedChip}</span>
              </span>
            </button>
          );
        }

        if (mode === "browsing") {
          const isActive = focusedPageType === pt;
          const svg = preview ? pickPreviewSvg(preview, pt) : undefined;
          return (
            <button
              key={pt}
              type="button"
              role="tab"
              aria-selected={isActive}
              onClick={() => onSelectPageType(pt)}
              className={`ti-focusable templates-bottom-tile ${
                isActive ? "templates-bottom-tile-active" : ""
              } ${!svg ? "templates-bottom-tile-empty" : ""}`}
            >
              <div className="templates-bottom-thumb">
                {svg ? (
                  <div dangerouslySetInnerHTML={{ __html: sanitizeSvg(svg) }} />
                ) : null}
              </div>
              <span className="templates-bottom-label">
                <span>{label}</span>
              </span>
            </button>
          );
        }

        // importing or empty: 5 muted disabled placeholders.
        return (
          <button
            key={pt}
            type="button"
            role="tab"
            aria-selected={false}
            disabled
            className="ti-focusable templates-bottom-tile templates-bottom-tile-empty"
          >
            <div className="templates-bottom-thumb" />
            <span className="templates-bottom-label">
              <span>{label}</span>
            </span>
          </button>
        );
      })}
    </section>
  );
}

function formatPageSelectionChanges(
  changes: PageSelectionChange[],
  t: (key: string) => string,
): string {
  if (changes.length === 0) return "";
  const lines = changes.map((change) => {
    const label = t(`templates.preview.tilelabel.${change.pageType}`);
    const from = change.from ? String(change.from) : t("templates.chip.notAssigned");
    return `- ${label}: ${from} -> ${change.to}`;
  });
  return [
    "Page selection changes since the previous Agent message:",
    ...lines,
    "Use these latest role assignments as the source of truth for inspection or templateization.",
  ].join("\n");
}
