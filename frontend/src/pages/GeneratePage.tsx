import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  Bot,
  BarChart3,
  ChevronDown,
  CircleCheck,
  Database,
  Download,
  Eye,
  EyeOff,
  Image as ImageIcon,
  LoaderCircle,
  Link as LinkIcon,
  Maximize2,
  MessageSquareText,
  Minus,
  MoreHorizontal,
  MousePointer2,
  Omega,
  Play,
  Plus,
  Redo2,
  Save,
  Search,
  Send,
  Settings2,
  Sparkles,
  Square,
  Table2,
  Type,
  Undo2,
  User,
  Video,
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
import { PptistStudioHost } from "../components/pptist/PptistStudioHost";
import { useGeneration } from "../hooks/useGeneration";
import { useLocale } from "../i18n";
import { fetchTemplateAgentClaudeCodeStatus, fetchTemplates } from "../lib/api";
import type {
  DeepSeekSettings,
  GenerationHistoryItem,
  GenerationAgentMessage,
  JobStatus,
  OpenAISettings,
  PreviewSlide,
  CriticEvent,
  ResearchConfig,
  ResearchEnrichmentStats,
  TemplateInfo,
  UploadResponse,
} from "../lib/types";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Progress } from "../components/ui/progress";
import { Tabs, TabsList, TabsTrigger } from "../components/ui/tabs";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "../components/ui/tooltip";
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
  artifactThinkingMode?: "disabled" | "default";
  deepseekSettings?: DeepSeekSettings;
  openaiSettings?: OpenAISettings;
}

type RoutingProfileMap = Record<string, RoutingProfile>;

interface PresentationSettingsDraft {
  generationBackend?: "provider" | "agent";
  agentRuntime?: "claude_code" | "codex";
  agentModel?: string;
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
  researchConfig?: ResearchConfig;
  templateId?: string;
}

