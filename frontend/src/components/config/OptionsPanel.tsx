import { useState } from "react";
import { BookOpen, Eye, EyeOff, FlaskConical, HelpCircle, Key, Layers, Puzzle, Search, Settings2, Sparkles } from "lucide-react";
import { useLocale } from "../../i18n";
import type { ResearchConfig, TemplateInfo } from "../../lib/types";
import { FontSelector } from "./FontSelector";
import { Button } from "../ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select";
import { Switch } from "../ui/switch";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "../ui/tooltip";

interface OptionsPanelProps {
  canvasFormat: string;
  languageMode: "zh" | "en" | "custom";
  customLanguage: string;
  numPages: string;
  detailLevel: string;
  generationMode: "sequential" | "chapter_parallel" | "page_parallel";
  parallelConcurrency: string;
  timeoutSeconds: string;
  maxCriticAttempts: string;
  visualQaMaxAttempts: string;
  instruction: string;
  enableDeepResearch: boolean;
  enableVisualCritic: boolean;
  enableIcon: boolean;
  enableIconRag: boolean;
  geminiApiKey: string;
  templateId: string;
  templates: TemplateInfo[];
  density: string;
  customFont: string;
  headingFont: string;
  bodyFont: string;
  cjkHeadingFont: string;
  cjkBodyFont: string;
  researchConfig: ResearchConfig;
  onCanvasFormatChange: (value: string) => void;
  onLanguageModeChange: (value: "zh" | "en" | "custom") => void;
  onCustomLanguageChange: (value: string) => void;
  onNumPagesChange: (value: string) => void;
  onDetailLevelChange: (value: string) => void;
  onGenerationModeChange: (value: "sequential" | "chapter_parallel" | "page_parallel") => void;
  onParallelConcurrencyChange: (value: string) => void;
  onTimeoutSecondsChange: (value: string) => void;
  onMaxCriticAttemptsChange: (value: string) => void;
  onVisualQaMaxAttemptsChange: (value: string) => void;
  onInstructionChange: (value: string) => void;
  onEnableDeepResearchChange: (value: boolean) => void;
  onEnableVisualCriticChange: (value: boolean) => void;
  onEnableIconChange: (value: boolean) => void;
  onEnableIconRagChange: (value: boolean) => void;
  onGeminiApiKeyChange: (value: string) => void;
  onTemplateChange: (value: string) => void;
  onDensityChange: (value: string) => void;
  onCustomFontChange: (value: string) => void;
  onHeadingFontChange: (value: string) => void;
  onBodyFontChange: (value: string) => void;
  onCjkHeadingFontChange: (value: string) => void;
  onCjkBodyFontChange: (value: string) => void;
  onResearchConfigChange: (config: ResearchConfig) => void;
}

