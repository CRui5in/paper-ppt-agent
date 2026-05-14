import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  Bot,
  BarChart3,
  BringToFront,
  ChevronDown,
  CircleCheck,
  Copy,
  Database,
  FileText,
  Image,
  ImagePlus,
  Info,
  Layers,
  LoaderCircle,
  Link as LinkIcon,
  MessageSquareText,
  MousePointer2,
  Play,
  Plus,
  Redo2,
  Save,
  SendToBack,
  Settings2,
  Sparkles,
  Square,
  Table2,
  Trash2,
  Type,
  Undo2,
  Wand2,
  X,
} from "lucide-react";
import { Layout } from "../components/layout/Layout";
import { HoverTooltip } from "../components/common/HoverTooltip";
import { ModelSelector } from "../components/config/ModelSelector";
import { OptionsPanel } from "../components/config/OptionsPanel";
import { AgentLog } from "../components/progress/AgentLog";
import { FloatingInspector } from "../components/progress/FloatingInspector";
import { inferActiveStage, PROGRESS_STAGES, ProgressPanel } from "../components/progress/ProgressPanel";
import { UploadZone } from "../components/upload/UploadZone";
import { RecentTasksPanel } from "../components/history/RecentTasksPanel";
import { ImageSearchPanel } from "../components/result/ImageSearchPanel";
import { useGeneration } from "../hooks/useGeneration";
import { useLocale } from "../i18n";
import { fetchTemplates } from "../lib/api";
import type {
  DeepSeekSettings,
  GenerationHistoryItem,
  JobStatus,
  OpenAISettings,
  PreviewSlide,
  CriticEvent,
  ResearchConfig,
  ResearchEnrichmentStats,
  TemplateInfo,
  UploadResponse,
} from "../lib/types";
import { TemplateManager } from "../components/template/TemplateManager";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Progress } from "../components/ui/progress";
import { Tabs, TabsList, TabsTrigger } from "../components/ui/tabs";
import { translateJobMessage, translateStageStatus } from "../lib/i18nStatus";

const ROUTING_PROFILE_STORAGE_KEY = "paper-ppt-agent-routing-profiles-v1";
const PRESENTATION_SETTINGS_STORAGE_KEY = "paper-ppt-agent-presentation-settings-v1";
type LanguageMode = "zh" | "en" | "custom";
type SecondaryPanel = "log" | "critic";
const DEFAULT_DEEPSEEK_SETTINGS: DeepSeekSettings = {
  thinking_enabled: true,
  reasoning_effort: "max",
};
const DEFAULT_OPENAI_SETTINGS: OpenAISettings = {
  reasoning_effort: "medium",
  verbosity: "high",
};

interface RoutingProfile {
  model: string;
  baseUrl: string;
  apiKey: string;
  deepseekSettings?: DeepSeekSettings;
  openaiSettings?: OpenAISettings;
}

type RoutingProfileMap = Record<string, RoutingProfile>;

interface PresentationSettingsDraft {
  canvasFormat?: string;
  languageMode?: LanguageMode;
  customLanguage?: string;
  numPages?: string;
  detailLevel?: string;
  generationMode?: "sequential" | "chapter_parallel" | "page_parallel";
  parallelConcurrency?: string;
  timeoutSeconds?: string;
  maxCriticAttempts?: string;
  visualQaMaxAttempts?: string;
  instruction?: string;
  density?: string;
  customFont?: string;
  headingFont?: string;
  bodyFont?: string;
  cjkHeadingFont?: string;
  cjkBodyFont?: string;
  enableDeepResearch?: boolean;
  enableVisualCritic?: boolean;
  enableIcon?: boolean;
  enableIconRag?: boolean;
  researchConfig?: ResearchConfig;
  templateId?: string;
}

