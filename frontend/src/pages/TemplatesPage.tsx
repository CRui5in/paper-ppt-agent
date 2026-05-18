import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import {
  Bot,
  Check,
  Inbox,
  Layers,
  Library,
  Loader2,
  MessageSquareText,
  Pencil,
  Sparkles,
  Trash2,
  Upload,
  Wand2,
  X as XIcon,
} from "lucide-react";

import { Layout } from "../components/layout/Layout";
import { useLocale } from "../i18n";
import { useTemplateImport } from "../hooks/useTemplateImport";
import {
  deleteTemplate,
  fetchTemplatePreview,
  fetchTemplates,
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
  PreviewSlide,
  SlideDocument,
  UserAnnotation,
} from "../lib/types";
import { ProgressView } from "../components/template/ProgressView";
import { AgentImportingView } from "../components/template/AgentImportingView";
import { SlideStage, SlideStageToolbar, type StageMode } from "../components/template/SlideStage";
import { KonvaSlideEditor, type EditorCommand, type EditorCommandType, type EditorState } from "../components/preview/KonvaSlideEditor";
import { CollabPanel } from "../components/template/CollabPanel";
import { buildAgentActivityEvents } from "../components/template/agentActivity";
import {
  BigPreview,
  MiddleEmptyState,
} from "../components/template/MiddlePagePreview";
import { detectUserLanguage } from "../components/template/detectUserLanguage";

const ROUTING_PROFILE_STORAGE_KEY = "paper-ppt-agent-routing-profiles-v1";
const TEMPLATE_AGENT_CONFIG_STORAGE_KEY = "paper-ppt-agent-template-agent-config-v1";
const ACTIVE_TEMPLATE_IMPORT_STORAGE_KEY = "paper-ppt-agent-active-template-import-v1";
const TEMPLATE_UPLOAD_MODE_STORAGE_KEY = "paper-ppt-agent-template-upload-mode-v1";

type PageSelectionChange = {
  pageType: TemplatePageType;
  from: number | null;
  to: number;
};

interface RoutingProfile {
  model: string;
  baseUrl: string;
  apiKey: string;
  deepseekSettings?: DeepSeekSettings;
  openaiSettings?: OpenAISettings;
}

type RoutingProfileMap = Record<string, RoutingProfile>;

const PAGE_TYPES: TemplatePageType[] = ["cover", "toc", "chapter", "content", "ending"];

type LibraryFilter = "all" | "builtin" | "user";
type CollabMode = "classic" | "agent";

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
      if (parsed && (parsed.mode === "custom" || parsed.mode === "claude_code")) {
        return {
          mode: parsed.mode,
          api_key: parsed.api_key ?? "",
          auth_token: parsed.auth_token ?? "",
          base_url: parsed.base_url ?? "",
          model: parsed.model ?? "",
          custom_model_option: parsed.custom_model_option ?? "",
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
    return value === "agent" ? "agent" : "classic";
  } catch {
    return "classic";
  }
}

function writeTemplateUploadMode(mode: CollabMode): void {
  try {
    window.localStorage.setItem(TEMPLATE_UPLOAD_MODE_STORAGE_KEY, mode);
  } catch {
    /* noop */
  }
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
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "")
    .replace(/\son\w+\s*=\s*"[^"]*"/gi, "")
    .replace(/\son\w+\s*=\s*'[^']*'/gi, "")
    .replace(/javascript:/gi, "");
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

