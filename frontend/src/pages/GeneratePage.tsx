import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Terminal, X } from "lucide-react";
import { Layout } from "../components/layout/Layout";
import { ModelSelector } from "../components/config/ModelSelector";
import { OptionsPanel } from "../components/config/OptionsPanel";
import { SlidePreview } from "../components/preview/SlidePreview";
import { SlideViewer } from "../components/preview/SlideViewer";
import { AgentLog } from "../components/progress/AgentLog";
import { ProgressPanel } from "../components/progress/ProgressPanel";
import { FilePreview } from "../components/upload/FilePreview";
import { UploadZone } from "../components/upload/UploadZone";
import { useGeneration } from "../hooks/useGeneration";
import { useLocale } from "../i18n";
import { fetchTemplates } from "../lib/api";
import type { DeepSeekSettings, OpenAISettings, ResearchConfig, TemplateInfo } from "../lib/types";
import { TemplateManager } from "../components/template/TemplateManager";

const ROUTING_PROFILE_STORAGE_KEY = "paper-ppt-agent-routing-profiles-v1";
const PRESENTATION_SETTINGS_STORAGE_KEY = "paper-ppt-agent-presentation-settings-v1";
type LanguageMode = "zh" | "en" | "custom";
type SecondaryPanel = "log";
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
    startGeneration,
    cancelCurrentRun,
    connect,
    resumeCurrentRun,
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
  const [timeoutSeconds, setTimeoutSeconds] = useState(initialSettings.timeoutSeconds ?? "");
  const [maxCriticAttempts, setMaxCriticAttempts] = useState(initialSettings.maxCriticAttempts ?? "3");
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
    numPages,
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

  return (
    <Layout contentClassName="studio-page">
      <section className="studio-layout">
        <div className="studio-column studio-column-left">
          <UploadZone onFileSelect={(file) => void uploadFile(file)} />
          <FilePreview session={uploadSession} />
          <ProgressPanel job={job} connectionStatus={connectionStatus} enrichmentStats={enrichmentStats} />
        </div>

        <div className="studio-column studio-column-preview">
          <SlideViewer slide={selectedSlide} />
          <SlidePreview slides={slides} selectedSlide={selectedSlide} onSelect={selectSlide} />
        </div>

        <div className="studio-config-rail studio-column-right">
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
          <div className="studio-secondary-actions">
            <button
              type="button"
              className={`secondary-action ${secondaryPanel === "log" ? "secondary-action-active" : ""}`}
              onClick={() => setSecondaryPanel((current) => (current === "log" ? null : "log"))}
            >
              <Terminal size={16} />
              <span>{t("log.title")}</span>
            </button>
          </div>
          <button
            type="button"
            className="primary-button full-width launch-button"
            disabled={
              !uploadSession ||
              !provider ||
              !model.trim() ||
              !apiKey ||
              (languageMode === "custom" && !customLanguage.trim()) ||
              canCancelCurrentRun
            }
            onClick={async () => {
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
              const jobId = await startGeneration({
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
                  timeout_seconds: parseOptionalPositiveInt(timeoutSeconds),
                  max_critic_attempts: parseBoundedPositiveInt(maxCriticAttempts, 3, 1, 10),
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
                  visual_qa_max_attempts: parseBoundedPositiveInt(visualQaMaxAttempts, 1, 1, 10),
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
              connect(jobId);
            }}
          >
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
              {cancelLoading || job?.status === "cancelling"
                ? t("studio.canceling")
                : t("studio.cancel")}
            </button>
          ) : null}
          {error ? <p className="error-text">{error}</p> : null}
        </div>
      </section>
      <aside
        className={`studio-secondary-panel ${secondaryPanel ? "studio-secondary-panel-open" : ""}`}
        aria-hidden={!secondaryPanel}
      >
        <div className="studio-secondary-header">
          <div className="panel-title-row">
            <Terminal size={15} className="panel-title-icon" />
            <p className="panel-title">{t("log.title")}</p>
          </div>
          <button
            type="button"
            className="icon-btn"
            aria-label={t("common.close")}
            onClick={() => setSecondaryPanel(null)}
          >
            <X size={17} />
          </button>
        </div>
        <div className="studio-secondary-body">
          {secondaryPanel === "log" ? <AgentLog logs={logs} criticEvents={criticEvents} jobId={jobId} /> : null}
        </div>
      </aside>
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

function resolveRequestedLanguage(languageMode: LanguageMode, customLanguage: string): string {
  if (languageMode === "custom") {
    return customLanguage.trim();
  }
  return languageMode;
}
