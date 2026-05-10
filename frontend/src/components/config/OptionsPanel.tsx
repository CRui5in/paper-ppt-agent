import { useState } from "react";
import { BookOpen, Eye, EyeOff, FlaskConical, HelpCircle, Key, Layers, Puzzle, Search, Settings2, Sparkles } from "lucide-react";
import { useLocale } from "../../i18n";
import type { ResearchConfig, TemplateInfo } from "../../lib/types";
import { FontSelector } from "./FontSelector";

interface OptionsPanelProps {
  canvasFormat: string;
  languageMode: "zh" | "en" | "custom";
  customLanguage: string;
  numPages: string;
  detailLevel: string;
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
  onManageTemplates: () => void;
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
          <select
            value={props.templateId}
            onChange={(event) => {
              if (event.target.value === "__manage__") {
                props.onManageTemplates();
              } else {
                props.onTemplateChange(event.target.value);
              }
            }}
          >
            <option value="">{t("options.templateNone")}</option>
            {props.templates.map((tmpl) => (
              <option key={tmpl.template_id} value={tmpl.template_id}>
                {tmpl.label || tmpl.template_id}
              </option>
            ))}
            <option value="__manage__">{t("options.templateManage")}</option>
          </select>
        </label>
        <label className="form-field">
          <span>{t("options.canvas")}</span>
          <select value={props.canvasFormat} onChange={(event) => props.onCanvasFormatChange(event.target.value)}>
            <option value="ppt169">{t("options.canvas169")}</option>
            <option value="ppt43">{t("options.canvas43")}</option>
          </select>
        </label>
        <label className="form-field">
          <span>{t("options.language")}</span>
          <select
            value={props.languageMode}
            onChange={(event) =>
              props.onLanguageModeChange(event.target.value as "zh" | "en" | "custom")
            }
          >
            <option value="zh">{t("options.languageZh")}</option>
            <option value="en">{t("options.languageEn")}</option>
            <option value="custom">{t("options.languageCustom")}</option>
          </select>
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
          <select value={props.detailLevel} onChange={(event) => props.onDetailLevelChange(event.target.value)}>
            <option value="normal">{t("options.detailNormal")}</option>
            <option value="high">{t("options.detailHigh")}</option>
            <option value="very_high">{t("options.detailVeryHigh")}</option>
          </select>
        </label>
        <label className="form-field">
          <span>{t("options.density")}</span>
          <select value={props.density} onChange={(event) => props.onDensityChange(event.target.value)}>
            <option value="compact">{t("options.densityCompact")}</option>
            <option value="normal">{t("options.densityNormal")}</option>
            <option value="spacious">{t("options.densitySpacious")}</option>
          </select>
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
          <span>{t("options.maxCriticAttempts")}</span>
          <input
            type="number"
            min="1"
            max="10"
            value={props.maxCriticAttempts}
            onChange={(event) => props.onMaxCriticAttemptsChange(event.target.value)}
          />
        </label>
        <label className="visual-qa-field">
          <span
            className={`visual-qa-control ${
              props.enableVisualCritic ? "visual-qa-control-active" : ""
            }`}
          >
            <input
              className="visual-qa-input"
              type="checkbox"
              checked={props.enableVisualCritic}
              onChange={(event) => props.onEnableVisualCriticChange(event.target.checked)}
            />
            <span className="visual-qa-icon" aria-hidden="true">
              <Eye size={16} />
            </span>
            <span className="visual-qa-copy">
              <span className="visual-qa-name">{t("options.visualCritic")}</span>
              <span className="visual-qa-experimental">{t("common.experimental")}</span>
            </span>
            <span
              className="visual-qa-help"
              data-tooltip={t("options.visualCriticTooltip")}
              aria-label={t("options.visualCriticTooltip")}
              tabIndex={0}
              onClick={(event) => event.preventDefault()}
            >
              <HelpCircle size={14} />
            </span>
            <span className="visual-qa-switch" aria-hidden="true">
              <span />
            </span>
          </span>
        </label>
        {props.enableVisualCritic ? (
          <label className="form-field">
            <span>{t("options.visualQaMaxAttempts")}</span>
            <input
              type="number"
              min="1"
              max="10"
              value={props.visualQaMaxAttempts}
              onChange={(event) => props.onVisualQaMaxAttemptsChange(event.target.value)}
            />
          </label>
        ) : null}
      </div>