export function TemplatesPage() {
  const { t, locale } = useLocale();
  const navigate = useNavigate();

  // ── Library state ─────────────────────────────────────────────────────
  const [templates, setTemplates] = useState<TemplateInfo[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(false);
  const [libraryError, setLibraryError] = useState<string | null>(null);
  const [filter, setFilter] = useState<LibraryFilter>("all");
  const [selectedTemplateId, setSelectedTemplateId] = useState<string | null>(null);
  const [preview, setPreview] = useState<TemplatePreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [focusedPageType, setFocusedPageType] = useState<TemplatePageType>("cover");

  // ── Import state ──────────────────────────────────────────────────────
  const [modelConfig] = useState<TemplateImportModelConfig | undefined>(readModelConfig);
  const [importId, setImportId] = useState<string | undefined>(readActiveTemplateImportId);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [confirmingFlag, setConfirmingFlag] = useState(false);
  const [autoSelectedFor, setAutoSelectedFor] = useState<string | null>(null);
  const [stageMode, setStageMode] = useState<StageMode>("select");
  const [stageShowTemplated, setStageShowTemplated] = useState<boolean>(false);
  const [templateEditorCommand, setTemplateEditorCommand] = useState<EditorCommand | undefined>(undefined);
  const [templateEditorState, setTemplateEditorState] = useState<EditorState>({
    autoSave: true,
    saveState: "idle",
    canEdit: true,
    canUndo: false,
    canRedo: false,
  });
  const [selectedSlideIndex, setSelectedSlideIndex] = useState<number | null>(null);
  const [slideSvgByIndex, setSlideSvgByIndex] = useState<Record<number, string>>({});
  const [pendingAgentSelectionChanges, setPendingAgentSelectionChanges] = useState<PageSelectionChange[]>([]);
  const selectionBaselineRef = useRef<Partial<Record<TemplatePageType, number | null>>>({});
  const [uploadMode, setUploadMode] = useState<CollabMode>(readTemplateUploadMode);
  const [collabMode, setCollabMode] = useState<CollabMode>(readTemplateUploadMode);
  const [agentConfig, setAgentConfig] = useState<TemplateAgentConfig>(readTemplateAgentConfig);

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
    retryStep,
    saveAgentTemplateSvg,
  } = useTemplateImport(importId, { modelConfig, onMissingImport: handleMissingImport });

  const modelConfigured = Boolean(modelConfig?.api_key && modelConfig?.model);
  const agentConfigured =
    agentConfig.mode === "claude_code" ||
    Boolean((agentConfig.api_key || agentConfig.auth_token) && agentConfig.model);

  useEffect(() => {
    writeActiveTemplateImportId(importId);
  }, [importId]);

  useEffect(() => {
    writeTemplateUploadMode(uploadMode);
    if (!importId) {
      setCollabMode(uploadMode);
    }
  }, [importId, uploadMode]);

  useEffect(() => {
    if (status?.collaboration_mode === "classic" || status?.collaboration_mode === "agent") {
      setCollabMode(status.collaboration_mode);
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

  // ── Load library ──────────────────────────────────────────────────────
  const loadTemplates = useCallback(async () => {
    setTemplatesLoading(true);
    setLibraryError(null);
    try {
      setTemplates(await fetchTemplates());
    } catch (err) {
      setLibraryError(err instanceof Error ? err.message : "Failed to load templates");
    } finally {
      setTemplatesLoading(false);
    }
  }, []);

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
          setLibraryError(err instanceof Error ? err.message : "Preview failed");
        }
      })
      .finally(() => {
        if (!cancelled) setPreviewLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedTemplateId]);

  // ── Auto-select after import completes ───────────────────────────────
  useEffect(() => {
    if (!status) return;
    if (status.status !== "complete") return;
    const tid = status.template_id;
    if (!tid || autoSelectedFor === tid) return;
    setAutoSelectedFor(tid);
    setImportId(undefined);
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
      setUploadError(null);
      if (uploadMode === "classic" && !modelConfigured) {
        setUploadError(t("template.modelRequired"));
        return;
      }
      if (uploadMode === "agent" && !agentConfigured) {
        setUploadError("Agent mode needs Claude Code config or a custom endpoint.");
        return;
      }
      try {
        const id = await upload(
          file,
          uploadMode,
          uploadMode === "classic" ? modelConfig as TemplateImportModelConfig : undefined,
        );
        writeActiveTemplateImportId(id);
        setImportId(id);
        setCollabMode(uploadMode);
        setSelectedTemplateId(null);
        setSelectedSlideIndex(null);
      } catch (err) {
        setUploadError(err instanceof Error ? err.message : "Upload failed");
      }
    },
    [agentConfigured, modelConfigured, modelConfig, upload, uploadMode, t],
  );

  const handleConfirm = useCallback(async () => {
    setConfirmingFlag(true);
    try {
      await confirm();
    } finally {
      setConfirmingFlag(false);
    }
  }, [confirm]);

  const handleCancelImport = useCallback(() => {
    writeActiveTemplateImportId(undefined);
    setImportId(undefined);
    setCollabMode(uploadMode);
    setAutoSelectedFor(null);
    setSelectedSlideIndex(null);
    setPendingAgentSelectionChanges([]);
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
      setStageShowTemplated(true);
      setStageMode("select");
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
        setStageShowTemplated(true);
        setStageMode("select");
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
        setLibraryError(err instanceof Error ? err.message : "Delete failed");
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
        setLibraryError(err instanceof Error ? err.message : "Rename failed");
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

  // Agent mode starts with a real read-only inspection: the Agent checks the
  // review workspace and asks the user whether to begin template planning.
  // It must not edit review.json or generate placeholders until the user
  // sends an explicit instruction in the chat.
  const autoAgentInspectionRef = useRef<string | null>(null);
  useEffect(() => {
    if (collabMode !== "agent") return;
    if (!importId) return;
    if (autoAgentInspectionRef.current === importId) return;
    if (importStatus !== "review_required") return;
    if (!review) return;
    if (!agentConfigured) return;
    const existing = (review.conversation ?? []).filter((m) => {
      const meta = (m.meta ?? {}) as Record<string, unknown>;
      return meta.mode === "agent" || Boolean(meta.agent_job_id);
    });
    if (existing.length > 0) return;
    autoAgentInspectionRef.current = importId;
    const seed =
      locale === "zh"
        ? "请先只读检查当前模板导入工作区状态：阅读 agent_context.json 和 agent_task.json，说明五个页面的选择、当前准备状态和是否可以开始模板化。不要编辑 review.json，不要制作占位内容，不要标记 llm 完成。最后询问用户是否开始，以及是否有补充要求。"
        : "First perform a read-only inspection of the current template-import workspace: read agent_context.json and agent_task.json, then explain the five page selections, current preparation state, and whether template editing can start. Do not edit review.json, do not create placeholders, and do not mark llm complete. End by asking whether to start and whether the user has extra requirements.";
    void runAgent(seed, { ...agentConfig, reply_language: locale }, {
      silent: true,
      preview: false,
      planning: false,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [collabMode, importId, importStatus, Boolean(review)]);

  const slide = review?.slides.find((s) => s.index === selectedSlideIndex) ?? review?.slides[0] ?? null;
  const slidePreviewKey = useMemo(
    () =>
      (review?.slides ?? [])
        .map((s) => `${s.index}:${s.preview_svg_url ?? ""}:${s.preview_svg ? "inline" : ""}`)
        .join("|"),
    [review?.slides],
  );

  useEffect(() => {
    if (!review || !importId) {
      setSlideSvgByIndex({});
      return;
    }
    let cancelled = false;
    const inlineMap: Record<number, string> = {};
    review.slides.forEach((s) => {
      if (s.preview_svg) inlineMap[s.index] = s.preview_svg;
    });
    setSlideSvgByIndex(inlineMap);
    const toFetch = review.slides.filter((s) => s.preview_svg_url && !s.preview_svg);
    if (toFetch.length === 0) return () => {
      cancelled = true;
    };
    void Promise.all(
      toFetch.map(async (s): Promise<[number, string] | null> => {
        try {
          const res = await fetch(s.preview_svg_url as string);
          if (!res.ok) {
            console.warn(`Slide preview fetch failed: ${s.preview_svg_url} (${res.status})`);
            return null;
          }
          return [s.index, await res.text()];
        } catch (error) {
          console.warn(`Slide preview fetch failed: ${s.preview_svg_url}`, error);
          return null;
        }
      }),
    ).then((items) => {
      if (cancelled) return;
      setSlideSvgByIndex((prev) => {
        const next = { ...prev };
        items.forEach((item) => {
          if (item) next[item[0]] = item[1];
        });
        return next;
      });
    });
    return () => {
      cancelled = true;
    };
  }, [importId, review, slidePreviewKey]);

  const slideForStage = useMemo<TemplateImportSlide | null>(
    () =>
      slide
        ? {
            ...slide,
            preview_svg: slideSvgByIndex[slide.index] ?? slide.preview_svg ?? "",
          }
        : null,
    [slide, slideSvgByIndex],
  );
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

  const templatedSvg = useMemo<string | null>(() => {
    if (!slideForStage || !importPreview) return null;
    const selections = draft.page_selections ?? {};
    let mappedPt: TemplatePageType | null = null;
    for (const pt of PAGE_TYPES) {
      if (selections[pt] === slideForStage.index) {
        mappedPt = pt;
        break;
      }
    }
    const pt = mappedPt ?? slideForStage.page_type;
    return pickPreviewSvg(importPreview, pt) ?? null;
  }, [draft.page_selections, importPreview, slideForStage]);

  const templatedPageType = useMemo<TemplatePageType | null>(() => {
    if (!slideForStage) return null;
    const selections = draft.page_selections ?? {};
    for (const pt of PAGE_TYPES) {
      if (selections[pt] === slideForStage.index) return pt;
    }
    return slideForStage.page_type ?? null;
  }, [draft.page_selections, slideForStage]);

  const templateEditorSlide = useMemo<PreviewSlide | undefined>(() => {
    if (!slideForStage || !templatedSvg || !templatedPageType) return undefined;
    return {
      index: slideForStage.index,
      name: t(`template.page.${templatedPageType}`),
      source: "template-import",
      content: templatedSvg,
      document: null,
    };
  }, [slideForStage, t, templatedPageType, templatedSvg]);

  useEffect(() => {
    if (stageMode === "edit") {
      setStageShowTemplated(true);
    }
  }, [stageMode]);

  const runTemplateEditorCommand = useCallback((type: EditorCommandType) => {
    setTemplateEditorCommand({ type, id: Date.now() });
  }, []);

  const handleSaveTemplateSlide = useCallback(
    async (_slide: PreviewSlide, content: string, _document: SlideDocument) => {
      if (!templatedPageType) return;
      await saveAgentTemplateSvg(templatedPageType, content);
    },
    [saveAgentTemplateSvg, templatedPageType],
  );

  // Confirm import gating — require cover + content per design.
  const canConfirm =
    isReviewing &&
    Boolean(review) &&
    Boolean(draft.page_selections?.cover) &&
    Boolean(draft.page_selections?.content) &&
    (collabMode !== "agent" || (review?.llm?.agent === true && review?.llm?.status === "complete")) &&
    !confirmingFlag;

  const confirmDisabledHint =
    isReviewing && !canConfirm ? t("template.confirmDisabledHint") : "";

  const collabConversation = useMemo(() => {
    const conversation = review?.conversation ?? [];
    const filtered = conversation.filter((message) => {
      const meta = message.meta ?? {};
      const isAgentMessage = meta.mode === "agent" || Boolean(meta.agent_job_id);
      return collabMode === "agent" ? isAgentMessage : !isAgentMessage;
    });
    return filtered;
  }, [collabMode, review, review?.conversation]);

  const collabActivityEvents = useMemo(
    () =>
      buildAgentActivityEvents(status, review, draft, {
        mode: collabMode,
        agentEvents,
        llmEvents,
      }),
    [status, review, draft, collabMode, agentEvents, llmEvents],
  );

  return (
    <Layout showSidebar={false} contentClassName="studio-page templates-workspace-page">
      <section className="templates-workspace ti-surface">
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
              uploading={importLoading && !importId}
              mode={uploadMode}
              onModeChange={setUploadMode}
              modelConfigured={modelConfigured}
              agentConfigured={agentConfigured}
              error={uploadError ?? importError}
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
          <div className="slide-workspace-header">
            <p>
              <span>
                {isReviewing
                  ? t("template.reviewTemplate")
                  : isImporting
                    ? t("templates.previewHeader")
                    : selectedTemplate
                      ? selectedTemplate.label || selectedTemplate.template_id
                      : t("templates.previewHeader")}
              </span>
            </p>
            {isReviewing ? (
              <div className="templates-export-menu">
                {confirmDisabledHint ? (
                  <span className="templates-confirm-hint" title={confirmDisabledHint}>
                    {confirmDisabledHint}
                  </span>
                ) : null}
                <button
                  type="button"
                  onClick={handleCancelImport}
                  className="ti-focusable inline-flex items-center gap-1 rounded-[var(--ti-radius-sm,6px)] border px-2.5 py-1 text-xs font-semibold"
                  style={{
                    borderColor: "var(--ti-line)",
                    background: "var(--ti-surface)",
                    color: "var(--ti-muted)",
                    minHeight: 34,
                    padding: "0 14px",
                    gap: 8,
                  }}
                >
                  <XIcon size={12} />
                  {t("common.cancel")}
                </button>
                <button
                  type="button"
                  className="result-export-main"
                  disabled={!canConfirm}
                  onClick={() => void handleConfirm()}
                  title={confirmDisabledHint || undefined}
                >
                  {confirmingFlag ? (
                    <Loader2 size={16} className="animate-spin" />
                  ) : (
                    <Check size={16} />
                  )}
                  <span>{t("template.confirmImport")}</span>
                </button>
              </div>
            ) : selectedTemplate && selectedTemplate.editable ? (
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
            ) : selectedTemplate ? (
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
            ) : null}
          </div>

          <div className="slide-stage templates-slide-stage-grid">
            {isReviewing && review && slideForStage ? (
              <SlideStageToolbar
                className="templates-slide-toolbar"
                mode={stageMode}
                onModeChange={setStageMode}
                showTemplated={stageShowTemplated}
                onShowTemplatedChange={setStageShowTemplated}
                templatedAvailable={Boolean(templatedSvg)}
                editorState={templateEditorState}
                onEditorCommand={runTemplateEditorCommand}
              />
            ) : null}
            <aside
              className="thumbnail-rail templates-vertical-rail"
              aria-label={t("templates.thumbnailRail.empty")}
            >
              {isReviewing && review && review.slides.length > 0 ? (
                review.slides.map((s) => (
                  <SlideThumb
                    key={s.index}
                    index={s.index}
                    svg={slideSvgByIndex[s.index] ?? s.preview_svg}
                    active={selectedSlideIndex === s.index}
                    onClick={() => {
                      setStageMode("select");
                      setSelectedSlideIndex(s.index);
                    }}
                  />
                ))
              ) : !isImporting && selectedTemplate && preview ? (
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
                {isReviewing && review && slideForStage && stageMode === "edit" && templateEditorSlide ? (
                  <KonvaSlideEditor
                    slide={templateEditorSlide}
                    editable
                    command={templateEditorCommand}
                    onStateChange={setTemplateEditorState}
                    onSave={handleSaveTemplateSlide}
                  />
                ) : isReviewing && review && slideForStage ? (
                  <SlideStage
                    slide={slideForStage}
                    templatedSvg={templatedSvg}
                    annotations={activeAnnotations}
                    mode={stageMode}
                    onModeChange={setStageMode}
                    toolbarHidden
                    showTemplated={stageShowTemplated}
                    onShowTemplatedChange={setStageShowTemplated}
                    editorState={templateEditorState}
                    onEditorCommand={runTemplateEditorCommand}
                    onCreateAnnotation={(bbox_norm, note, linkedElementId) => {
                      updateDraft({
                        annotations: [
                          ...annotations,
                          {
                            annotation_id: `pending-${Date.now()}`,
                            slide_index: slideForStage.index,
                            bbox_norm,
                            note,
                            linked_element_id: linkedElementId ?? null,
                            created_at: Date.now() / 1000,
                            resolved: false,
                          },
                        ],
                      });
                    }}
                    onUpdateAnnotation={(id, patch) => {
                      updateDraft({
                        annotations: annotations.map((a) =>
                          a.annotation_id === id ? { ...a, ...patch } : a,
                        ),
                      });
                    }}
                    onDeleteAnnotation={(id) => {
                      updateDraft({
                        annotations: annotations.filter((a) => a.annotation_id !== id),
                      });
                    }}
                    slideCount={review.slide_count ?? review.slides.length}
                    className="templates-slide-stage flex-1"
                  />
                ) : isImporting ? (
                  <div className="templates-stage-importing">
                    <div>
                      {collabMode === "agent" ? (
                        <AgentImportingView
                          message={status?.message || t("template.uploading")}
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
                            onRetry={(stepId) => void retryStep(stepId)}
                          />
                          <div className="mt-3 flex justify-end">
                            <button
                              type="button"
                              onClick={handleCancelImport}
                              className="ti-focusable inline-flex items-center gap-1 rounded-[var(--ti-radius-sm,6px)] border px-3 py-1.5 text-sm"
                              style={{
                                borderColor: "var(--ti-line)",
                                background: "var(--ti-surface)",
                                color: "var(--ti-text)",
                              }}
                            >
                              <XIcon size={13} />
                              {t("common.cancel")}
                            </button>
                          </div>
                        </>
                      )}
                    </div>
                  </div>
                ) : selectedTemplate && preview ? (
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
                ) : (
                  <MiddleEmptyState />
                )}
              </div>
              {/* 5-page rail belongs only under the canvas in this layout
               * (left rail spans full panel height). */}
              <BottomRail
                mode={
                  isReviewing
                    ? "review"
                    : isImporting
                      ? "importing"
                      : selectedTemplate && preview
                        ? "browsing"
                        : "empty"
                }
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
                agentStatus={agentStatus}
                onSendFeedback={async (text) => {
                  if (collabMode === "agent") {
                    const selectionNote = formatPageSelectionChanges(pendingAgentSelectionChanges, t);
                    await runAgent(
                      selectionNote ? `${text}\n\n${selectionNote}` : text,
                      { ...agentConfig, reply_language: locale },
                    );
                    if (selectionNote) setPendingAgentSelectionChanges([]);
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
                    ? agentConfig.model || agentConfig.custom_model_option || "Claude Code"
                    : modelConfig?.model
                }
                agentCancelPending={agentCancelPending}
              />
            </div>
          </div>
        </aside>
      </section>
    </Layout>
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
  error: string | null;
  compact?: boolean;
}

function UploadCard({
  onUpload,
  uploading,
  mode,
  onModeChange,
  modelConfigured,
  agentConfigured,
  error,
  compact,
}: UploadCardProps) {
  const { t } = useLocale();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const ready = mode === "agent" ? agentConfigured : modelConfigured;

  const handleFile = async (file: File) => {
    const isPptx =
      file.name.toLowerCase().endsWith(".pptx") ||
      file.type ===
        "application/vnd.openxmlformats-officedocument.presentationml.presentation";
    if (!isPptx) return;
    await onUpload(file);
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
    if (file) void handleFile(file);
  };

  return (
    <div className="flex flex-col gap-2">
      <div
        role="button"
        tabIndex={0}
        aria-busy={uploading}
        onClick={() => !uploading && inputRef.current?.click()}
        onKeyDown={(e) => {
          if ((e.key === "Enter" || e.key === " ") && !uploading) {
            e.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        className={`ti-focusable group flex cursor-pointer flex-col items-center justify-center gap-2 rounded-[var(--ti-radius-lg,14px)] border-2 border-dashed text-center transition ${compact ? "p-4" : "p-7"}`}
        style={{
          borderColor: dragging ? "var(--ti-accent)" : "var(--ti-line)",
          background: dragging
            ? "color-mix(in srgb, var(--ti-accent) 10%, transparent)"
            : "var(--ti-surface-inset)",
          color: "var(--ti-text)",
        }}
      >
        <div
          className="ti-upload-mode-switch"
          onClick={(event) => event.stopPropagation()}
          role="radiogroup"
          aria-label="Template import mode"
        >
          {(["classic", "agent"] as const).map((item) => {
            const active = mode === item;
            return (
              <button
                key={item}
                type="button"
                disabled={uploading}
                onClick={() => onModeChange(item)}
                className="ti-focusable ti-upload-mode-button"
                data-active={active}
                aria-checked={active}
                role="radio"
              >
                {item === "agent" ? <Bot size={12} /> : <MessageSquareText size={12} />}
                <span>{item === "agent" ? "Agent" : "LLM"}</span>
              </button>
            );
          })}
        </div>
        <span
          className="flex h-10 w-10 items-center justify-center rounded-full"
          style={{
            background: "color-mix(in srgb, var(--ti-accent) 14%, transparent)",
            color: "var(--ti-accent)",
          }}
        >
          {uploading ? <Loader2 size={18} className="animate-spin" /> : <Upload size={18} />}
        </span>
        <strong className="text-sm">{t("templates.upload.title")}</strong>
        <span className="text-xs" style={{ color: "var(--ti-muted)" }}>
          {t("templates.upload.hint")}
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
              ? "Agent mode needs Claude Code config or a custom endpoint."
              : t("template.modelRequired")}
          </span>
        </div>
      ) : null}
      {error ? (
        <div
          role="alert"
          className="rounded-[var(--ti-radius-sm,6px)] border px-2 py-1.5 text-xs"
          style={{
            borderColor: "color-mix(in srgb, var(--ti-danger) 50%, var(--ti-line))",
            background: "color-mix(in srgb, var(--ti-danger) 10%, transparent)",
            color: "var(--ti-danger)",
          }}
        >
          {error}
        </div>
      ) : null}
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
        <div className="mt-0.5 flex items-center gap-1">
          <span
            className="rounded-full px-1.5 py-0.5 text-[9px] font-semibold uppercase"
            style={{
              background: isUser
                ? "color-mix(in srgb, var(--ti-success) 14%, transparent)"
                : "color-mix(in srgb, var(--ti-muted) 12%, transparent)",
              color: isUser ? "var(--ti-success)" : "var(--ti-muted)",
            }}
          >
            {isUser ? t("templates.badge.user") : t("templates.badge.builtin")}
          </span>
          {template.slide_count ? (
            <span className="text-[10px]" style={{ color: "var(--ti-muted)" }}>
              {template.slide_count} {t("template.slideCount")}
            </span>
          ) : null}
        </div>
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
  active,
  onClick,
}: {
  index: number;
  svg: string | undefined;
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
        {svg ? (
          <div dangerouslySetInnerHTML={{ __html: sanitizeSvg(svg) }} />
        ) : null}
      </div>
    </button>
  );
}

function EmptySlideThumb() {
  return (
    <div className="rail-slide rail-slide-empty" aria-hidden="true">
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
                  <span
                    role="button"
                    tabIndex={0}
                    className="templates-bottom-edit"
                    title={t("templates.chip.chooseReference")}
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
    "Please refresh agent_template/source_map.json and the related agent_template SVG baseline for any changed page type before applying this request.",
  ].join("\n");
}
