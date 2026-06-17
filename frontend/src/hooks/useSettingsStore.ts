/**
 * Unified settings store.
 *
 * Previously, model routing profiles, presentation draft options, and deep-
 * research API keys each lived in their own localStorage key, read and written
 * by ad-hoc helpers in GeneratePage, ResultPage, ImageSearchPanel and
 * TemplatesPage. That made the data hard to keep consistent (ResultPage could
 * not read a key written by GeneratePage if the shape drifted) and produced
 * confusing "configure your model again" errors.
 *
 * This single zustand store is now the source of truth. On first load it
 * migrates any data found under the legacy keys into one combined payload and
 * then removes the legacy keys, so the consolidation is transparent.
 */

import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import type { DeepSeekSettings, OpenAISettings, ResearchConfig } from "../lib/types";

// ── Legacy keys (consumed during migration, then deleted) ───────────────────
const LEGACY_ROUTING_KEY = "paper-ppt-agent-routing-profiles-v1";
const LEGACY_PRESENTATION_KEY = "paper-ppt-agent-presentation-settings-v1";
const LEGACY_RESEARCH_KEY = "paper-ppt-agent-research-keys";

const STORE_VERSION = 1;

export interface RoutingProfile {
  model: string;
  baseUrl: string;
  apiKey: string;
  artifactThinkingMode?: "disabled" | "default";
  deepseekSettings?: DeepSeekSettings;
  openaiSettings?: OpenAISettings;
}
export type RoutingProfileMap = Record<string, RoutingProfile>;

export interface PresentationSettingsDraft {
  generationBackend?: "provider" | "agent";
  agentRuntime?: "claude_code" | "codex";
  agentModel?: string;
  canvasFormat?: string;
  languageMode?: "zh" | "en" | "custom";
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

export interface ResearchKeys {
  web_search_provider?: "tavily" | "serpapi";
  semantic_scholar_api_key?: string;
  tavily_api_key?: string;
  serpapi_key?: string;
}

interface SettingsState {
  routingProfiles: RoutingProfileMap;
  presentation: PresentationSettingsDraft;
  researchKeys: ResearchKeys;
  setRoutingProfiles: (profiles: RoutingProfileMap | ((prev: RoutingProfileMap) => RoutingProfileMap)) => void;
  setRoutingProfile: (provider: string, profile: RoutingProfile) => void;
  setPresentation: (draft: PresentationSettingsDraft | ((prev: PresentationSettingsDraft) => PresentationSettingsDraft)) => void;
  setResearchKeys: (keys: ResearchKeys | ((prev: ResearchKeys) => ResearchKeys)) => void;
}

function parseJSON<T>(raw: string | null, fallback: T): T {
  if (!raw) return fallback;
  try {
    const parsed = JSON.parse(raw);
    return (parsed && typeof parsed === "object" ? parsed : fallback) as T;
  } catch {
    return fallback;
  }
}

/** Read legacy keys once and merge them into the unified shape. */
function migrateLegacy(): Partial<SettingsState> {
  if (typeof window === "undefined") return {};
  const migrated: Partial<SettingsState> = {};
  let touched = false;

  const legacyRouting = window.localStorage.getItem(LEGACY_ROUTING_KEY);
  if (legacyRouting) {
    const profiles = parseJSON<RoutingProfileMap>(legacyRouting, {});
    if (Object.keys(profiles).length) {
      migrated.routingProfiles = profiles;
      touched = true;
    }
    try { window.localStorage.removeItem(LEGACY_ROUTING_KEY); } catch { /* noop */ }
  }

  const legacyPresentation = window.localStorage.getItem(LEGACY_PRESENTATION_KEY);
  if (legacyPresentation) {
    const draft = parseJSON<PresentationSettingsDraft>(legacyPresentation, {});
    if (Object.keys(draft).length) {
      migrated.presentation = draft;
      touched = true;
    }
    try { window.localStorage.removeItem(LEGACY_PRESENTATION_KEY); } catch { /* noop */ }
  }

  const legacyResearch = window.localStorage.getItem(LEGACY_RESEARCH_KEY);
  if (legacyResearch) {
    const keys = parseJSON<ResearchKeys>(legacyResearch, {});
    if (Object.keys(keys).length) {
      migrated.researchKeys = keys;
      touched = true;
    }
    try { window.localStorage.removeItem(LEGACY_RESEARCH_KEY); } catch { /* noop */ }
  }

  return touched ? migrated : {};
}

export const useSettingsStore = create<SettingsState>()(
  persist(
    (set) => ({
      routingProfiles: {},
      presentation: {},
      researchKeys: {},
      setRoutingProfiles: (profiles) =>
        set((state) => ({
          routingProfiles:
            typeof profiles === "function" ? profiles(state.routingProfiles) : profiles,
        })),
      setRoutingProfile: (provider, profile) =>
        set((state) => ({
          routingProfiles: { ...state.routingProfiles, [provider]: profile },
        })),
      setPresentation: (draft) =>
        set((state) => ({
          presentation:
            typeof draft === "function" ? draft(state.presentation) : draft,
        })),
      setResearchKeys: (keys) =>
        set((state) => ({
          researchKeys: typeof keys === "function" ? keys(state.researchKeys) : keys,
        })),
    }),
    {
      name: "paper-ppt-agent-settings-v1",
      version: STORE_VERSION,
      storage: createJSONStorage(() => window.localStorage),
      // Run the legacy-key migration before hydrating from our own key, so a
      // first upgrade carries old data forward.
      onRehydrateStorage: () => (state) => {
        if (!state) return;
        const legacy = migrateLegacy();
        if (legacy.routingProfiles) {
          state.routingProfiles = { ...legacy.routingProfiles, ...state.routingProfiles };
        }
        if (legacy.presentation) {
          state.presentation = { ...legacy.presentation, ...state.presentation };
        }
        if (legacy.researchKeys) {
          state.researchKeys = { ...legacy.researchKeys, ...state.researchKeys };
        }
      },
    },
  ),
);

/**
 * Read a routing profile for a provider with optional model/baseUrl overrides,
 * mirroring ResultPage's old readProviderProfile helper. Returns null when no
 * API key is configured for the provider.
 */
export function readProviderProfile(
  provider: string,
  defaults?: { model?: string; baseUrl?: string },
): {
  provider: string;
  model: string;
  apiKey: string;
  baseUrl: string;
  artifactThinkingMode: "disabled" | "default";
  deepseekSettings?: DeepSeekSettings;
  openaiSettings?: OpenAISettings;
} | null {
  const profile = useSettingsStore.getState().routingProfiles[provider];
  if (!profile?.apiKey) return null;
  return {
    provider,
    model: defaults?.model || profile.model,
    apiKey: profile.apiKey,
    baseUrl: defaults?.baseUrl || profile.baseUrl,
    artifactThinkingMode: profile.artifactThinkingMode ?? "disabled",
    deepseekSettings: profile.deepseekSettings,
    openaiSettings: profile.openaiSettings,
  };
}