      {/* Deep research section */}
      <div className="options-icon-section">
        <label className="visual-qa-field">
          <span
            className={`visual-qa-control ${
              props.enableDeepResearch ? "visual-qa-control-active" : ""
            }`}
          >
            <input
              className="visual-qa-input"
              type="checkbox"
              checked={props.enableDeepResearch}
              onChange={(event) => props.onEnableDeepResearchChange(event.target.checked)}
            />
            <span className="visual-qa-icon" aria-hidden="true">
              <BookOpen size={16} />
            </span>
            <span className="visual-qa-copy">
              <span className="visual-qa-name">{t("options.deepResearch")}</span>
              <span className="visual-qa-experimental">{t("common.experimental")}</span>
            </span>
            <span
              className="visual-qa-help"
              data-tooltip={t("options.deepResearchTooltip")}
              aria-label={t("options.deepResearchTooltip")}
              tabIndex={0}
              onClick={(event) => event.preventDefault()}
            >
              <HelpCircle size={14} />
            </span>
            <span className="visual-qa-switch" aria-hidden="true">
              <span />
            </span>
          </span>
        </label>
      </div>

      {/* Icon section */}
      <div className="options-icon-section">
        <label className="visual-qa-field">
          <span
            className={`visual-qa-control ${
              props.enableIcon ? "visual-qa-control-active" : ""
            }`}
          >
            <input
              className="visual-qa-input"
              type="checkbox"
              checked={props.enableIcon}
              onChange={(event) => {
                props.onEnableIconChange(event.target.checked);
                if (!event.target.checked) {
                  props.onEnableIconRagChange(false);
                }
              }}
            />
            <span className="visual-qa-icon" aria-hidden="true">
              <Puzzle size={16} />
            </span>
            <span className="visual-qa-copy">
              <span className="visual-qa-name">{t("options.enableIcon")}</span>
            </span>
            <span
              className="visual-qa-help"
              data-tooltip={t("options.enableIconTooltip")}
              aria-label={t("options.enableIconTooltip")}
              tabIndex={0}
              onClick={(event) => event.preventDefault()}
            >
              <HelpCircle size={14} />
            </span>
            <span className="visual-qa-switch" aria-hidden="true">
              <span />
            </span>
          </span>
        </label>