function uniqueStrings(values: string[]): string[] {
  const out: string[] = [];
  for (const value of values) {
    const item = value.trim();
    if (item && !out.includes(item)) out.push(item);
  }
  return out;
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
    agentMessages,
    criticEvents,
    enrichmentStats,
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
    interruptCurrentAgent,
    sendAgentFeedback,
    connect,
    resumeCurrentRun,
    refreshHistoryStatuses,
    reset,
  } = useGeneration();
  const [initialSettings] = useState(readPresentationSettingsDraft);

  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [artifactThinkingMode, setArtifactThinkingMode] = useState<"disabled" | "default">(
    "disabled",
  );
  const [deepSeekSettings, setDeepSeekSettings] = useState<DeepSeekSettings>(
    DEFAULT_DEEPSEEK_SETTINGS,
  );
  const [openAISettings, setOpenAISettings] = useState<OpenAISettings>(
    DEFAULT_OPENAI_SETTINGS,
  );
  const [generationBackend, setGenerationBackend] = useState<"provider" | "agent">(
    initialSettings.generationBackend ?? "provider",
  );
  const [agentRuntime, setAgentRuntime] = useState<"claude_code" | "codex">(
    initialSettings.agentRuntime ?? "claude_code",
  );
  const [agentModel, setAgentModel] = useState(initialSettings.agentModel ?? "");
  const [agentBaseUrl, setAgentBaseUrl] = useState("");
  const [agentCredential, setAgentCredential] = useState("");
  const [agentCredentialKind, setAgentCredentialKind] = useState<"api_key" | "auth_token">("api_key");
  const [showAgentCredential, setShowAgentCredential] = useState(false);
  const [agentModelOptions, setAgentModelOptions] = useState<string[]>([]);
  const [showAgentModeConfirm, setShowAgentModeConfirm] = useState(false);
  const [showCodexRuntimeConfirm, setShowCodexRuntimeConfirm] = useState(false);
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
  const RESEARCH_KEYS_STORAGE = "paper-ppt-agent-research-keys";
  const [enableDeepResearch, setEnableDeepResearch] = useState(initialSettings.enableDeepResearch ?? false);
  const [enableVisualCritic, setEnableVisualCritic] = useState(initialSettings.enableVisualCritic ?? false);
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
  const [templateId, setTemplateId] = useState(initialSettings.templateId ?? "");
  const [templates, setTemplates] = useState<TemplateInfo[]>([]);
  const [cancelLoading, setCancelLoading] = useState(false);
  const [secondaryPanel, setSecondaryPanel] = useState<SecondaryPanel | null>(null);
  const [workspaceSideTab, setWorkspaceSideTab] = useState<"sources" | "config">("sources");
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
      setArtifactThinkingMode(saved?.artifactThinkingMode ?? "disabled");
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
    setArtifactThinkingMode(saved?.artifactThinkingMode ?? "disabled");
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
      artifactThinkingMode,
      deepseekSettings: provider === "deepseek" ? deepSeekSettings : existing?.deepseekSettings,
      openaiSettings: provider === "openai" ? openAISettings : existing?.openaiSettings,
    };
    writeRoutingProfiles(profiles);
  }, [apiKey, artifactThinkingMode, baseUrl, deepSeekSettings, model, openAISettings, provider, targetJobId]);

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
    setArtifactThinkingMode(savedProfile?.artifactThinkingMode ?? "disabled");
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
    setMaxCriticAttempts(String(options.max_critic_attempts ?? 0));
    setEnableDeepResearch(Boolean(options.enable_deep_research));
    setEnableVisualCritic(Boolean(options.enable_visual_critic));
    setVisualQaMaxAttempts(String(options.visual_qa_max_attempts ?? 1));
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
    setTemplateId(options.template_id ?? "");
  }, [selectedRunConfig, targetJobId]);

  useEffect(() => {
    setLanguageMode((current) => (current === "custom" ? current : locale === "zh" ? "zh" : "en"));
  }, [locale]);

  useEffect(() => {
    if (agentRuntime !== "claude_code") {
      setAgentModelOptions([]);
      return;
    }
    let cancelled = false;
    void fetchTemplateAgentClaudeCodeStatus()
      .then((status) => {
        if (cancelled) return;
        setAgentModelOptions(uniqueStrings([
          ...(status.available_models ?? []),
          ...Object.values(status.configured_models ?? {}),
        ]));
        const provider = status.provider_config;
        if (provider?.base_url) {
          setAgentBaseUrl(provider.base_url ?? "");
        } else {
          setAgentBaseUrl("");
          setAgentCredential("");
        }
        const credential = provider?.api_key || provider?.auth_token || provider?.oauth_token || "";
        if (provider?.api_key) {
          setAgentCredentialKind("api_key");
        } else if (provider?.auth_token || provider?.oauth_token) {
          setAgentCredentialKind("auth_token");
        }
        if (credential) {
          setAgentCredential(credential);
        }
        if (provider?.base_url) {
          setAgentModel(status.default_model ?? "");
        } else if (status.default_model) {
          setAgentModel((current) => current.trim() ? current : status.default_model ?? "");
        }
      })
      .catch(() => {
        if (!cancelled) setAgentModelOptions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [agentRuntime]);

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
        generationBackend,
        agentRuntime,
        agentModel,
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
    generationBackend,
    agentRuntime,
    agentModel,
    enableDeepResearch,
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
    const normalizedAgentModel = agentRuntime === "claude_code" ? agentModel.trim() : "";
    const normalizedAgentBaseUrl = agentRuntime === "claude_code" ? agentBaseUrl.trim() : "";
    const normalizedAgentCredential = agentRuntime === "claude_code" ? agentCredential.trim() : "";
    if (generationBackend === "provider") {
      const profiles = readRoutingProfiles();
      profiles[provider] = {
        model: normalizedModel,
        baseUrl,
        apiKey,
        artifactThinkingMode,
        deepseekSettings: provider === "deepseek" ? deepSeekSettings : undefined,
        openaiSettings: provider === "openai" ? openAISettings : undefined,
      };
      writeRoutingProfiles(profiles);
    }
    const agentReasoningEffort =
      agentRuntime === "codex"
        ? enableDeepResearch || detailLevel === "very_high"
          ? "xhigh"
          : detailLevel === "high"
            ? "high"
            : undefined
        : undefined;
    const nextJobId = await startGeneration({
      session_id: uploadSession.session_id,
      instruction,
      model_config: generationBackend === "provider"
        ? {
            provider,
            model: normalizedModel,
            api_key: apiKey,
            base_url: baseUrl || undefined,
            artifact_thinking_mode: artifactThinkingMode,
            deepseek_settings: provider === "deepseek" ? deepSeekSettings : undefined,
            openai_settings: provider === "openai" ? openAISettings : undefined,
          }
        : undefined,
      options: {
        generation_backend: generationBackend,
        agent_config: generationBackend === "agent"
          ? {
              runtime: agentRuntime,
              model: agentRuntime === "claude_code" ? normalizedAgentModel || undefined : undefined,
              base_url: agentRuntime === "claude_code" ? normalizedAgentBaseUrl || undefined : undefined,
              api_key:
                agentRuntime === "claude_code" && normalizedAgentBaseUrl && agentCredentialKind === "api_key"
                  ? normalizedAgentCredential || undefined
                  : undefined,
              auth_token:
                agentRuntime === "claude_code" && normalizedAgentBaseUrl && agentCredentialKind === "auth_token"
                  ? normalizedAgentCredential || undefined
                  : undefined,
              allow_external_research: Boolean(researchConfig.arxiv_search_enabled || researchConfig.semantic_scholar_enabled || researchConfig.web_search_enabled),
              allow_deep_research: enableDeepResearch,
              enable_visual_qa: enableVisualCritic,
              reasoning_effort: agentReasoningEffort,
              load_project_settings: true,
              reply_language: locale === "zh" ? "zh" : "en",
            }
          : undefined,
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
    (generationBackend === "provider" && (!provider || !model.trim() || !apiKey)) ||
    (languageMode === "custom" && !customLanguage.trim()) ||
    canCancelCurrentRun;
  const activeAgentGeneration = Boolean(
    jobId && selectedRunConfig?.provider?.startsWith("agent:") && job,
  );
  const sideConfigLabel = activeAgentGeneration ? "Agent" : t("config.title");
  const requestGenerationBackend = (nextBackend: "provider" | "agent") => {
    if (nextBackend === "provider") {
      setShowAgentModeConfirm(false);
      setShowCodexRuntimeConfirm(false);
      setGenerationBackend(nextBackend);
      return;
    }
    if (generationBackend !== "agent") {
      setShowAgentModeConfirm(true);
    }
  };
  const requestAgentRuntime = (nextRuntime: "claude_code" | "codex") => {
    if (nextRuntime === "claude_code") {
      setShowCodexRuntimeConfirm(false);
      setAgentRuntime(nextRuntime);
      return;
    }
    if (agentRuntime !== "codex") {
      setShowCodexRuntimeConfirm(true);
    }
  };

  return (
    <Layout showSidebar={false} contentClassName="studio-page scholarly-workspace-page">
      <section className="scholarly-workspace" data-side-tab={workspaceSideTab}>
        <div className="workspace-side-tabs" role="tablist" aria-label={`${t("source.title")} / ${sideConfigLabel}`}>
          <button
            type="button"
            className={`workspace-side-tab ${workspaceSideTab === "sources" ? "workspace-side-tab-active" : ""}`}
            aria-selected={workspaceSideTab === "sources"}
            role="tab"
            onClick={() => setWorkspaceSideTab("sources")}
          >
            <Database size={16} />
            <span>{t("source.title")}</span>
          </button>
          <button
            type="button"
            className={`workspace-side-tab ${workspaceSideTab === "config" ? "workspace-side-tab-active" : ""}`}
            aria-selected={workspaceSideTab === "config"}
            role="tab"
            onClick={() => setWorkspaceSideTab("config")}
          >
            {activeAgentGeneration ? <Bot size={16} /> : <Settings2 size={16} />}
            <span>{sideConfigLabel}</span>
          </button>
        </div>
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
          isGenerating={canCancelCurrentRun}
          loading={Boolean(targetJobId && !job && slides.length === 0)}
        />

        {activeAgentGeneration ? (
          <GenerationAgentConsole
            job={job}
            jobId={jobId}
            runtime={selectedRunConfig?.provider?.replace(/^agent:/, "") || agentRuntime}
            messages={agentMessages}
            canStop={canCancelCurrentRun}
            stopPending={cancelLoading || job?.status === "cancelling"}
            onStop={async () => {
              setCancelLoading(true);
              try {
                await interruptCurrentAgent();
              } finally {
                setCancelLoading(false);
              }
            }}
            onSend={(text) => sendAgentFeedback(text)}
            allowSendWhenTerminal={true}
          />
        ) : (
        <aside className="configuration-panel">
          <section className="agent-mode-section">
            <div className="segmented-control generation-backend-toggle" role="tablist">
              <button
                type="button"
                className={generationBackend === "provider" ? "active" : ""}
                onClick={() => requestGenerationBackend("provider")}
              >
                Provider
              </button>
              <button
                type="button"
                className={generationBackend === "agent" ? "active" : ""}
                onClick={() => requestGenerationBackend("agent")}
              >
                Agent
              </button>
            </div>
            {generationBackend === "agent" ? (
              <div className="agent-generation-config">
                <div className="segmented-control runtime-toggle" role="tablist">
                  <button
                    type="button"
                    className={agentRuntime === "claude_code" ? "active" : ""}
                    onClick={() => requestAgentRuntime("claude_code")}
                  >
                    Claude Code
                  </button>
                  <button
                    type="button"
                    className={agentRuntime === "codex" ? "active" : ""}
                    onClick={() => requestAgentRuntime("codex")}
                  >
                    Codex
                  </button>
                </div>
                {agentRuntime === "claude_code" ? (
                  <>
                    {agentBaseUrl.trim() ? (
                      <label className="agent-model-field">
                        <span>{t("generation.agent.baseUrl")}</span>
                        <input
                          type="text"
                          value={agentBaseUrl}
                          onChange={(event) => setAgentBaseUrl(event.target.value)}
                          placeholder={t("generation.agent.baseUrlPlaceholder")}
                        />
                      </label>
                    ) : null}
                    <label className="agent-model-field">
                      <span>{t("generation.agent.model")}</span>
                      <input
                        type="text"
                        list="generation-agent-model-options"
                        value={agentModel}
                        onChange={(event) => setAgentModel(event.target.value)}
                        placeholder={t("generation.agent.modelPlaceholderClaude")}
                      />
                      {agentModelOptions.length > 0 ? (
                        <datalist id="generation-agent-model-options">
                          {agentModelOptions.map((option) => (
                            <option key={option} value={option} />
                          ))}
                        </datalist>
                      ) : null}
                    </label>
                    {agentBaseUrl.trim() ? (
                      <label className="agent-model-field">
                        <span>{t("generation.agent.apiKey")}</span>
                        <span className="agent-secret-input">
                          <input
                            type={showAgentCredential ? "text" : "password"}
                            value={agentCredential}
                            onChange={(event) => setAgentCredential(event.target.value)}
                            placeholder={t("generation.agent.apiKeyPlaceholder")}
                          />
                          <button
                            type="button"
                            className="api-key-toggle"
                            onClick={() => setShowAgentCredential((value) => !value)}
                            aria-label={showAgentCredential ? t("generation.agent.hideApiKey") : t("generation.agent.showApiKey")}
                          >
                            {showAgentCredential ? <EyeOff size={14} /> : <Eye size={14} />}
                          </button>
                        </span>
                      </label>
                    ) : null}
                  </>
                ) : null}
              </div>
            ) : null}
          </section>
          <div className="workspace-panel-header">
            <div className="workspace-panel-title">
              <Settings2 size={18} />
              <span>{t("config.title")}</span>
            </div>
          </div>
          <div className="configuration-scroll">
            {generationBackend === "provider" ? (
              <ModelSelector
                providers={providers}
                provider={provider}
                model={model}
                baseUrl={baseUrl}
                apiKey={apiKey}
                artifactThinkingMode={artifactThinkingMode}
                deepSeekSettings={deepSeekSettings}
                openAISettings={openAISettings}
                onProviderChange={(nextProvider) => {
                  setProvider(nextProvider);
                }}
                onModelChange={setModel}
                onBaseUrlChange={setBaseUrl}
                onApiKeyChange={setApiKey}
                onArtifactThinkingModeChange={setArtifactThinkingMode}
                onDeepSeekSettingsChange={setDeepSeekSettings}
                onOpenAISettingsChange={setOpenAISettings}
              />
            ) : null}
            <OptionsPanel
                agentMode={generationBackend === "agent"}
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
                onTemplateChange={setTemplateId}
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
        )}

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
      {showAgentModeConfirm
        ? createPortal(
            <AgentModeEntryConfirm
              onClose={() => setShowAgentModeConfirm(false)}
              onConfirm={() => {
                setShowAgentModeConfirm(false);
                setGenerationBackend("agent");
              }}
            />,
            document.body,
          )
        : null}
      {showCodexRuntimeConfirm
        ? createPortal(
            <CodexRuntimeConfirm
              onClose={() => setShowCodexRuntimeConfirm(false)}
              onConfirm={() => {
                setShowCodexRuntimeConfirm(false);
                setAgentRuntime("codex");
              }}
            />,
            document.body,
          )
        : null}
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
    </Layout>
  );
}

function AgentModeEntryConfirm({
  onClose,
  onConfirm,
}: {
  onClose: () => void;
  onConfirm: () => void;
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
    <div className="workbench-agent-confirm-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="workbench-agent-confirm-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="workbench-agent-mode-confirm-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="workbench-agent-confirm-header">
          <div className="workbench-agent-confirm-icon"><Bot size={18} /></div>
          <div>
            <h2 id="workbench-agent-mode-confirm-title">{t("generation.agentModeConfirm.title")}</h2>
            <p>{t("generation.agentModeConfirm.description")}</p>
          </div>
          <button type="button" className="workbench-agent-confirm-close" onClick={onClose} aria-label={t("generation.agentModeConfirm.close")}>
            <X size={16} />
          </button>
        </header>
        <div className="workbench-agent-confirm-notice">
          <strong>{t("generation.agentModeConfirm.requirementTitle")}</strong>
          <p>{t("generation.agentModeConfirm.requirement")}</p>
        </div>
        <footer className="workbench-agent-confirm-actions">
          <button type="button" className="secondary-button" onClick={onClose}>{t("generation.agentModeConfirm.cancel")}</button>
          <button type="button" className="primary-button" onClick={onConfirm}>
            <Bot size={15} />
            {t("generation.agentModeConfirm.confirm")}
          </button>
        </footer>
      </section>
    </div>
  );
}

function CodexRuntimeConfirm({
  onClose,
  onConfirm,
}: {
  onClose: () => void;
  onConfirm: () => void;
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
    <div className="workbench-agent-confirm-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="workbench-agent-confirm-dialog workbench-codex-confirm-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="workbench-codex-confirm-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="workbench-agent-confirm-header">
          <div className="workbench-agent-confirm-icon workbench-agent-confirm-icon-warning"><Sparkles size={18} /></div>
          <div>
            <h2 id="workbench-codex-confirm-title">{t("generation.codexConfirm.title")}</h2>
            <p>{t("generation.codexConfirm.description")}</p>
          </div>
          <button type="button" className="workbench-agent-confirm-close" onClick={onClose} aria-label={t("generation.codexConfirm.close")}>
            <X size={16} />
          </button>
        </header>
        <div className="workbench-agent-confirm-notice workbench-agent-confirm-warning">
          <strong>{t("generation.codexConfirm.recommendTitle")}</strong>
          <p>{t("generation.codexConfirm.recommendation")}</p>
        </div>
        <footer className="workbench-agent-confirm-actions">
          <button type="button" className="secondary-button" onClick={onClose}>{t("generation.codexConfirm.cancel")}</button>
          <button type="button" className="primary-button" onClick={onConfirm}>{t("generation.codexConfirm.confirm")}</button>
        </footer>
      </section>
    </div>
  );
}

export function GenerationAgentConsole({
  job,
  jobId,
  runtime,
  messages,
  canStop,
  stopPending,
  onStop,
  onSend,
  allowSendWhenTerminal = false,
}: {
  job?: JobStatus;
  jobId?: string;
  runtime: string;
  messages: GenerationAgentMessage[];
  canStop: boolean;
  stopPending: boolean;
  onStop: () => Promise<void> | void;
  onSend: (text: string) => Promise<void> | void;
  allowSendWhenTerminal?: boolean;
}) {
  const { t } = useLocale();
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [activeChannel, setActiveChannel] = useState("main");
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const terminal = Boolean(job && ["complete", "error", "cancelled"].includes(job.status));
  const canSend = Boolean(jobId && draft.trim() && job?.status !== "pausing" && (!terminal || allowSendWhenTerminal) && !sending);
  const displayRuntime = runtime === "claude_code" ? "Claude Code" : "Codex";
  const linkedMessages = useMemo(() => linkGenerationSubagentMessages(messages), [messages]);
  const usage = useMemo(() => latestGenerationAgentUsage(linkedMessages), [linkedMessages]);
  const subagents = useMemo(() => generationSubagentChannels(linkedMessages), [linkedMessages]);
  const showChannelTabs = subagents.length > 0;
  const visibleMessages = useMemo(
    () => linkedMessages.filter((message) => message.kind !== "usage" && (activeChannel === "main" ? !message.subagentId : message.subagentId === activeChannel)),
    [activeChannel, linkedMessages],
  );
  const forceCloseToolState = job?.status === "error" || job?.status === "cancelled" || job?.status === "cancelling"
    ? "error"
    : job?.status === "complete"
      ? "done"
      : undefined;
  const activityItems = useMemo(
    () => generationActivityItems(visibleMessages, t, forceCloseToolState),
    [forceCloseToolState, t, visibleMessages],
  );
  const hasActiveToolGroup = useMemo(
    () => activityItems.some((item) => item.kind === "group" && item.state === "active"),
    [activityItems],
  );
  const stopLabel =
    job?.status === "cancelling"
        ? t("generation.agent.canceling")
        : job?.status === "paused" || job?.status === "pausing"
          ? t("generation.agent.cancel")
          : t("generation.agent.pause");
  const showThinking = Boolean(
    job &&
    !terminal &&
    job.status !== "idle" &&
    job.status !== "paused" &&
    job.status !== "cancelling" &&
    !hasActiveToolGroup,
  );
  const showSlowResponse = job?.message?.startsWith("Agent has not produced new activity for a while.") ?? false;

  useEffect(() => {
    const node = scrollerRef.current;
    if (!node) return;
    const frame = window.requestAnimationFrame(() => {
      node.scrollTop = node.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activityItems.length, job?.message, showThinking]);

  useEffect(() => {
    if (activeChannel !== "main" && !subagents.some((item) => item.id === activeChannel)) {
      setActiveChannel("main");
    }
  }, [activeChannel, subagents]);

  const submit = async () => {
    const text = draft.trim();
    if (!text || !jobId || (terminal && !allowSendWhenTerminal)) return;
    setDraft("");
    setSending(true);
    try {
      await onSend(text);
    } finally {
      setSending(false);
    }
  };

  return (
    <aside className="configuration-panel generation-agent-console">
      <div className="generation-agent-console-header">
        <div className="workspace-panel-title">
          <Bot size={18} />
          <span>{displayRuntime}</span>
        </div>
      </div>
      {showChannelTabs ? (
        <div className="generation-agent-channel-tabs" role="tablist">
          <button
            type="button"
            className={activeChannel === "main" ? "active" : ""}
            onClick={() => setActiveChannel("main")}
          >
            {t("generation.agent.main")}
          </button>
          {subagents.map((subagent, index) => (
            <GenerationTooltip key={subagent.id} content={subagent.detail || ""}>
              <button
                type="button"
                className={activeChannel === subagent.id ? "active" : ""}
                onClick={() => setActiveChannel(subagent.id)}
              >
                {t("generation.agent.subagent").replace("{n}", String(index + 1))}
              </button>
            </GenerationTooltip>
          ))}
        </div>
      ) : null}
      <div ref={scrollerRef} className="generation-agent-chat">
        {activityItems.map((item) => <GenerationAgentActivityItem key={item.id} item={item} />)}
        {showThinking ? <GenerationAgentThinking slowResponse={showSlowResponse} /> : null}
      </div>
      <div className="generation-agent-composer">
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
              event.preventDefault();
              void submit();
            }
          }}
          placeholder={t("generation.agent.placeholder")}
          disabled={terminal && !allowSendWhenTerminal}
          rows={3}
        />
        <div className="generation-agent-composer-footer">
          <div className="generation-agent-composer-meta">
            <GenerationTooltip content={t("template.collab.model")}>
              <span>
                <Bot size={10} />
                <em>{usage.model || displayRuntime}</em>
              </span>
            </GenerationTooltip>
            <GenerationTooltip content={generationTokensTooltip(t, usage)}>
              <span>
                {formatGenerationTokens(usage.total_tokens || generationUsageTotal(usage))}
              </span>
            </GenerationTooltip>
          </div>
          <div className="generation-agent-actions">
            {canStop ? (
              <GenerationTooltip content={stopLabel}>
                <span className="generation-tooltip-trigger">
                  <button
                    type="button"
                    className="secondary-button danger-button generation-agent-icon-button"
                    disabled={stopPending}
                    onClick={() => void onStop()}
                    aria-label={stopLabel}
                  >
                    {stopPending ? <LoaderCircle size={14} className="spin" /> : <Square size={13} fill="currentColor" />}
                  </button>
                </span>
              </GenerationTooltip>
            ) : null}
            <GenerationTooltip content={t("template.collab.send")}>
              <span className="generation-tooltip-trigger">
                <button
                  type="button"
                  className="primary-button generation-agent-icon-button"
                  disabled={!canSend}
                  onClick={() => void submit()}
                  aria-label={t("template.collab.send")}
                >
                  {sending ? <LoaderCircle size={14} className="spin" /> : <Send size={14} />}
                </button>
              </span>
            </GenerationTooltip>
          </div>
        </div>
      </div>
    </aside>
  );
}

function GenerationAgentThinking({ slowResponse = false }: { slowResponse?: boolean }) {
  const { t } = useLocale();
  return (
    <div className="generation-agent-bubble-row" data-role="assistant" data-kind="thinking">
      <div className="generation-agent-bubble generation-agent-thinking">
        <div className="generation-agent-bubble-head">
          <span><Bot size={11} /></span>
          <strong>Agent</strong>
        </div>
        <p>
          <span>{t(slowResponse ? "generation.agent.slowResponse" : "template.collab.thinking")}</span>
          <span className="generation-agent-thinking-dots" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
        </p>
      </div>
    </div>
  );
}

interface GenerationAgentActivity {
  id: string;
  kind: "message" | "tool" | "group" | "status";
  role?: "user" | "assistant" | "system";
  label: string;
  detail?: string;
  state?: "active" | "done" | "error" | "info";
  count?: number;
  children?: GenerationAgentMessage[];
}

function GenerationAgentActivityItem({ item }: { item: GenerationAgentActivity }) {
  const isUser = item.role === "user";
  if (item.kind === "group") {
    return <GenerationAgentToolGroup item={item} />;
  }
  return (
    <div className="generation-agent-bubble-row" data-role={isUser ? "user" : "assistant"} data-kind={item.kind}>
      <div className="generation-agent-bubble">
        <div className="generation-agent-bubble-head">
          <span>{isUser ? <User size={11} /> : item.kind === "tool" ? <CircleCheck size={11} /> : <Bot size={11} />}</span>
          <strong>{item.label}</strong>
        </div>
        {item.detail ? <p>{item.detail}</p> : null}
      </div>
    </div>
  );
}

function GenerationTooltip({ content, children }: { content: string; children: ReactNode }) {
  if (!content) return <>{children}</>;
  return (
    <TooltipProvider delayDuration={120}>
      <Tooltip>
        <TooltipTrigger asChild>{children}</TooltipTrigger>
        <TooltipContent side="top" align="start" className="config-tooltip-content generation-agent-tooltip-content">
          {content}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function GenerationAgentToolGroup({ item }: { item: GenerationAgentActivity }) {
  const [open, setOpen] = useState(false);
  const state = item.state ?? "info";
  const summaryTitle = generationToolGroupTitle(item);
  return (
    <div className="generation-agent-tool-group" data-state={state} data-open={open ? "true" : "false"}>
      <GenerationTooltip content={summaryTitle}>
        <button
          type="button"
          className="generation-agent-tool-summary"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
        >
          <ChevronDown size={12} className="generation-agent-tool-chevron" />
          <span className="generation-agent-tool-state-icon">
            {state === "active" ? <LoaderCircle size={11} className="spin" /> : <CircleCheck size={11} />}
          </span>
          <strong>{item.label}</strong>
          {item.detail ? <em>{item.detail}</em> : null}
          {item.count ? <b>{item.count}</b> : null}
        </button>
      </GenerationTooltip>
      {open ? (
        <div className="generation-agent-tool-details">
          {(item.children ?? []).map((message) => (
            <GenerationTooltip key={message.id} content={generationToolMessageTitle(message)}>
              <div
                className="generation-agent-tool-detail"
                data-state={message.status ?? "info"}
              >
                <span>{message.status === "running" ? <LoaderCircle size={10} className="spin" /> : <CircleCheck size={10} />}</span>
                <em>{describeGenerationTool(message)}</em>
              </div>
            </GenerationTooltip>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function generationToolGroupTitle(item: GenerationAgentActivity): string {
  const lines = [item.label, item.detail].filter(Boolean) as string[];
  for (const child of item.children ?? []) {
    const childTitle = generationToolMessageTitle(child);
    if (childTitle) lines.push(childTitle);
  }
  return lines.join("\n\n");
}

function generationToolMessageTitle(message: GenerationAgentMessage): string {
  const lines = [describeGenerationTool(message)];
  const input = message.data?.input;
  if (input && typeof input === "object") {
    const obj = input as Record<string, unknown>;
    const command = typeof obj.command === "string" ? obj.command : "";
    const cwd = typeof obj.cwd === "string" ? obj.cwd : "";
    const path = typeof obj.file_path === "string" ? obj.file_path : typeof obj.path === "string" ? obj.path : "";
    const prompt = typeof obj.prompt === "string" ? obj.prompt : "";
    if (cwd) lines.push(`cwd: ${cwd}`);
    if (command) lines.push(`command: ${command}`);
    if (path) lines.push(`path: ${path}`);
    if (prompt) lines.push(`prompt: ${prompt}`);
  }
  const output = message.data?.output;
  if (output && typeof output === "object") {
    const obj = output as Record<string, unknown>;
    const exitCode = obj.exit_code ?? obj.exitCode;
    const aggregated = typeof obj.aggregated_output === "string" ? obj.aggregated_output : "";
    if (exitCode !== undefined && exitCode !== null) lines.push(`exit: ${String(exitCode)}`);
    if (aggregated) lines.push(`output: ${aggregated}`);
  }
  return lines.filter(Boolean).join("\n");
}

function latestGenerationAgentUsage(messages: GenerationAgentMessage[]) {
  const fallback = {
    model: "",
    input_tokens: 0,
    output_tokens: 0,
    cache_read_input_tokens: 0,
    cache_creation_input_tokens: 0,
    reasoning_output_tokens: 0,
    total_tokens: 0,
  };
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.kind !== "usage") continue;
    const data = message.data ?? {};
    return {
      model: typeof data.model === "string" ? data.model : "",
      input_tokens: Number(data.input_tokens ?? 0),
      output_tokens: Number(data.output_tokens ?? 0),
      cache_read_input_tokens: Number(data.cache_read_input_tokens ?? 0),
      cache_creation_input_tokens: Number(data.cache_creation_input_tokens ?? 0),
      reasoning_output_tokens: Number(data.reasoning_output_tokens ?? 0),
      total_tokens: Number(data.total_tokens ?? 0),
    };
  }
  return fallback;
}

function generationUsageTotal(usage: ReturnType<typeof latestGenerationAgentUsage>): number {
  return (
    usage.input_tokens +
    usage.output_tokens +
    usage.cache_read_input_tokens +
    usage.cache_creation_input_tokens +
    usage.reasoning_output_tokens
  );
}

function generationTokensTooltip(
  t: (key: string) => string,
  usage: ReturnType<typeof latestGenerationAgentUsage>,
): string {
  const lines = [
    t("template.collab.tokensThisTask"),
    `${t("template.collab.tokensInput")}: ${usage.input_tokens.toLocaleString()}`,
    `${t("template.collab.tokensOutput")}: ${usage.output_tokens.toLocaleString()}`,
  ];
  if (usage.cache_read_input_tokens > 0) {
    lines.push(`${t("template.collab.tokensCacheRead")}: ${usage.cache_read_input_tokens.toLocaleString()}`);
  }
  if (usage.cache_creation_input_tokens > 0) {
    lines.push(`${t("template.collab.tokensCacheCreate")}: ${usage.cache_creation_input_tokens.toLocaleString()}`);
  }
  if (usage.reasoning_output_tokens > 0) {
    lines.push(`Reasoning: ${usage.reasoning_output_tokens.toLocaleString()}`);
  }
  const total = usage.total_tokens || generationUsageTotal(usage);
  lines.push(`Total: ${total.toLocaleString()}`);
  return lines.join("\n");
}

function linkGenerationSubagentMessages(messages: GenerationAgentMessage[]): GenerationAgentMessage[] {
  const subagentByToolUseId = new Map<string, string>();
  messages.forEach((message) => {
    if (message.toolUseId && message.subagentId) {
      subagentByToolUseId.set(message.toolUseId, message.subagentId);
    }
  });
  return messages.map((message) => {
    if (message.subagentId || !message.toolUseId) {
      return message;
    }
    const subagentId = subagentByToolUseId.get(message.toolUseId);
    return subagentId ? { ...message, subagentId } : message;
  });
}

function generationSubagentChannels(messages: GenerationAgentMessage[]) {
  const byId = new Map<string, { id: string; detail: string }>();
  messages.forEach((message) => {
    if (!message.subagentId) return;
    byId.set(message.subagentId, {
      id: message.subagentId,
      detail: describeGenerationTool(message),
    });
  });
  return Array.from(byId.values());
}

function generationActivityItems(
  messages: GenerationAgentMessage[],
  t: (key: string) => string,
  forceCloseToolState?: "done" | "error",
): GenerationAgentActivity[] {
  const out: GenerationAgentActivity[] = [];
  let pendingTools: GenerationAgentMessage[] = [];
  const flushTools = () => {
    if (!pendingTools.length) return;
    const children = normalizeGenerationToolEvents(pendingTools, forceCloseToolState);
    const visibleTools = children.filter((message) => message.status === "running");
    const last =
      visibleTools[visibleTools.length - 1] ??
      children[children.length - 1] ??
      pendingTools[pendingTools.length - 1];
    const hasRunning = visibleTools.length > 0;
    out.push({
      id: `tools:${pendingTools[0].toolUseId ?? pendingTools[0].id}`,
      kind: "group",
      label: hasRunning
          ? t("template.collab.steps")
          : t("template.collab.stepsDone"),
      detail: describeGenerationTool(last),
      state: children.some((message) => message.status === "error") ? "error" : hasRunning ? "active" : "done",
      count: children.length,
      children,
    });
    pendingTools = [];
  };
  messages.forEach((message) => {
    if (message.kind === "status" || message.kind === "usage") {
      return;
    }
    if (message.kind === "tool") {
      pendingTools.push(message);
      return;
    }
    flushTools();
    out.push({
      id: message.id,
      kind: "message",
      role: message.role,
      label: message.role === "user" ? t("template.chatUser") : "Agent",
      detail: message.content,
      state: message.status === "error" ? "error" : message.status === "complete" ? "done" : "info",
    });
  });
  flushTools();
  return out.slice(-60);
}

function normalizeGenerationToolEvents(
  messages: GenerationAgentMessage[],
  forceCloseToolState?: "done" | "error",
): GenerationAgentMessage[] {
  const rows: GenerationAgentMessage[] = [];
  const openById = new Map<string, number>();
  const openWithoutId: number[] = [];

  for (const message of messages) {
    const isResult =
      (message.status === "complete" || message.status === "error") &&
      (!message.tool || message.content === "Tool result");
    if (isResult) {
      let targetIndex: number | undefined;
      if (message.toolUseId) {
        targetIndex = openById.get(message.toolUseId);
      }
      if (targetIndex === undefined) {
        targetIndex = openWithoutId.shift();
      }
      if (targetIndex !== undefined && rows[targetIndex]) {
        rows[targetIndex] = {
          ...rows[targetIndex],
          status: message.status,
          data: {
            ...(rows[targetIndex].data ?? {}),
            result: message.data,
          },
        };
        continue;
      }
      if (message.status === "error") {
        rows.push(message);
      }
      continue;
    }

    const rowIndex = rows.length;
    rows.push(message);
    if (message.status === "running") {
      if (message.toolUseId) {
        openById.set(message.toolUseId, rowIndex);
      } else {
        openWithoutId.push(rowIndex);
      }
    }
  }

  if (forceCloseToolState) {
    return rows.map((row) =>
      row.status === "running"
        ? { ...row, status: forceCloseToolState === "done" ? "complete" : "error" }
        : row,
    );
  }
  return rows;
}

function describeGenerationTool(message: GenerationAgentMessage): string {
  const tool = message.tool || "";
  const input = message.data?.input;
  if (input && typeof input === "object") {
    const obj = input as Record<string, unknown>;
    const command = typeof obj.command === "string" ? obj.command : "";
    const path = typeof obj.file_path === "string" ? obj.file_path : typeof obj.path === "string" ? obj.path : "";
    const prompt = typeof obj.prompt === "string" ? obj.prompt : "";
    const value = command || path.replace(/\\/g, "/").split("/").slice(-2).join("/") || prompt;
    return [tool, value].filter(Boolean).join(" ").slice(0, 140);
  }
  return message.content;
}

function formatGenerationTokens(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
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
      )) : null}
    </div>
  );
}

function SlideWorkspace({
  jobId,
  job,
  slides,
  isGenerating,
  loading,
}: {
  jobId?: string;
  job?: JobStatus;
  slides?: PreviewSlide[];
  isGenerating: boolean;
  loading?: boolean;
}) {
  const { t } = useLocale();
  const safeSlides = Array.isArray(slides) ? slides : [];
  const slideSignature = safeSlides.map((slide) => slide.index).join(":");
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const canOpenPptPreview = Boolean(
    jobId && (job?.output_path || job?.status === "complete" || job?.status === "error" || job?.status === "cancelled"),
  );
  const hostKey = `generate:${jobId ?? "empty"}:${canOpenPptPreview ? "preview" : "pending"}:${safeSlides.length}:${job?.slides_completed ?? 0}`;

  useEffect(() => {
    setSelectedIndex((current) => {
      if (!safeSlides.length) return null;
      if (current && safeSlides.some((slide) => slide.index === current)) return current;
      return safeSlides[safeSlides.length - 1]?.index ?? safeSlides[0].index;
    });
  }, [slideSignature, safeSlides.length]);

  if (canOpenPptPreview && jobId) {
    return (
      <main className="slide-workspace-panel result-pptist-panel generate-pptist-panel">
        <PptistStudioHost
          key={hostKey}
          source={{ kind: "preview", jobId }}
          className="pptist-generate-host"
        />
      </main>
    );
  }

  const lastSlideIndex = safeSlides.length > 0 ? Math.max(...safeSlides.map((slide) => slide.index)) : 0;
  const expectedSlides = Math.max(job?.total_slides ?? 0, lastSlideIndex, safeSlides.length, isGenerating ? 1 : 0);
  const thumbnailCount = Math.max(1, expectedSlides || 1);
  const slideByIndex = new Map(safeSlides.map((slide) => [slide.index, slide]));
  const selectedSlide = selectedIndex ? slideByIndex.get(selectedIndex) : safeSlides[safeSlides.length - 1];
  const message = loading || isGenerating ? t("monitor.waiting") : t("preview.emptyState");

  return (
    <main className="slide-workspace-panel generate-pptist-panel">
      <div className="generate-pptist-empty-shell">
        <DisabledPptistWorkbenchHeader />
        <div className="generate-pptist-empty-rail">
          {Array.from({ length: thumbnailCount }).map((_, index) => (
            <button
              type="button"
              key={index}
              className={`generate-pptist-empty-thumb ${selectedSlide?.index === index + 1 || (!selectedSlide && index === 0) ? "generate-pptist-empty-thumb-active" : ""}`}
              disabled={!slideByIndex.has(index + 1)}
              onClick={() => setSelectedIndex(index + 1)}
            >
              <span>{String(index + 1).padStart(2, "0")}</span>
              <div className={!slideByIndex.has(index + 1) && (loading || isGenerating) ? "motion-skeleton" : ""}>
                {slideByIndex.has(index + 1) ? (
                  <div dangerouslySetInnerHTML={{ __html: slideByIndex.get(index + 1)?.content ?? "" }} />
                ) : null}
              </div>
            </button>
          ))}
        </div>
        <div className="generate-pptist-empty-body">
          <div className="generate-pptist-empty-canvas">
            {selectedSlide ? (
              <div className="generate-pptist-live-slide" dangerouslySetInnerHTML={{ __html: selectedSlide.content }} />
            ) : (
              <span>{message}</span>
            )}
          </div>
          <label className="generate-pptist-empty-notes">
            <span>{selectedSlide?.notes || t("preview.notesPlaceholder")}</span>
            <em>0 / 1000</em>
          </label>
        </div>
      </div>
    </main>
  );
}

function DisabledPptistWorkbenchHeader() {
  const { t } = useLocale();
  return (
    <div className="generate-pptist-disabled-header" aria-disabled="true">
      <div className="generate-pptist-disabled-title" aria-hidden="true" />

      <div className="generate-pptist-disabled-tool">
        <div className="generate-pptist-disabled-left-tools">
          <DisabledToolbarButton icon={<Undo2 size={16} />} label={t("editor.undo")} compact />
          <DisabledToolbarButton icon={<Redo2 size={16} />} label={t("editor.redo")} compact />
          <span className="generate-pptist-disabled-divider" />
          <DisabledToolbarButton icon={<MoreHorizontal size={16} />} label={t("pptist.more")} compact />
          <DisabledToolbarButton icon={<MessageSquareText size={16} />} label={t("pptist.comments")} compact />
          <DisabledToolbarButton icon={<MousePointer2 size={16} />} label={t("pptist.selectionPane")} compact />
          <DisabledToolbarButton icon={<Search size={16} />} label={t("pptist.searchReplace")} compact />
        </div>

        <div className="generate-pptist-disabled-insert-tools">
          <DisabledToolbarButton icon={<Type size={16} />} label={t("pptist.textbox")} caret />
          <DisabledToolbarButton icon={<Square size={16} />} label={t("editor.shapeTool")} caret />
          <DisabledToolbarButton icon={<ImageIcon size={16} />} label={t("editor.pictureTool")} caret />
          <DisabledToolbarButton icon={<Minus size={16} />} label={t("pptist.line")} />
          <DisabledToolbarButton icon={<BarChart3 size={16} />} label={t("pptist.chart")} />
          <DisabledToolbarButton icon={<Table2 size={16} />} label={t("editor.tableTool")} />
          <DisabledToolbarButton icon={<span className="generate-pptist-sigma">Σ</span>} label={t("pptist.formula")} />
          <DisabledToolbarButton icon={<Video size={16} />} label={t("pptist.media")} />
          <DisabledToolbarButton icon={<Omega size={16} />} label={t("pptist.symbol")} />
        </div>

        <div className="generate-pptist-disabled-right-tools">
          <DisabledToolbarButton icon={<Minus size={15} />} label={t("pptist.zoomOut")} compact />
          <span className="generate-pptist-disabled-scale">100%</span>
          <DisabledToolbarButton icon={<Plus size={15} />} label={t("pptist.zoomIn")} compact />
          <DisabledToolbarButton icon={<Maximize2 size={15} />} label={t("editor.fit")} compact />
        </div>
      </div>

      <div className="generate-pptist-disabled-actions">
        <DisabledToolbarButton icon={<Save size={16} />} label={t("editor.save")} />
        <DisabledToolbarButton icon={<Play size={16} />} label={t("preview.slideshow")} caret compact />
        <span className="generate-pptist-disabled-divider" />
        <DisabledToolbarButton icon={<Download size={16} />} label={t("result.download")} caret className="generate-pptist-disabled-primary" />
        <DisabledToolbarButton icon={<Eye size={16} />} label={t("pptist.properties")} />
        <a
          className="generate-pptist-disabled-github"
          href="https://github.com/pipipi-pikachu/PPTist"
          target="_blank"
          rel="noreferrer"
          aria-label="PPTist by pipipi-pikachu"
        >
          <GitHubMark />
        </a>
      </div>
    </div>
  );
}

function DisabledToolbarButton({
  icon,
  label,
  caret,
  compact,
  className,
}: {
  icon: ReactNode;
  label: string;
  caret?: boolean;
  compact?: boolean;
  className?: string;
}) {
  return (
    <GenerationTooltip content={label}>
      <span className="generation-tooltip-trigger">
        <button
          type="button"
          className={`generate-pptist-disabled-button ${compact ? "generate-pptist-disabled-button-compact" : ""} ${className ?? ""}`}
          disabled
          aria-label={label}
        >
          {icon}
          {!compact ? <span>{label}</span> : null}
          {caret ? <ChevronDown size={13} /> : null}
        </button>
      </span>
    </GenerationTooltip>
  );
}

function GitHubMark() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="17"
      height="17"
      aria-hidden="true"
      focusable="false"
      fill="currentColor"
    >
      <path d="M12 2C6.48 2 2 6.58 2 12.21c0 4.51 2.87 8.33 6.84 9.68.5.09.68-.22.68-.49 0-.24-.01-.88-.01-1.73-2.78.62-3.37-1.37-3.37-1.37-.45-1.18-1.11-1.49-1.11-1.49-.91-.63.07-.62.07-.62 1 .07 1.53 1.05 1.53 1.05.89 1.56 2.34 1.11 2.91.85.09-.66.35-1.11.63-1.37-2.22-.26-4.56-1.14-4.56-5.06 0-1.12.39-2.03 1.03-2.75-.1-.26-.45-1.3.1-2.71 0 0 .84-.27 2.75 1.05A9.29 9.29 0 0 1 12 6.91c.85 0 1.71.12 2.51.34 1.9-1.32 2.74-1.05 2.74-1.05.55 1.41.2 2.45.1 2.71.64.72 1.03 1.63 1.03 2.75 0 3.93-2.34 4.8-4.57 5.05.36.32.68.94.68 1.9 0 1.37-.01 2.47-.01 2.81 0 .27.18.59.69.49A10.15 10.15 0 0 0 22 12.21C22 6.58 17.52 2 12 2Z" />
    </svg>
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