export function OptionsPanel(props: OptionsPanelProps) {
  const { t } = useLocale();
  const [showGeminiKey, setShowGeminiKey] = useState(false);
  const [showScholarKey, setShowScholarKey] = useState(false);
  const [showTavilyKey, setShowTavilyKey] = useState(false);
  const [showSerpApiKey, setShowSerpApiKey] = useState(false);
  return (
    <section className="panel">
      <div className="panel-title-row" style={{ marginBottom: "0.75rem" }}>
        <Settings2 size={15} className="panel-title-icon" />
        <p className="panel-title">{t("options.title")}</p>
      </div>
      <div className="options-grid">
        <label className="form-field">
          <span>
            <Layers size={12} style={{ marginRight: 4, verticalAlign: "middle" }} />
            {t("options.template")}
          </span>
          <Select
            value={props.templateId || "__none__"}
            onValueChange={(value) => {
              props.onTemplateChange(value === "__none__" ? "" : value);
            }}
          >
            <SelectTrigger className="template-select-trigger">
              <SelectValue />
            </SelectTrigger>
            <SelectContent className="template-select-content" viewportClassName="template-select-viewport">
              <SelectItem value="__none__">{t("options.templateNone")}</SelectItem>
              {props.templates.map((tmpl) => (
                <SelectItem key={tmpl.template_id} value={tmpl.template_id}>
                  {tmpl.label || tmpl.template_id}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
        <label className="form-field">
          <span>{t("options.canvas")}</span>
          <Select value={props.canvasFormat} onValueChange={props.onCanvasFormatChange}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="ppt169">{t("options.canvas169")}</SelectItem>
              <SelectItem value="ppt43">{t("options.canvas43")}</SelectItem>
            </SelectContent>
          </Select>
        </label>
        <label className="form-field">
          <span>{t("options.language")}</span>
          <Select
            value={props.languageMode}
            onValueChange={(value) =>
              props.onLanguageModeChange(value as "zh" | "en" | "custom")
            }
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="zh">{t("options.languageZh")}</SelectItem>
              <SelectItem value="en">{t("options.languageEn")}</SelectItem>
              <SelectItem value="custom">{t("options.languageCustom")}</SelectItem>
            </SelectContent>
          </Select>
          {props.languageMode === "custom" ? (
            <input
              type="text"
              value={props.customLanguage}
              onChange={(event) => props.onCustomLanguageChange(event.target.value)}
              placeholder={t("options.languageCustomPlaceholder")}
            />
          ) : null}
        </label>
        <label className="form-field">
          <span>{t("options.pages")}</span>
          <input
            type="number"
            min="0"
            value={props.numPages}
            onChange={(event) => props.onNumPagesChange(event.target.value)}
            placeholder={t("options.auto")}
          />
        </label>
        <label className="form-field">
          <span>{t("options.detailLevel")}</span>
          <Select value={props.detailLevel} onValueChange={props.onDetailLevelChange}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="normal">{t("options.detailNormal")}</SelectItem>
              <SelectItem value="high">{t("options.detailHigh")}</SelectItem>
              <SelectItem value="very_high">{t("options.detailVeryHigh")}</SelectItem>
            </SelectContent>
          </Select>
        </label>
        <label className="form-field">
          <span>{t("options.density")}</span>
          <Select value={props.density} onValueChange={props.onDensityChange}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="compact">{t("options.densityCompact")}</SelectItem>
              <SelectItem value="normal">{t("options.densityNormal")}</SelectItem>
              <SelectItem value="spacious">{t("options.densitySpacious")}</SelectItem>
            </SelectContent>
          </Select>
        </label>
        <label className="form-field">
          <span>{t("options.timeout")}</span>
          <input
            type="number"
            min="1"
            value={props.timeoutSeconds}
            onChange={(event) => props.onTimeoutSecondsChange(event.target.value)}
            placeholder={t("options.timeoutPlaceholder")}
          />
        </label>
        <label className="form-field">
          <span>
            {t("options.maxCriticAttempts")}
            <ConfigHelp text={t("options.maxCriticAttemptsTooltip")} />
          </span>
          <input
            type="number"
            min="0"
            max="10"
            value={props.maxCriticAttempts}
            onChange={(event) => props.onMaxCriticAttemptsChange(event.target.value)}
          />
        </label>
        <label className="visual-qa-field visual-qa-field-wide">
          <span
            className={`visual-qa-control ${
              props.enableVisualCritic ? "visual-qa-control-active" : ""
            }`}
          >
            <span className="visual-qa-icon" aria-hidden="true">
              <Eye size={16} />
            </span>
            <span className="visual-qa-copy">
              <span className="visual-qa-name">{t("options.visualCritic")}</span>
            </span>
            <ConfigHelp text={t("options.visualCriticTooltip")} />
            <Switch checked={props.enableVisualCritic} onCheckedChange={props.onEnableVisualCriticChange} />
          </span>
        </label>
        {props.enableVisualCritic ? (
          <div className="options-icon-sub visual-qa-attempts-sub">
            <label className="visual-qa-field visual-qa-child-field visual-qa-attempts-field">
              <span className="visual-qa-control visual-qa-number-control">
                <span className="visual-qa-icon" aria-hidden="true">
                  <Eye size={16} />
                </span>
                <span className="visual-qa-copy">
                  <span className="visual-qa-name">{t("options.visualQaMaxAttempts")}</span>
                </span>
                <input
                  className="visual-qa-number-input"
                  type="number"
                  min="0"
                  max="10"
                  value={props.visualQaMaxAttempts}
                  onChange={(event) => props.onVisualQaMaxAttemptsChange(event.target.value)}
                />
              </span>
            </label>
          </div>
        ) : null}
      </div>

      {/* Deep research section */}
      <div className="options-icon-section">
        <label className="visual-qa-field visual-qa-field-wide">
          <span
            className={`visual-qa-control ${
              props.enableDeepResearch ? "visual-qa-control-active" : ""
            }`}
          >
            <span className="visual-qa-icon" aria-hidden="true">
              <BookOpen size={16} />
            </span>
            <span className="visual-qa-copy">
              <span className="visual-qa-name">{t("options.deepResearch")}</span>
            </span>
            <ConfigHelp text={t("options.deepResearchTooltip")} />
            <Switch checked={props.enableDeepResearch} onCheckedChange={props.onEnableDeepResearchChange} />
          </span>
        </label>
      </div>

      <div className="options-icon-section">
        <label className="visual-qa-field visual-qa-field-wide">
          <span
            className={`visual-qa-control ${
              props.generationMode !== "sequential" ? "visual-qa-control-active" : ""
            }`}
          >
            <span className="visual-qa-icon" aria-hidden="true">
              <FlaskConical size={16} />
            </span>
            <span className="visual-qa-copy">
              <span className="visual-qa-name">{t("options.parallelGeneration")}</span>
              <span className="visual-qa-experimental parallel-experimental-badge">{t("common.experimental")}</span>
            </span>
            <ConfigHelp text={t("options.parallelGenerationTooltip")} />
            <Switch
              checked={props.generationMode !== "sequential"}
              onCheckedChange={(checked) =>
                props.onGenerationModeChange(checked ? "chapter_parallel" : "sequential")
              }
            />
          </span>
        </label>
        {props.generationMode !== "sequential" ? (
          <div className="options-icon-sub visual-qa-attempts-sub">
            <label className="form-field">
              <Select
                value={props.generationMode}
                onValueChange={(value) =>
                  props.onGenerationModeChange(value as "chapter_parallel" | "page_parallel")
                }
              >
                <SelectTrigger className="parallel-mode-trigger" aria-label={t("options.parallelMode")}>
                  <SelectValue className="parallel-mode-value" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="chapter_parallel">{t("options.parallelChapter")}</SelectItem>
                  <SelectItem value="page_parallel">{t("options.parallelPage")}</SelectItem>
                </SelectContent>
              </Select>
            </label>
            {props.generationMode === "page_parallel" ? (
              <label className="form-field">
                <span>
                  {t("options.parallelPageConcurrency")}
                  <ConfigHelp text={t("options.parallelPageConcurrencyTooltip")} />
                </span>
                <input
                  type="number"
                  min="1"
                  max="8"
                  value={props.parallelConcurrency}
                  onChange={(event) => props.onParallelConcurrencyChange(event.target.value)}
                />
              </label>
            ) : null}
          </div>
        ) : null}
      </div>

      {/* Icon section */}
      <div className="options-icon-section">
        <label className="visual-qa-field visual-qa-field-wide">
          <span
            className={`visual-qa-control ${
              props.enableIcon ? "visual-qa-control-active" : ""
            }`}
          >
            <span className="visual-qa-icon" aria-hidden="true">
              <Puzzle size={16} />
            </span>
            <span className="visual-qa-copy">
              <span className="visual-qa-name">{t("options.enableIcon")}</span>
            </span>
            <ConfigHelp text={t("options.enableIconTooltip")} />
            <Switch
              checked={props.enableIcon}
              onCheckedChange={(checked) => {
                props.onEnableIconChange(checked);
                if (!checked) {
                  props.onEnableIconRagChange(false);
                }
              }}
            />
          </span>
        </label>

        {props.enableIcon ? (
          <div className="options-icon-sub">
            <label className="visual-qa-field visual-qa-child-field">
              <span
                className={`visual-qa-control ${
                  props.enableIconRag ? "visual-qa-control-active" : ""
                }`}
              >
                <span className="visual-qa-icon" aria-hidden="true">
                  <Puzzle size={14} />
                </span>
                <span className="visual-qa-copy">
                  <span className="visual-qa-name">{t("options.iconRag")}</span>
                </span>
                <ConfigHelp text={t("options.iconRagTooltip")} />
                <Switch checked={props.enableIconRag} onCheckedChange={props.onEnableIconRagChange} />
              </span>
            </label>
            {props.enableIconRag ? (
              <label className="form-field options-api-key-field">
                <span>
                  <Key size={12} style={{ marginRight: 4, verticalAlign: "middle" }} />
                  Gemini API Key
                </span>
                <div className="form-field-icon api-key-wrapper">
                  <Key size={14} className="field-icon" />
                  <input
                    type={showGeminiKey ? "text" : "password"}
                    value={props.geminiApiKey}
                    onChange={(event) => props.onGeminiApiKeyChange(event.target.value)}
                    placeholder="AIza..."
                  />
                  <button
                    type="button"
                    className="api-key-toggle"
                    onClick={() => setShowGeminiKey((v) => !v)}
                    tabIndex={-1}
                  >
                    {showGeminiKey ? <EyeOff size={14} /> : <Eye size={14} />}
                  </button>
                </div>
              </label>
            ) : null}
          </div>
        ) : null}
      </div>

      {/* Research Enrichment section */}
      <div className="options-icon-section research-enrichment-section">
        <label className="visual-qa-field visual-qa-field-wide">
          <span
            className={`visual-qa-control ${
              props.researchConfig.arxiv_search_enabled || props.researchConfig.semantic_scholar_enabled || props.researchConfig.web_search_enabled
                ? "visual-qa-control-active"
                : ""
            }`}
          >
            <span className="visual-qa-icon" aria-hidden="true">
              <Sparkles size={16} />
            </span>
            <span className="visual-qa-copy">
              <span className="visual-qa-name">{t("options.researchEnrichment")}</span>
            </span>
            <ConfigHelp text={t("options.researchEnrichmentTooltip")} />
            <Switch
              checked={!!(props.researchConfig.arxiv_search_enabled || props.researchConfig.semantic_scholar_enabled || props.researchConfig.web_search_enabled)}
              onCheckedChange={(enabled) => {
                props.onResearchConfigChange({
                  ...props.researchConfig,
                  // Enabling the master switch defaults to the two free academic sources.
                  // Web search remains opt-in because it requires an API key.
                  arxiv_search_enabled: enabled,
                  semantic_scholar_enabled: enabled,
                  web_search_enabled: enabled ? !!props.researchConfig.web_search_enabled : false,
                });
              }}
            />
          </span>
        </label>

        {(props.researchConfig.arxiv_search_enabled || props.researchConfig.semantic_scholar_enabled || props.researchConfig.web_search_enabled) ? (
          <div className="options-icon-sub research-sub">
            {/* arxiv toggle */}
            <label className="visual-qa-field visual-qa-child-field">
              <span
                className={`visual-qa-control ${
                  props.researchConfig.arxiv_search_enabled ? "visual-qa-control-active" : ""
                }`}
              >
                <span className="visual-qa-icon" aria-hidden="true">
                  <BookOpen size={14} />
                </span>
                <span className="visual-qa-copy">
                  <span className="visual-qa-name">{t("options.arxivSearch")}</span>
                  <span className="visual-qa-tag visual-qa-tag-free">{t("common.free")}</span>
                </span>
                <ConfigHelp text={t("options.arxivSearchTooltip")} />
                <Switch
                  checked={!!props.researchConfig.arxiv_search_enabled}
                  onCheckedChange={(checked) => {
                    props.onResearchConfigChange({
                      ...props.researchConfig,
                      arxiv_search_enabled: checked,
                    });
                  }}
                />
              </span>
            </label>

            {/* Semantic Scholar toggle */}
            <label className="visual-qa-field visual-qa-child-field">
              <span
                className={`visual-qa-control ${
                  props.researchConfig.semantic_scholar_enabled ? "visual-qa-control-active" : ""
                }`}
              >
                <span className="visual-qa-icon" aria-hidden="true">
                  <BookOpen size={14} />
                </span>
                <span className="visual-qa-copy">
                  <span className="visual-qa-name">{t("options.semanticScholar")}</span>
                  <span className="visual-qa-tag visual-qa-tag-free">{t("common.free")}</span>
                </span>
                <ConfigHelp text={t("options.semanticScholarTooltip")} />
                <Switch
                  checked={!!props.researchConfig.semantic_scholar_enabled}
                  onCheckedChange={(checked) => {
                    props.onResearchConfigChange({
                      ...props.researchConfig,
                      semantic_scholar_enabled: checked,
                    });
                  }}
                />
              </span>
            </label>
            {props.researchConfig.semantic_scholar_enabled ? (
              <div className="options-icon-sub">
                <label className="form-field options-api-key-field">
                  <span>
                    <Key size={12} style={{ marginRight: 4, verticalAlign: "middle" }} />
                    {t("options.semanticScholarApiKey")}
                  </span>
                  <div className="form-field-icon api-key-wrapper">
                    <Key size={14} className="field-icon" />
                    <input
                      type={showScholarKey ? "text" : "password"}
                      value={props.researchConfig.semantic_scholar_api_key ?? ""}
                      onChange={(event) => {
                        props.onResearchConfigChange({
                          ...props.researchConfig,
                          semantic_scholar_api_key: event.target.value || undefined,
                        });
                      }}
                      placeholder={t("options.semanticScholarApiKeyPlaceholder")}
                    />
                    <button
                      type="button"
                      className="api-key-toggle"
                      onClick={() => setShowScholarKey((v) => !v)}
                      tabIndex={-1}
                    >
                      {showScholarKey ? <EyeOff size={14} /> : <Eye size={14} />}
                    </button>
                  </div>
                </label>
              </div>
            ) : null}

            {/* Web search toggle */}
            <label className="visual-qa-field visual-qa-child-field">
              <span
                className={`visual-qa-control ${
                  props.researchConfig.web_search_enabled ? "visual-qa-control-active" : ""
                }`}
              >
                <span className="visual-qa-icon" aria-hidden="true">
                  <Search size={14} />
                </span>
                <span className="visual-qa-copy">
                  <span className="visual-qa-name">{t("options.webSearch")}</span>
                  <span className="visual-qa-tag visual-qa-tag-key">{t("common.needsKey")}</span>
                </span>
                <ConfigHelp text={t("options.webSearchTooltip")} />
                <Switch
                  checked={!!props.researchConfig.web_search_enabled}
                  onCheckedChange={(checked) => {
                    props.onResearchConfigChange({
                      ...props.researchConfig,
                      web_search_enabled: checked,
                    });
                  }}
                />
              </span>
            </label>
            {props.researchConfig.web_search_enabled ? (
              <div className="options-icon-sub">
                <div className="research-provider-select">
                  <Button
                    type="button"
                    size="sm"
                    variant={(props.researchConfig.web_search_provider ?? "tavily") === "tavily" ? "default" : "outline"}
                    className="research-provider-button"
                    onClick={() => {
                      props.onResearchConfigChange({
                        ...props.researchConfig,
                        web_search_provider: "tavily",
                      });
                    }}
                  >
                    {t("options.tavilyApiKey")}
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant={props.researchConfig.web_search_provider === "serpapi" ? "default" : "outline"}
                    className="research-provider-button"
                    onClick={() => {
                      props.onResearchConfigChange({
                        ...props.researchConfig,
                        web_search_provider: "serpapi",
                      });
                    }}
                  >
                    {t("options.serpApiKey")}
                  </Button>
                </div>
                {(props.researchConfig.web_search_provider ?? "tavily") === "tavily" ? (
                  <label className="form-field options-api-key-field">
                    <span>
                      <Key size={12} style={{ marginRight: 4, verticalAlign: "middle" }} />
                      {t("options.tavilyApiKey")}
                    </span>
                    <div className="form-field-icon api-key-wrapper">
                      <Key size={14} className="field-icon" />
                      <input
                        type={showTavilyKey ? "text" : "password"}
                        value={props.researchConfig.tavily_api_key ?? ""}
                        onChange={(event) => {
                          props.onResearchConfigChange({
                            ...props.researchConfig,
                            tavily_api_key: event.target.value || undefined,
                          });
                        }}
                        placeholder="tvly-..."
                      />
                      <button
                        type="button"
                        className="api-key-toggle"
                        onClick={() => setShowTavilyKey((v) => !v)}
                        tabIndex={-1}
                      >
                        {showTavilyKey ? <EyeOff size={14} /> : <Eye size={14} />}
                      </button>
                    </div>
                  </label>
                ) : (
                  <label className="form-field options-api-key-field">
                    <span>
                      <Key size={12} style={{ marginRight: 4, verticalAlign: "middle" }} />
                      {t("options.serpApiKey")}
                    </span>
                    <div className="form-field-icon api-key-wrapper">
                      <Key size={14} className="field-icon" />
                      <input
                        type={showSerpApiKey ? "text" : "password"}
                        value={props.researchConfig.serpapi_key ?? ""}
                        onChange={(event) => {
                          props.onResearchConfigChange({
                            ...props.researchConfig,
                            serpapi_key: event.target.value || undefined,
                          });
                        }}
                        placeholder="serp-..."
                      />
                      <button
                        type="button"
                        className="api-key-toggle"
                        onClick={() => setShowSerpApiKey((v) => !v)}
                        tabIndex={-1}
                      >
                        {showSerpApiKey ? <EyeOff size={14} /> : <Eye size={14} />}
                      </button>
                    </div>
                  </label>
                )}
                {((props.researchConfig.web_search_provider ?? "tavily") === "tavily" && !props.researchConfig.tavily_api_key) ||
                (props.researchConfig.web_search_provider === "serpapi" && !props.researchConfig.serpapi_key) ? (
                  <p className="research-warning">
                    <FlaskConical size={11} style={{ marginRight: 4, verticalAlign: "middle" }} />
                    {t("options.webSearchNoKey")}
                  </p>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>

      <label className="form-field font-section-field">
        <span>{t("options.customFont")}</span>
        <FontSelector
          value={props.customFont}
          onChange={props.onCustomFontChange}
          headingFont={props.headingFont}
          onHeadingFontChange={props.onHeadingFontChange}
          bodyFont={props.bodyFont}
          onBodyFontChange={props.onBodyFontChange}
          cjkHeadingFont={props.cjkHeadingFont}
          onCjkHeadingFontChange={props.onCjkHeadingFontChange}
          cjkBodyFont={props.cjkBodyFont}
          onCjkBodyFontChange={props.onCjkBodyFontChange}
        />
      </label>

      <label className="form-field">
        <span>{t("options.instruction")}</span>
        <textarea
          rows={4}
          value={props.instruction}
          onChange={(event) => props.onInstructionChange(event.target.value)}
          placeholder={t("options.instructionPlaceholder")}
        />
      </label>
    </section>
  );
}

function ConfigHelp({ text }: { text: string }) {
  return (
    <TooltipProvider delayDuration={120}>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className="visual-qa-help"
            aria-label={text}
            onClick={(event) => event.preventDefault()}
          >
            <HelpCircle size={14} />
          </button>
        </TooltipTrigger>
        <TooltipContent side="top" align="center" className="config-tooltip-content">
          {text}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