        {props.enableIcon ? (
          <div className="options-icon-sub">
            <label className="visual-qa-field">
              <span
                className={`visual-qa-control ${
                  props.enableIconRag ? "visual-qa-control-active" : ""
                }`}
              >
                <input
                  className="visual-qa-input"
                  type="checkbox"
                  checked={props.enableIconRag}
                  onChange={(event) => props.onEnableIconRagChange(event.target.checked)}
                />
                <span className="visual-qa-icon" aria-hidden="true">
                  <Puzzle size={14} />
                </span>
                <span className="visual-qa-copy">
                  <span className="visual-qa-name">{t("options.iconRag")}</span>
                </span>
                <span
                  className="visual-qa-help"
                  data-tooltip={t("options.iconRagTooltip")}
                  aria-label={t("options.iconRagTooltip")}
                  tabIndex={0}
                  onClick={(event) => event.preventDefault()}
                >
                  <HelpCircle size={14} />
                </span>
                <span className="visual-qa-switch" aria-hidden="true">
                  <span />
                </span>
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
        <label className="visual-qa-field">
          <span
            className={`visual-qa-control ${
              props.researchConfig.arxiv_search_enabled || props.researchConfig.semantic_scholar_enabled || props.researchConfig.web_search_enabled
                ? "visual-qa-control-active"
                : ""
            }`}
          >
            <input
              className="visual-qa-input"
              type="checkbox"
              checked={!!(props.researchConfig.arxiv_search_enabled || props.researchConfig.semantic_scholar_enabled || props.researchConfig.web_search_enabled)}
              onChange={(event) => {
                const enabled = event.target.checked;
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
            <span className="visual-qa-icon" aria-hidden="true">
              <Sparkles size={16} />
            </span>
            <span className="visual-qa-copy">
              <span className="visual-qa-name">{t("options.researchEnrichment")}</span>
              <span className="visual-qa-experimental">{t("common.experimental")}</span>
            </span>
            <span
              className="visual-qa-help"
              data-tooltip={t("options.researchEnrichmentTooltip")}
              aria-label={t("options.researchEnrichmentTooltip")}
              tabIndex={0}
              onClick={(event) => event.preventDefault()}
            >
              <HelpCircle size={14} />
            </span>
            <span className="visual-qa-switch" aria-hidden="true">
              <span />
            </span>
          </span>
        </label>

        {(props.researchConfig.arxiv_search_enabled || props.researchConfig.semantic_scholar_enabled || props.researchConfig.web_search_enabled) ? (
          <div className="options-icon-sub research-sub">
            <p className="research-explainer">{t("options.researchExplainer")}</p>

            {/* arxiv toggle */}
            <label className="visual-qa-field">
              <span
                className={`visual-qa-control ${
                  props.researchConfig.arxiv_search_enabled ? "visual-qa-control-active" : ""
                }`}
              >
                <input
                  className="visual-qa-input"
                  type="checkbox"
                  checked={!!props.researchConfig.arxiv_search_enabled}
                  onChange={(event) => {
                    props.onResearchConfigChange({
                      ...props.researchConfig,
                      arxiv_search_enabled: event.target.checked,
                    });
                  }}
                />
                <span className="visual-qa-icon" aria-hidden="true">
                  <BookOpen size={14} />
                </span>
                <span className="visual-qa-copy">
                  <span className="visual-qa-name">{t("options.arxivSearch")}</span>
                  <span className="visual-qa-tag visual-qa-tag-free">{t("common.free")}</span>
                </span>
                <span
                  className="visual-qa-help"
                  data-tooltip={t("options.arxivSearchTooltip")}
                  aria-label={t("options.arxivSearchTooltip")}
                  tabIndex={0}
                  onClick={(event) => event.preventDefault()}
                >
                  <HelpCircle size={14} />
                </span>
                <span className="visual-qa-switch" aria-hidden="true">
                  <span />
                </span>
              </span>
            </label>

            {/* Semantic Scholar toggle */}
            <label className="visual-qa-field">
              <span
                className={`visual-qa-control ${
                  props.researchConfig.semantic_scholar_enabled ? "visual-qa-control-active" : ""
                }`}
              >
                <input
                  className="visual-qa-input"
                  type="checkbox"
                  checked={!!props.researchConfig.semantic_scholar_enabled}
                  onChange={(event) => {
                    props.onResearchConfigChange({
                      ...props.researchConfig,
                      semantic_scholar_enabled: event.target.checked,
                    });
                  }}
                />
                <span className="visual-qa-icon" aria-hidden="true">
                  <BookOpen size={14} />
                </span>
                <span className="visual-qa-copy">
                  <span className="visual-qa-name">{t("options.semanticScholar")}</span>
                  <span className="visual-qa-tag visual-qa-tag-free">{t("common.free")}</span>
                </span>
                <span
                  className="visual-qa-help"
                  data-tooltip={t("options.semanticScholarTooltip")}
                  aria-label={t("options.semanticScholarTooltip")}
                  tabIndex={0}
                  onClick={(event) => event.preventDefault()}
                >
                  <HelpCircle size={14} />
                </span>
                <span className="visual-qa-switch" aria-hidden="true">
                  <span />
                </span>
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
            <label className="visual-qa-field">
              <span
                className={`visual-qa-control ${
                  props.researchConfig.web_search_enabled ? "visual-qa-control-active" : ""
                }`}
              >
                <input
                  className="visual-qa-input"
                  type="checkbox"
                  checked={!!props.researchConfig.web_search_enabled}
                  onChange={(event) => {
                    props.onResearchConfigChange({
                      ...props.researchConfig,
                      web_search_enabled: event.target.checked,
                    });
                  }}
                />
                <span className="visual-qa-icon" aria-hidden="true">
                  <Search size={14} />
                </span>
                <span className="visual-qa-copy">
                  <span className="visual-qa-name">{t("options.webSearch")}</span>
                  <span className="visual-qa-tag visual-qa-tag-key">{t("common.needsKey")}</span>
                </span>
                <span
                  className="visual-qa-help"
                  data-tooltip={t("options.webSearchTooltip")}
                  aria-label={t("options.webSearchTooltip")}
                  tabIndex={0}
                  onClick={(event) => event.preventDefault()}
                >
                  <HelpCircle size={14} />
                </span>
                <span className="visual-qa-switch" aria-hidden="true">
                  <span />
                </span>
              </span>
            </label>
            {props.researchConfig.web_search_enabled ? (
              <div className="options-icon-sub">
                <div className="research-provider-select">
                  <label className="research-provider-option">
                    <input
                      type="radio"
                      name="web-search-provider"
                      checked={(props.researchConfig.web_search_provider ?? "tavily") === "tavily"}
                      onChange={() => {
                        props.onResearchConfigChange({
                          ...props.researchConfig,
                          web_search_provider: "tavily",
                        });
                      }}
                    />
                    <span>{t("options.tavilyApiKey")}</span>
                  </label>
                  <label className="research-provider-option">
                    <input
                      type="radio"
                      name="web-search-provider"
                      checked={props.researchConfig.web_search_provider === "serpapi"}
                      onChange={() => {
                        props.onResearchConfigChange({
                          ...props.researchConfig,
                          web_search_provider: "serpapi",
                        });
                      }}
                    />
                    <span>{t("options.serpApiKey")}</span>
                  </label>
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
