import { useState } from "react";
import type { DeepSeekSettings, OpenAISettings, ProviderListItem } from "../../lib/types";
import { useLocale } from "../../i18n";
import { Bot, Cpu, Zap, Globe, Key, Eye, EyeOff, BrainCircuit } from "lucide-react";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select";
import { Switch } from "../ui/switch";

interface ModelSelectorProps {
  providers: ProviderListItem[];
  provider: string;
  model: string;
  baseUrl: string;
  apiKey: string;
  deepSeekSettings: DeepSeekSettings;
  openAISettings: OpenAISettings;
  onProviderChange: (provider: string) => void;
  onModelChange: (model: string) => void;
  onBaseUrlChange: (baseUrl: string) => void;
  onApiKeyChange: (apiKey: string) => void;
  onDeepSeekSettingsChange: (settings: DeepSeekSettings) => void;
  onOpenAISettingsChange: (settings: OpenAISettings) => void;
}

export function ModelSelector({
  providers,
  provider,
  model,
  baseUrl,
  apiKey,
  deepSeekSettings,
  openAISettings,
  onProviderChange,
  onModelChange,
  onBaseUrlChange,
  onApiKeyChange,
  onDeepSeekSettingsChange,
  onOpenAISettingsChange,
}: ModelSelectorProps) {
  const selectedProvider = providers.find((item) => item.name === provider);
  const { t } = useLocale();
  const datalistId = `model-options-${provider || "default"}`;
  const [showKey, setShowKey] = useState(false);
  const isDeepSeek = provider === "deepseek";
  const showOpenAISettings = provider === "openai" && isGpt5OrNewer(model);
  const selectedProviderIcon = getProviderIcon(provider, selectedProvider?.display_name);

  return (
    <section className="panel">
      <div className="panel-header-row">
        <div>
          <div className="panel-title-row">
            <Bot size={15} className="panel-title-icon" />
            <p className="panel-title">{t("model.title")}</p>
          </div>
          <p className="panel-support-text">{selectedProvider?.display_name ?? t("model.waiting")}</p>
        </div>
      </div>

      <label className="form-field">
        <span>{t("model.provider")}</span>
        <div className="provider-select-shell">
          <Select value={provider} onValueChange={onProviderChange}>
            <SelectTrigger className="provider-select-trigger">
              <span className="provider-select-value">
                <ProviderIcon icon={selectedProviderIcon} label={selectedProvider?.display_name ?? provider} />
                <span>{selectedProvider?.display_name ?? (provider || t("model.waiting"))}</span>
              </span>
            </SelectTrigger>
            <SelectContent>
            {providers.map((item) => (
              <SelectItem key={item.name} value={item.name}>
                <span className="provider-select-item">
                  <ProviderIcon icon={getProviderIcon(item.name, item.display_name)} label={item.display_name} />
                  <span>{item.display_name}</span>
                </span>
              </SelectItem>
            ))}
            </SelectContent>
          </Select>
        </div>
      </label>

      <label className="form-field">
        <span>{t("model.model")}</span>
        <div className="form-field-icon">
          <Zap size={14} className="field-icon" />
          <Input
            list={datalistId}
            value={model}
            className="pl-9"
            placeholder={t("model.modelPlaceholder")}
            onChange={(event) => onModelChange(event.target.value)}
          />
        </div>
        <datalist id={datalistId}>
          {selectedProvider?.models.map((item) => (
            <option key={item.id} value={item.id}>
              {item.display_name}
            </option>
          ))}
        </datalist>
      </label>

      <label className="form-field">
        <span>{t("model.baseUrl")}</span>
        <div className="form-field-icon">
          <Globe size={14} className="field-icon" />
          <Input
            type="url"
            className="pl-9"
            placeholder={t("model.baseUrlPlaceholder")}
            value={baseUrl}
            onChange={(event) => onBaseUrlChange(event.target.value)}
          />
        </div>
      </label>

      <label className="form-field">
        <span>{t("model.apiKey")}</span>
        <div className="form-field-icon api-key-wrapper">
          <Key size={14} className="field-icon" />
          <Input
            type={showKey ? "text" : "password"}
            className="pl-9 pr-10"
            placeholder={t("model.apiPlaceholder")}
            value={apiKey}
            onChange={(event) => onApiKeyChange(event.target.value)}
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="api-key-toggle"
            onClick={() => setShowKey((v) => !v)}
            tabIndex={-1}
          >
            {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
          </Button>
        </div>
      </label>

      {isDeepSeek ? (
        <div className="deepseek-settings">
          <div className="panel-title-row">
            <BrainCircuit size={15} className="panel-title-icon" />
            <p className="panel-title">{t("model.deepseekTitle")}</p>
          </div>
          <p className="panel-support-text">{t("model.deepseekBody")}</p>

          <div className="model-subsettings-grid model-subsettings-grid-deepseek">
          <label className="visual-qa-field deepseek-toggle-row">
            <span className={`visual-qa-control ${deepSeekSettings.thinking_enabled ? "visual-qa-control-active" : ""}`}>
              <span className="visual-qa-icon" aria-hidden="true">
                <BrainCircuit size={16} />
              </span>
              <span className="visual-qa-copy">
                <span className="visual-qa-name">{t("model.deepseekThinking")}</span>
              </span>
              <span />
              <Switch
                checked={deepSeekSettings.thinking_enabled}
                onCheckedChange={(checked) =>
                  onDeepSeekSettingsChange({
                    ...deepSeekSettings,
                    thinking_enabled: checked,
                  })
                }
              />
            </span>
          </label>

          <label className="form-field">
            <span>{t("model.deepseekEffort")}</span>
            <div className="form-field-icon">
              <BrainCircuit size={14} className="field-icon" />
              <Select
                value={deepSeekSettings.reasoning_effort}
                disabled={!deepSeekSettings.thinking_enabled}
                onValueChange={(value) =>
                  onDeepSeekSettingsChange({
                    ...deepSeekSettings,
                    reasoning_effort: value as DeepSeekSettings["reasoning_effort"],
                  })
                }
              >
                <SelectTrigger className="pl-9">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="high">{t("model.deepseekEffortHigh")}</SelectItem>
                  <SelectItem value="max">{t("model.deepseekEffortMax")}</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </label>
          </div>
        </div>
      ) : null}

      {showOpenAISettings ? (
        <div className="deepseek-settings">
          <div className="panel-title-row">
            <BrainCircuit size={15} className="panel-title-icon" />
            <p className="panel-title">{t("model.openaiTitle")}</p>
          </div>

          <div className="model-subsettings-grid">
          <label className="form-field">
            <span>{t("model.openaiReasoning")}</span>
            <div className="form-field-icon">
              <BrainCircuit size={14} className="field-icon" />
              <Select
                value={openAISettings.reasoning_effort}
                onValueChange={(value) =>
                  onOpenAISettingsChange({
                    ...openAISettings,
                    reasoning_effort: value as OpenAISettings["reasoning_effort"],
                  })
                }
              >
                <SelectTrigger className="pl-9">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">{t("model.openaiReasoningNone")}</SelectItem>
                  <SelectItem value="low">{t("model.openaiReasoningLow")}</SelectItem>
                  <SelectItem value="medium">{t("model.openaiReasoningMedium")}</SelectItem>
                  <SelectItem value="high">{t("model.openaiReasoningHigh")}</SelectItem>
                  <SelectItem value="xhigh">{t("model.openaiReasoningXhigh")}</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </label>

          <label className="form-field">
            <span>{t("model.openaiVerbosity")}</span>
            <div className="form-field-icon">
              <Zap size={14} className="field-icon" />
              <Select
                value={openAISettings.verbosity}
                onValueChange={(value) =>
                  onOpenAISettingsChange({
                    ...openAISettings,
                    verbosity: value as OpenAISettings["verbosity"],
                  })
                }
              >
                <SelectTrigger className="pl-9">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="low">{t("model.openaiVerbosityLow")}</SelectItem>
                  <SelectItem value="medium">{t("model.openaiVerbosityMedium")}</SelectItem>
                  <SelectItem value="high">{t("model.openaiVerbosityHigh")}</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </label>
          </div>
        </div>
      ) : null}
    </section>
  );
}

type ProviderIconKind = "openai" | "deepseek" | "claude" | "gemini" | "default";

const PROVIDER_ICON_SRC: Record<Exclude<ProviderIconKind, "default">, string> = {
  openai: "/provider-icons/openai.svg",
  deepseek: "/provider-icons/deepseek.svg",
  claude: "/provider-icons/claude.svg",
  gemini: "/provider-icons/gemini.svg",
};

function getProviderIcon(name?: string, displayName?: string): ProviderIconKind {
  const normalized = `${name ?? ""} ${displayName ?? ""}`.toLowerCase();
  if (normalized.includes("deepseek")) return "deepseek";
  if (normalized.includes("claude") || normalized.includes("anthropic")) return "claude";
  if (normalized.includes("gemini") || normalized.includes("google")) return "gemini";
  if (normalized.includes("openai") || normalized.includes("chatgpt") || normalized.includes("gpt")) return "openai";
  return "default";
}

function ProviderIcon({ icon, label }: { icon: ProviderIconKind; label?: string }) {
  if (icon === "default") {
    return (
      <span className="provider-icon provider-icon-fallback" aria-hidden="true" title={label}>
        <Cpu size={14} />
      </span>
    );
  }
  return (
    <span className="provider-icon" aria-hidden="true" title={label}>
      <img src={PROVIDER_ICON_SRC[icon]} alt="" />
    </span>
  );
}

function isGpt5OrNewer(model: string) {
  const normalized = model.trim().toLowerCase();
  if (!normalized.startsWith("gpt-")) {
    return false;
  }
  const version = normalized.slice(4).split("-", 1)[0];
  const parsed = Number.parseFloat(version);
  return Number.isFinite(parsed) ? parsed >= 5 : normalized.startsWith("gpt-5");
}