function readRoutingProfiles(): RoutingProfileMap {
  try {
    const raw = window.localStorage.getItem(ROUTING_PROFILE_STORAGE_KEY);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw) as RoutingProfileMap;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function writeRoutingProfiles(profiles: RoutingProfileMap) {
  window.localStorage.setItem(ROUTING_PROFILE_STORAGE_KEY, JSON.stringify(profiles));
}

function readPresentationSettingsDraft(): PresentationSettingsDraft {
  try {
    const raw = window.localStorage.getItem(PRESENTATION_SETTINGS_STORAGE_KEY);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw) as PresentationSettingsDraft;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function writePresentationSettingsDraft(settings: PresentationSettingsDraft) {
  window.localStorage.setItem(PRESENTATION_SETTINGS_STORAGE_KEY, JSON.stringify(settings));
}

function getProviderDefaults(
  providers: { name: string; default_base_url?: string | null }[],
  providerName: string,
) {
  const selectedProvider = providers.find((item) => item.name === providerName);
  return {
    baseUrl: selectedProvider?.default_base_url ?? "",
  };
}

export function GeneratePage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { t, locale } = useLocale();
  const {
    providers,
    uploadSession,
    jobId,
    job,
    slides,
    logs,
    criticEvents,
    enrichmentStats,
    selectedSlide,
    connectionStatus,
    error,
    currentRunConfig,
    history,
    runs,
    loadProviders,
    uploadFile,
    clearUploadSession,
    startGeneration,
    cancelCurrentRun,
    connect,
    resumeCurrentRun,
    hydrateResult,
    refreshHistoryStatuses,
    selectSlide,
    reset,
  } = useGeneration();
  const [initialSettings] = useState(readPresentationSettingsDraft);

  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [deepSeekSettings, setDeepSeekSettings] = useState<DeepSeekSettings>(
    DEFAULT_DEEPSEEK_SETTINGS,
  );
  const [openAISettings, setOpenAISettings] = useState<OpenAISettings>(
    DEFAULT_OPENAI_SETTINGS,
  );
  const [density, setDensity] = useState(initialSettings.density ?? "normal");
  const [customFont, setCustomFont] = useState(initialSettings.customFont ?? "");
  const [headingFont, setHeadingFont] = useState(initialSettings.headingFont ?? "");
  const [bodyFont, setBodyFont] = useState(initialSettings.bodyFont ?? "");
  const [cjkHeadingFont, setCjkHeadingFont] = useState(initialSettings.cjkHeadingFont ?? "");
  const [cjkBodyFont, setCjkBodyFont] = useState(initialSettings.cjkBodyFont ?? "");
  const [canvasFormat, setCanvasFormat] = useState(initialSettings.canvasFormat ?? "ppt169");
  const [languageMode, setLanguageMode] = useState<LanguageMode>(
    initialSettings.languageMode ?? (locale === "zh" ? "zh" : "en"),
  );
  const [customLanguage, setCustomLanguage] = useState(initialSettings.customLanguage ?? "");
  const [numPages, setNumPages] = useState(initialSettings.numPages ?? "");
  const [detailLevel, setDetailLevel] = useState(initialSettings.detailLevel ?? "normal");
  const [generationMode, setGenerationMode] = useState<"sequential" | "chapter_parallel" | "page_parallel">(
    initialSettings.generationMode ?? "sequential",
  );
  const [parallelConcurrency, setParallelConcurrency] = useState(initialSettings.parallelConcurrency ?? "3");
  const [timeoutSeconds, setTimeoutSeconds] = useState(initialSettings.timeoutSeconds ?? "");
  const [maxCriticAttempts, setMaxCriticAttempts] = useState(initialSettings.maxCriticAttempts ?? "0");
  const [visualQaMaxAttempts, setVisualQaMaxAttempts] = useState(initialSettings.visualQaMaxAttempts ?? "1");
  const [instruction, setInstruction] = useState(initialSettings.instruction ?? "");
  const GEMINI_KEY_STORAGE = "paper-ppt-agent-gemini-api-key";
  const RESEARCH_KEYS_STORAGE = "paper-ppt-agent-research-keys";
  const [enableDeepResearch, setEnableDeepResearch] = useState(initialSettings.enableDeepResearch ?? false);
  const [enableVisualCritic, setEnableVisualCritic] = useState(initialSettings.enableVisualCritic ?? false);
  const [enableIcon, setEnableIcon] = useState(initialSettings.enableIcon ?? false);
  const [enableIconRag, setEnableIconRag] = useState(initialSettings.enableIconRag ?? false);
  const [researchConfig, setResearchConfig] = useState<ResearchConfig>(() => {
    const base = initialSettings.researchConfig ?? {};
    try {
      const saved = window.localStorage.getItem(RESEARCH_KEYS_STORAGE);
      if (saved) {
        const parsed = JSON.parse(saved) as Record<string, string>;
        return {
          ...base,
          web_search_provider: base.web_search_provider || (parsed.web_search_provider as "tavily" | "serpapi" | undefined) || undefined,
          semantic_scholar_api_key: base.semantic_scholar_api_key || parsed.semantic_scholar_api_key || undefined,
          tavily_api_key: base.tavily_api_key || parsed.tavily_api_key || undefined,
          serpapi_key: base.serpapi_key || parsed.serpapi_key || undefined,
        };
      }
    } catch { /* noop */ }
    return base;
  });
  const [geminiApiKey, setGeminiApiKey] = useState(() => {
    try { return window.localStorage.getItem(GEMINI_KEY_STORAGE) ?? ""; } catch { return ""; }
  });
  const [templateId, setTemplateId] = useState(initialSettings.templateId ?? "");
  const [templates, setTemplates] = useState<TemplateInfo[]>([]);
  const [templateManagerOpen, setTemplateManagerOpen] = useState(false);
  const [cancelLoading, setCancelLoading] = useState(false);
  const [secondaryPanel, setSecondaryPanel] = useState<SecondaryPanel | null>(null);
  const freshRequested = searchParams.get("fresh") === "1";
  const targetJobId = searchParams.get("job") ?? undefined;
  const targetHistoryEntry = targetJobId
    ? history.find((entry) => entry.jobId === targetJobId)
    : undefined;
  const selectedRunConfig = useMemo(() => {
    if (targetJobId) {
      const targetRunConfig = runs[targetJobId]?.currentRunConfig;
      if (targetRunConfig) {
        return targetRunConfig;
      }
      if (
        targetHistoryEntry?.provider &&
        targetHistoryEntry.model &&
        targetHistoryEntry.options
      ) {
        return {
          provider: targetHistoryEntry.provider,
          model: targetHistoryEntry.model,
          baseUrl: targetHistoryEntry.baseUrl ?? undefined,
          options: targetHistoryEntry.options,
          parentJobId: targetHistoryEntry.parentJobId ?? null,
        };
      }
      return undefined;
    }
    if (currentRunConfig) {
      return currentRunConfig;
    }
    return undefined;
  }, [currentRunConfig, runs, targetHistoryEntry, targetJobId]);
  const canCancelCurrentRun = Boolean(
    jobId &&
      job &&
      !["complete", "error", "cancelled"].includes(job.status),
  );

  useEffect(() => {
    void loadProviders();
  }, [loadProviders]);

  useEffect(() => {
    let stopped = false;
    const refresh = async () => {
      if (!stopped) {
        await refreshHistoryStatuses();
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [refreshHistoryStatuses]);

  useEffect(() => {
    fetchTemplates()
      .then((list) => setTemplates(list))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (freshRequested) {
      reset();
      navigate("/generate", { replace: true });
      return;
    }

    void resumeCurrentRun(targetJobId);
  }, [freshRequested, navigate, reset, resumeCurrentRun, targetJobId]);

  useEffect(() => {
    if (!provider && providers.length > 0) {
      const defaultProvider = providers[0].name;
      const saved = readRoutingProfiles()[defaultProvider];
      const defaults = getProviderDefaults(providers, defaultProvider);
      setProvider(defaultProvider);
      setModel("");
      setBaseUrl(saved?.baseUrl || defaults.baseUrl);
      setApiKey(saved?.apiKey || "");
      setDeepSeekSettings(saved?.deepseekSettings ?? DEFAULT_DEEPSEEK_SETTINGS);
      setOpenAISettings(saved?.openaiSettings ?? DEFAULT_OPENAI_SETTINGS);
    }
  }, [provider, providers]);

  useEffect(() => {
    if (!provider) {
      return;
    }
    if (targetJobId) {
      return;
    }
    const profiles = readRoutingProfiles();
    const saved = profiles[provider];
    const defaults = getProviderDefaults(providers, provider);
    setModel(saved?.model || "");
    setBaseUrl(saved?.baseUrl || defaults.baseUrl);
    setApiKey(saved?.apiKey || "");
    setDeepSeekSettings(saved?.deepseekSettings ?? DEFAULT_DEEPSEEK_SETTINGS);
    setOpenAISettings(saved?.openaiSettings ?? DEFAULT_OPENAI_SETTINGS);
  }, [provider, providers]);

  useEffect(() => {
    if (!provider) {
      return;
    }
    if (targetJobId) {
      return;
    }
    const profiles = readRoutingProfiles();
    const existing = profiles[provider];
    profiles[provider] = {
      model: model.trim() || existing?.model || "",
      baseUrl,
      apiKey,
      deepseekSettings: provider === "deepseek" ? deepSeekSettings : existing?.deepseekSettings,
      openaiSettings: provider === "openai" ? openAISettings : existing?.openaiSettings,
    };
    writeRoutingProfiles(profiles);
  }, [apiKey, baseUrl, deepSeekSettings, model, openAISettings, provider, targetJobId]);

  useEffect(() => {
    if (!targetJobId || !selectedRunConfig) {
      return;
    }

    const options = selectedRunConfig.options;
    const savedProfile = readRoutingProfiles()[selectedRunConfig.provider];
    setProvider(selectedRunConfig.provider);
    setModel(selectedRunConfig.model);
    setBaseUrl(selectedRunConfig.baseUrl ?? "");
    setApiKey(savedProfile?.apiKey ?? "");
    setDeepSeekSettings(savedProfile?.deepseekSettings ?? DEFAULT_DEEPSEEK_SETTINGS);
    setOpenAISettings(savedProfile?.openaiSettings ?? DEFAULT_OPENAI_SETTINGS);
    setCanvasFormat(options.canvas_format || "ppt169");
    setDensity(options.style_overrides?.density ?? "normal");
    setCustomFont(options.style_overrides?.font ?? "");
    setHeadingFont(options.style_overrides?.font_heading ?? "");
    setBodyFont(options.style_overrides?.font_body ?? "");
    setCjkHeadingFont(options.style_overrides?.cjk_heading ?? "");
    setCjkBodyFont(options.style_overrides?.cjk_body ?? "");
    setCustomFont(options.style_overrides?.font ?? "");
    if (options.language === "zh" || options.language === "en") {
      setLanguageMode(options.language);
      setCustomLanguage("");
    } else {
      setLanguageMode("custom");
      setCustomLanguage(options.language || "");
    }
    setNumPages(options.num_pages ? String(options.num_pages) : "");
    setDetailLevel(options.detail_level || "normal");
    setGenerationMode(options.generation_mode || "sequential");
    setParallelConcurrency(String(options.parallel_concurrency ?? 3));
    setTimeoutSeconds(options.timeout_seconds ? String(options.timeout_seconds) : "");
    setMaxCriticAttempts(String(options.max_critic_attempts ?? 3));
    setEnableDeepResearch(Boolean(options.enable_deep_research));
    setEnableVisualCritic(Boolean(options.enable_visual_critic));
    setVisualQaMaxAttempts(String(options.visual_qa_max_attempts ?? 1));
    setEnableIcon(options.enable_icon !== false);
    setEnableIconRag(options.enable_icon_rag !== false);
    setResearchConfig((prev) => {
      const incoming = options.research_config ?? {};
      return {
        ...incoming,
        web_search_provider: incoming.web_search_provider || prev.web_search_provider,
        semantic_scholar_api_key: incoming.semantic_scholar_api_key || prev.semantic_scholar_api_key,
        tavily_api_key: incoming.tavily_api_key || prev.tavily_api_key,
        serpapi_key: incoming.serpapi_key || prev.serpapi_key,
      };
    });
    setGeminiApiKey(options.gemini_api_key ?? "");
    setTemplateId(options.template_id ?? "");
  }, [selectedRunConfig, targetJobId]);

  useEffect(() => {
    setLanguageMode((current) => (current === "custom" ? current : locale === "zh" ? "zh" : "en"));
  }, [locale]);

  useEffect(() => {
    try { window.localStorage.setItem(GEMINI_KEY_STORAGE, geminiApiKey); } catch { /* noop */ }
  }, [geminiApiKey]);

  useEffect(() => {
    try {
      const existingRaw = window.localStorage.getItem(RESEARCH_KEYS_STORAGE);
      const existing = existingRaw ? (JSON.parse(existingRaw) as Record<string, string>) : {};
      const next = {
        web_search_provider: researchConfig.web_search_provider || existing.web_search_provider || "tavily",
        semantic_scholar_api_key:
          researchConfig.semantic_scholar_api_key || existing.semantic_scholar_api_key || "",
        tavily_api_key: researchConfig.tavily_api_key || existing.tavily_api_key || "",
        serpapi_key: researchConfig.serpapi_key || existing.serpapi_key || "",
      };
      window.localStorage.setItem(RESEARCH_KEYS_STORAGE, JSON.stringify(next));
    } catch { /* noop */ }
  }, [researchConfig.web_search_provider, researchConfig.semantic_scholar_api_key, researchConfig.tavily_api_key, researchConfig.serpapi_key]);

  useEffect(() => {
    if (targetJobId) {
      return;
    }
    try {
      writePresentationSettingsDraft({
        canvasFormat,
        languageMode,
        customLanguage,
        numPages,
        detailLevel,
        generationMode,
        parallelConcurrency,
        timeoutSeconds,
        maxCriticAttempts,
        visualQaMaxAttempts,
        instruction,
        density,
        customFont,
        headingFont,
        bodyFont,
        cjkHeadingFont,
        cjkBodyFont,
        enableDeepResearch,
        enableVisualCritic,
        enableIcon,
        enableIconRag,
        researchConfig,
        templateId,
      });
    } catch {
      // Ignore storage failures; settings still work for the current session.
    }
  }, [
    canvasFormat,
    cjkBodyFont,
    cjkHeadingFont,
    customFont,
    customLanguage,
    density,
    detailLevel,
    enableDeepResearch,
    enableIcon,
    enableIconRag,
    enableVisualCritic,
    maxCriticAttempts,
    visualQaMaxAttempts,
    headingFont,
    bodyFont,
    instruction,
    languageMode,
    generationMode,
    numPages,
    parallelConcurrency,
    researchConfig,
    templateId,
    timeoutSeconds,
    targetJobId,
  ]);

  useEffect(() => {
    if (job?.status === "complete" && jobId) {
      navigate(`/result?job=${jobId}`);
    }
  }, [job?.status, jobId, navigate]);

  const launchGeneration = async () => {
    if (!uploadSession) {
      return;
    }
    const normalizedModel = model.trim();
    const profiles = readRoutingProfiles();
    profiles[provider] = {
      model: normalizedModel,
      baseUrl,
      apiKey,
      deepseekSettings: provider === "deepseek" ? deepSeekSettings : undefined,
      openaiSettings: provider === "openai" ? openAISettings : undefined,
    };
    writeRoutingProfiles(profiles);
    const nextJobId = await startGeneration({
      session_id: uploadSession.session_id,
      instruction,
      model_config: {
        provider,
        model: normalizedModel,
        api_key: apiKey,
        base_url: baseUrl || undefined,
        deepseek_settings: provider === "deepseek" ? deepSeekSettings : undefined,
        openai_settings: provider === "openai" ? openAISettings : undefined,
      },
      options: {
        canvas_format: canvasFormat,
        style: "academic",
        language: resolveRequestedLanguage(languageMode, customLanguage),
        num_pages: numPages ? Number(numPages) : undefined,
        detail_level: detailLevel,
        generation_mode: generationMode,
        parallel_concurrency: generationMode === "page_parallel"
          ? parseBoundedInt(parallelConcurrency, 3, 1, 8)
          : undefined,
        timeout_seconds: parseOptionalPositiveInt(timeoutSeconds),
        max_critic_attempts: parseBoundedInt(maxCriticAttempts, 0, 0, 10),
        style_overrides:
          customFont || headingFont || bodyFont || cjkHeadingFont || cjkBodyFont || density !== "normal"
            ? {
                font: customFont || undefined,
                font_heading: headingFont || undefined,
                font_body: bodyFont || undefined,
                cjk_heading: cjkHeadingFont || undefined,
                cjk_body: cjkBodyFont || undefined,
                density: density as "compact" | "normal" | "spacious",
              }
            : undefined,
        enable_visual_critic: enableVisualCritic,
        visual_qa_max_attempts: parseBoundedInt(visualQaMaxAttempts, 1, 0, 10),
        enable_deep_research: enableDeepResearch,
        enable_icon: enableIcon,
        enable_icon_rag: enableIconRag,
        gemini_api_key: geminiApiKey || undefined,
        template_id: templateId || undefined,
        research_config: (researchConfig.arxiv_search_enabled || researchConfig.semantic_scholar_enabled || researchConfig.web_search_enabled)
          ? researchConfig
          : undefined,
      },
    });
    connect(nextJobId);
  };

  const generationDisabled =
    !uploadSession ||
    !provider ||
    !model.trim() ||
    !apiKey ||
    (languageMode === "custom" && !customLanguage.trim()) ||
    canCancelCurrentRun;

  return (
    <Layout showSidebar={false} contentClassName="studio-page scholarly-workspace-page">
      <section className="scholarly-workspace">
        <SourcesPanel
          uploadSession={uploadSession}
          job={job}
          jobId={jobId}
          connectionStatus={connectionStatus}
          enrichmentStats={enrichmentStats}
          slideCount={slides.length}
          logs={logs}
          history={history}
          runs={runs}
          locale={locale}
          onFileSelect={(file) => void uploadFile(file)}
          onSourceRemove={() => void clearUploadSession()}
        />

        <SlideWorkspace
          jobId={jobId}
          job={job}
          slides={slides}
          selectedSlide={selectedSlide}
          onSelect={selectSlide}
          canvasFormat={canvasFormat}
          isGenerating={canCancelCurrentRun}
          loading={Boolean(targetJobId && !job && slides.length === 0)}
          onImageApplied={async (slideIndex) => {
            if (!jobId) return;
            await hydrateResult(jobId);
            const refreshed = useGeneration.getState().runs[jobId]?.slides ?? useGeneration.getState().slides;
            selectSlide(refreshed.find((slide) => slide.index === slideIndex) ?? refreshed[0]);
          }}
        />

        <aside className="configuration-panel">
          <div className="workspace-panel-header">
            <div className="workspace-panel-title">
              <Settings2 size={18} />
              <span>{t("config.title")}</span>
            </div>
          </div>
          <div className="configuration-scroll">
            <ModelSelector
                providers={providers}
                provider={provider}
                model={model}
                baseUrl={baseUrl}
                apiKey={apiKey}
                deepSeekSettings={deepSeekSettings}
                openAISettings={openAISettings}
                onProviderChange={(nextProvider) => {
                  setProvider(nextProvider);
                }}
                onModelChange={setModel}
                onBaseUrlChange={setBaseUrl}
                onApiKeyChange={setApiKey}
                onDeepSeekSettingsChange={setDeepSeekSettings}
                onOpenAISettingsChange={setOpenAISettings}
            />
            <OptionsPanel
                canvasFormat={canvasFormat}
                languageMode={languageMode}
                customLanguage={customLanguage}
                numPages={numPages}
                detailLevel={detailLevel}
                generationMode={generationMode}
                parallelConcurrency={parallelConcurrency}
                timeoutSeconds={timeoutSeconds}
                maxCriticAttempts={maxCriticAttempts}
                visualQaMaxAttempts={visualQaMaxAttempts}
                instruction={instruction}
                enableDeepResearch={enableDeepResearch}
                enableVisualCritic={enableVisualCritic}
                enableIcon={enableIcon}
                enableIconRag={enableIconRag}
                geminiApiKey={geminiApiKey}
                templateId={templateId}
                templates={templates}
                onCanvasFormatChange={setCanvasFormat}
                onLanguageModeChange={setLanguageMode}
                onCustomLanguageChange={setCustomLanguage}
                onNumPagesChange={setNumPages}
                onDetailLevelChange={setDetailLevel}
                onGenerationModeChange={setGenerationMode}
                onParallelConcurrencyChange={setParallelConcurrency}
                onTimeoutSecondsChange={setTimeoutSeconds}
                onMaxCriticAttemptsChange={setMaxCriticAttempts}
                onVisualQaMaxAttemptsChange={setVisualQaMaxAttempts}
                onInstructionChange={setInstruction}
                onEnableDeepResearchChange={setEnableDeepResearch}
                onEnableVisualCriticChange={setEnableVisualCritic}
                onEnableIconChange={setEnableIcon}
                onEnableIconRagChange={setEnableIconRag}
                onGeminiApiKeyChange={setGeminiApiKey}
                onTemplateChange={setTemplateId}
                onManageTemplates={() => setTemplateManagerOpen(true)}
                density={density}
                customFont={customFont}
                headingFont={headingFont}
                bodyFont={bodyFont}
                cjkHeadingFont={cjkHeadingFont}
                cjkBodyFont={cjkBodyFont}
                onDensityChange={setDensity}
                onCustomFontChange={setCustomFont}
                onHeadingFontChange={setHeadingFont}
                onBodyFontChange={setBodyFont}
                onCjkHeadingFontChange={setCjkHeadingFont}
                onCjkBodyFontChange={setCjkBodyFont}
                researchConfig={researchConfig}
                onResearchConfigChange={setResearchConfig}
            />
          </div>
          <div className="configuration-actions">
            <button
              type="button"
              className="primary-button full-width launch-button"
              disabled={generationDisabled}
              onClick={() => void launchGeneration()}
            >
              {canCancelCurrentRun ? <LoaderCircle size={17} className="spin" /> : <Wand2 size={17} />}
              {t("studio.launch")}
            </button>
            {canCancelCurrentRun ? (
              <button
                type="button"
                className="secondary-button danger-button full-width cancel-generation-button"
                disabled={cancelLoading || job?.status === "cancelling"}
                onClick={async () => {
                  setCancelLoading(true);
                  try {
                    await cancelCurrentRun();
                  } finally {
                    setCancelLoading(false);
                  }
                }}
              >
                {cancelLoading || job?.status === "cancelling" ? <LoaderCircle size={15} className="spin" /> : null}
                {cancelLoading || job?.status === "cancelling"
                  ? t("studio.canceling")
                  : t("studio.cancel")}
              </button>
            ) : null}
          </div>
        </aside>

        <AgentMonitor
          job={job}
          logs={logs}
          criticEvents={criticEvents}
          jobId={jobId}
          connectionStatus={connectionStatus}
          enrichmentStats={enrichmentStats}
          slideCount={slides.length}
          activePanel={secondaryPanel}
          onOpenPanel={setSecondaryPanel}
        />
      </section>
      <FloatingInspector
        open={secondaryPanel === "log"}
        title={t("log.title")}
        icon={<MessageSquareText size={15} className="panel-title-icon" />}
        onClose={() => setSecondaryPanel(null)}
      >
        <AgentLog mode="logs" hideHeader logs={logs} criticEvents={[]} jobId={jobId} />
      </FloatingInspector>
      <FloatingInspector
        open={secondaryPanel === "critic"}
        title={t("monitor.review")}
        icon={<Sparkles size={15} className="panel-title-icon" />}
        onClose={() => setSecondaryPanel(null)}
      >
        <CriticPanel criticEvents={criticEvents} jobId={jobId} />
      </FloatingInspector>
      <TemplateManager
        open={templateManagerOpen}
        onClose={() => setTemplateManagerOpen(false)}
        onSelect={(tid) => {
          setTemplateId(tid);
          // Refresh templates list to include newly imported ones
          fetchTemplates()
            .then((list) => setTemplates(list))
            .catch(() => undefined);
        }}
      />
    </Layout>
  );
}

function SourcesPanel({
  uploadSession,
  job,
  jobId,
  connectionStatus,
  enrichmentStats,
  slideCount,
  logs,
  history,
  runs,
  locale,
  onFileSelect,
  onSourceRemove,
}: {
  uploadSession?: UploadResponse;
  job?: JobStatus;
  jobId?: string;
  connectionStatus: string;
  enrichmentStats?: ResearchEnrichmentStats;
  slideCount: number;
  logs: string[];
  history: GenerationHistoryItem[];
  runs: ReturnType<typeof useGeneration.getState>["runs"];
  locale: "en" | "zh";
  onFileSelect: (file: File) => void;
  onSourceRemove: () => void;
}) {
  const { t } = useLocale();
  const sourceItems = uploadSession
    ? [
        {
          name: uploadSession.file_info.name,
          meta: `${uploadSession.file_info.source_type.toUpperCase()} · ${(uploadSession.file_info.size / 1024).toFixed(1)} KB`,
          type: uploadSession.file_info.source_type.toLowerCase(),
        },
      ]
    : [];
  const hasStartedTask = Boolean(jobId && job && job.status !== "idle");
  return (
    <Card className="sources-panel">
      <CardHeader className="workspace-panel-header">
        <div className="workspace-panel-title">
          <Database size={17} />
          <CardTitle>{t("source.title")}</CardTitle>
        </div>
      </CardHeader>
      <CardContent className="sources-content">
      {!hasStartedTask ? (
        <>
          <UploadZone onFileSelect={onFileSelect} />
          <p className="source-limit-note">{t("source.limit")}</p>
          <Tabs value="papers" className="w-full">
            <TabsList className="source-tabs grid w-full grid-cols-3">
              <TabsTrigger value="papers">{t("source.papers")} <span>{sourceItems.length}</span></TabsTrigger>
              <HoverTooltip content={t("common.pending")}><TabsTrigger value="links" disabled>{t("source.links")} <span>0</span></TabsTrigger></HoverTooltip>
              <HoverTooltip content={t("common.pending")}><TabsTrigger value="datasets" disabled>{t("source.datasets")} <span>0</span></TabsTrigger></HoverTooltip>
            </TabsList>
          </Tabs>
          <SourceList sourceItems={sourceItems} onRemove={onSourceRemove} />
          <div className="source-footer">
            <span>{sourceItems.length} / 1 source</span>
            <HoverTooltip content="Multiple links are planned for a later version"><Button variant="secondary" size="sm" type="button" disabled><LinkIcon size={13} /> Add Link</Button></HoverTooltip>
          </div>
        </>
      ) : (
        <>
          <SourceList sourceItems={sourceItems} compact />
          <div className="source-inline-process">
            <div className="panel-title-row source-inline-process-title">
              <BarChart3 size={17} className="panel-title-icon" />
              <span>{t("progress.title")}</span>
            </div>
            <ProgressPanel compact hideHeader job={job} connectionStatus={connectionStatus} enrichmentStats={enrichmentStats} slideCount={slideCount} logs={logs} />
          </div>
        </>
      )}
      <RecentTasksPanel history={history} runs={runs} currentJobId={jobId} locale={locale} />
      </CardContent>
    </Card>
  );
}

function SourceList({
  sourceItems,
  compact = false,
  onRemove,
}: {
  sourceItems: Array<{ name: string; meta: string; type: string }>;
  compact?: boolean;
  onRemove?: () => Promise<void> | void;
}) {
  const { t } = useLocale();
  return (
    <div className={`source-list ${compact ? "source-list-compact" : ""}`}>
      {sourceItems.length > 0 ? sourceItems.map((item) => (
        <div className={`source-row ${onRemove ? "source-row-removable" : ""}`} key={item.name}>
          <span className="source-row-leading">
            <span className={`source-file-type source-file-${item.type.includes("doc") ? "doc" : "pdf"}`}>
              {item.type.includes("doc") ? "DOC" : item.type.toUpperCase().slice(0, 3)}
            </span>
            {onRemove ? (
              <button
                type="button"
                className="source-remove-button"
                aria-label={t("versions.delete")}
                onClick={(event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  void onRemove();
                }}
              >
                <X size={13} />
              </button>
            ) : null}
          </span>
          <span className="source-row-copy">
            <strong>{item.name}</strong>
            <em>{item.meta}</em>
          </span>
          <CircleCheck size={15} className="source-check" />
        </div>
      )) : (
        <div className="source-empty-state">
          <FileText size={22} />
          <span>{t("source.empty")}</span>
        </div>
      )}
    </div>
  );
}

function SlideWorkspace({
  jobId,
  job,
  slides,
  selectedSlide,
  onSelect,
  canvasFormat,
  isGenerating,
  loading,
  onImageApplied,
}: {
  jobId?: string;
  job?: JobStatus;
  slides?: PreviewSlide[];
  selectedSlide?: PreviewSlide;
  onSelect: (slide: PreviewSlide) => void;
  canvasFormat: string;
  isGenerating: boolean;
  loading?: boolean;
  onImageApplied?: (slideIndex: number) => Promise<void> | void;
}) {
  const { t } = useLocale();
  const safeSlides = Array.isArray(slides) ? slides : [];
  const lastSlideIndex = safeSlides.length > 0 ? safeSlides[safeSlides.length - 1].index : 0;
  const totalSlides = Math.max(job?.total_slides ?? 0, lastSlideIndex, safeSlides.length);
  const slideByIndex = new Map(safeSlides.map((slide) => [slide.index, slide]));
  const railItems = totalSlides > 0
    ? Array.from({ length: totalSlides }, (_, index) => slideByIndex.get(index + 1) ?? index + 1)
    : [];
  const [imageSearchOpen, setImageSearchOpen] = useState(false);
  const canUseImageSearch = Boolean(jobId && selectedSlide && !loading);
  return (
    <main className="slide-workspace-panel">
      <div className="slide-workspace-header">
        <p>
          <span>{t("preview.slidePreview")}</span>
          <HoverTooltip content={t("preview.editorWarning")}><span className="preview-info-tip"><Info size={15} /></span></HoverTooltip>
        </p>
      </div>
      <div className="slide-toolbar">
        <HoverTooltip content={t("common.pending")}><button type="button" disabled><Plus size={15} /> {t("preview.newSlide")}</button></HoverTooltip>
        <span className="toolbar-divider" />
        <HoverTooltip content={t("editor.textTool")}><button type="button" disabled><Type size={15} /></button></HoverTooltip>
        <HoverTooltip content={t("editor.shapeTool")}><button type="button" disabled><Square size={15} /></button></HoverTooltip>
        <HoverTooltip content={t("editor.pictureTool")}><button type="button" disabled><Image size={15} /></button></HoverTooltip>
        <HoverTooltip content={t("imageSearch.title")}>
          <button
            type="button"
            className={imageSearchOpen ? "slide-toolbar-button-active" : undefined}
            disabled={!canUseImageSearch}
            onClick={() => setImageSearchOpen((open) => !open)}
          >
            <ImagePlus size={15} /> {t("imageSearch.shortTitle")}
          </button>
        </HoverTooltip>
        <HoverTooltip content={t("editor.tableTool")}><button type="button" disabled><Table2 size={15} /></button></HoverTooltip>
        <span className="toolbar-divider" />
        <HoverTooltip content={t("editor.undo")}><button type="button" disabled><Undo2 size={15} /></button></HoverTooltip>
        <HoverTooltip content={t("editor.redo")}><button type="button" disabled><Redo2 size={15} /></button></HoverTooltip>
        <HoverTooltip content={t("editor.duplicate")}><button type="button" disabled><Copy size={15} /></button></HoverTooltip>
        <HoverTooltip content={t("editor.delete")}><button type="button" disabled><Trash2 size={15} /></button></HoverTooltip>
        <span className="toolbar-divider" />
        <HoverTooltip content={t("editor.sendBackward")}><button type="button" disabled><SendToBack size={15} /></button></HoverTooltip>
        <HoverTooltip content={t("editor.bringForward")}><button type="button" disabled><BringToFront size={15} /></button></HoverTooltip>
        <span className="toolbar-divider" />
        <HoverTooltip content={t("editor.autosave")}><button type="button" disabled><Layers size={15} /> {t("editor.manual")}</button></HoverTooltip>
        <HoverTooltip content={t("editor.saveEdits")}><button type="button" disabled><Save size={15} /> {t("editor.save")}</button></HoverTooltip>
        <span className="toolbar-spacer" />
        <HoverTooltip content={t("preview.startSlideshow")}><button type="button" disabled><Play size={15} /> {t("preview.slideshow")}</button></HoverTooltip>
        <button type="button" disabled><MousePointer2 size={15} /> {t("editor.fit")}</button>
        <button type="button" disabled>{canvasFormat === "ppt43" ? "4:3" : "16:9"} <ChevronDown size={13} /></button>
      </div>
      <div className="slide-stage">
        <div className="thumbnail-rail">
          {loading ? Array.from({ length: 4 }).map((_, index) => (
            <button type="button" className={`rail-slide ${index === 0 ? "rail-slide-active" : ""}`} key={index} disabled>
              <span>{index + 1}</span>
              <div className="rail-placeholder motion-skeleton" />
            </button>
          )) : railItems.length > 0 ? railItems.map((item) => (
            typeof item === "number" ? (
              <button
                type="button"
                key={`pending-${item}`}
                className="rail-slide rail-slide-pending"
                disabled
                aria-label={`PPT ${item} pending`}
              >
                <span>{item}</span>
                <div className="rail-placeholder rail-placeholder-pending motion-skeleton" />
              </button>
            ) : (
              <button
                type="button"
                key={item.index}
                className={`rail-slide ${selectedSlide?.index === item.index ? "rail-slide-active" : ""}`}
                onClick={() => onSelect(item)}
              >
                <span>{item.index}</span>
                <div dangerouslySetInnerHTML={{ __html: item.content }} />
              </button>
            )
          )) : Array.from({ length: 1 }).map((_, index) => (
            <button type="button" className={`rail-slide ${index === 0 ? "rail-slide-active" : ""}`} key={index}>
              <span>{index + 1}</span>
              <div className="rail-placeholder" />
            </button>
          ))}
          <HoverTooltip content={isGenerating ? t("common.pending") : "Manual slide creation is not available yet"}><button className={`rail-add ${isGenerating ? "rail-add-busy" : ""}`} type="button" disabled>
            {isGenerating ? <LoaderCircle size={18} /> : <Plus size={18} />}
          </button></HoverTooltip>
        </div>
        <div className="slide-canvas-area">
          {loading ? (
            <div className="scholarly-slide-frame slide-loading-frame motion-skeleton" />
          ) : selectedSlide ? (
            <div className="scholarly-slide-frame" dangerouslySetInnerHTML={{ __html: selectedSlide.content }} />
          ) : (
            <div className="scholarly-slide-frame slide-empty-preview">
              <span>{isGenerating ? t("monitor.waiting") : t("preview.emptyState")}</span>
            </div>
          )}
          <label className="speaker-notes speaker-notes-editable">
            <textarea value="" disabled placeholder={t("preview.notesPlaceholder")} readOnly />
            <em>0 / 1000</em>
          </label>
          {imageSearchOpen && jobId && selectedSlide ? (
            <ImageSearchPanel
              jobId={jobId}
              slideIndex={selectedSlide.index}
              slideTitle={selectedSlide.name}
              onClose={() => setImageSearchOpen(false)}
              onImageApplied={() => onImageApplied?.(selectedSlide.index)}
            />
          ) : null}
        </div>
      </div>
    </main>
  );
}

function AgentMonitor({
  job,
  logs,
  criticEvents,
  jobId,
  connectionStatus,
  enrichmentStats,
  slideCount,
  activePanel,
  onOpenPanel,
}: {
  job?: JobStatus;
  logs: string[];
  criticEvents: unknown[];
  jobId?: string;
  connectionStatus: string;
  enrichmentStats?: ResearchEnrichmentStats;
  slideCount: number;
  activePanel: SecondaryPanel | null;
  onOpenPanel: (panel: SecondaryPanel) => void;
}) {
  const { t, locale } = useLocale();
  const totalSlides = Math.max(job?.total_slides ?? 0, 0);
  const rawCompletedSlides = Math.max(job?.slides_completed ?? 0, slideCount);
  const completedSlides = totalSlides > 0
    ? clamp(rawCompletedSlides, 0, totalSlides)
    : rawCompletedSlides;
  const progress = clamp(Math.round((job?.progress ?? (totalSlides > 0 ? completedSlides / totalSlides : 0)) * 100), 0, 100);
  const status = job?.status ?? "idle";
  const rawLatestText = logs.length > 0
    ? logs[logs.length - 1].replace(/^\[[^\]]+\]\s*/, "")
    : job?.message ?? t("monitor.waiting");
  const latestText = translateJobMessage(rawLatestText, locale) ?? rawLatestText;
  const isConnected = connectionStatus === "connected";
  const isActiveRun = Boolean(job && !["idle", "complete", "error", "cancelled"].includes(status));
  const nextStep = formatMonitorNextStep(job, logs, locale, t);

  return (
    <section className="agent-monitor-panel">
      <div className="agent-monitor-header">
        <div className="workspace-panel-title">
          <Bot size={18} />
          <span>{t("monitor.title")}</span>
        </div>
        <div className="monitor-tabs">
          <button
            type="button"
            className={activePanel === "log" ? "monitor-tab-active" : ""}
            onClick={() => onOpenPanel("log")}
          >
            <MessageSquareText size={14} />
            {t("monitor.logs")}
          </button>
          <button
            type="button"
            className={activePanel === "critic" ? "monitor-tab-active" : ""}
            onClick={() => onOpenPanel("critic")}
          >
            <Sparkles size={14} />
            {t("monitor.review")}
          </button>
        </div>
      </div>
      <div className="agent-monitor-body">
        <div className={`agent-avatar ${isActiveRun ? "agent-avatar-active" : ""}`}><Bot size={26} /></div>
        <div className="agent-summary">
          <strong>{status === "idle" ? t("monitor.ready") : latestText}</strong>
          <span>
            {enrichmentStats?.total_findings
              ? t("monitor.findings").replace("{count}", String(enrichmentStats.total_findings))
              : t("monitor.counts")
                  .replace("{logs}", String(logs.length))
                  .replace("{reviews}", String(criticEvents.length))}
          </span>
        </div>
        <div className="monitor-progress-block">
          <span><strong>{progress}%</strong> {t("monitor.slideGeneration")}</span>
          <Progress value={progress} className="monitor-progress" />
          <em>{completedSlides} / {totalSlides || "?"} {t("preview.slides")}</em>
        </div>
        <div className="monitor-event">
          <strong>{t("monitor.lastEvent")}</strong>
          <HoverTooltip content={latestText}><span><i className={isConnected ? "event-dot-on" : ""} />{latestText}</span></HoverTooltip>
        </div>
        <div className="monitor-event">
          <strong>{t("monitor.nextStep")}</strong>
          <HoverTooltip content={nextStep}><span>{nextStep}</span></HoverTooltip>
        </div>
      </div>
    </section>
  );
}

function CriticPanel({ criticEvents, jobId }: { criticEvents: unknown[]; jobId?: string }) {
  return <AgentLog mode="critic" hideHeader logs={[]} criticEvents={criticEvents as CriticEvent[]} jobId={jobId} />;
}

function formatMonitorNextStep(
  job: JobStatus | undefined,
  logs: string[],
  locale: "en" | "zh",
  t: (key: string) => string,
) {
  const status = job?.status ?? "idle";
  if (status === "idle") {
    return t("monitor.nextUpload");
  }
  if (status === "complete") {
    return t("result.download");
  }
  if (status === "error" || status === "cancelled") {
    return translateStageStatus(status, locale, "progress");
  }
  const activeStage = inferActiveStage(job, logs);
  const activeIndex = PROGRESS_STAGES.findIndex((stage) => stage.id === activeStage);
  const nextStage = activeIndex >= 0 ? PROGRESS_STAGES[activeIndex + 1] : undefined;
  return nextStage
    ? translateStageStatus(nextStage.id, locale, "progress")
    : translateStageStatus(status, locale, "progress");
}

function parseOptionalPositiveInt(value: string): number | undefined {
  const normalized = value.trim();
  if (!normalized) {
    return undefined;
  }
  const parsed = Number(normalized);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return undefined;
  }
  return Math.floor(parsed);
}

function parseBoundedPositiveInt(
  value: string,
  fallback: number,
  min: number,
  max: number,
): number {
  const parsed = parseOptionalPositiveInt(value);
  if (parsed === undefined) {
    return fallback;
  }
  return Math.min(max, Math.max(min, parsed));
}

function parseBoundedInt(
  value: string,
  fallback: number,
  min: number,
  max: number,
): number {
  const normalized = value.trim();
  if (!normalized) {
    return fallback;
  }
  const parsed = Number(normalized);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.min(max, Math.max(min, Math.floor(parsed)));
}

function resolveRequestedLanguage(languageMode: LanguageMode, customLanguage: string): string {
  if (languageMode === "custom") {
    return customLanguage.trim();
  }
  return languageMode;
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}
