import { Bot, Loader2, X as XIcon } from "lucide-react";

import { useLocale } from "../../i18n";

export interface AgentImportingViewProps {
  /** Newest progress message. */
  message?: string;
  /** Cancel the active import. */
  onCancel: () => void;
  /** Optional model name for the badge. */
  modelLabel?: string;
}

/**
 * Replaces the classic step-pill ProgressView when the user picks Agent
 * mode. The Agent owns the whole pipeline (parse → render → classify →
 * draft) so we just surface a calm "Agent is working" panel — detailed
 * progress lives in the right-side collaboration feed.
 */
export function AgentImportingView({ message, onCancel, modelLabel }: AgentImportingViewProps) {
  const { t } = useLocale();
  return (
    <div className="ti-agent-importing">
      <div className="ti-agent-importing-icon" aria-hidden="true">
        <Bot size={28} />
      </div>
      <div className="ti-agent-importing-body">
        <div className="ti-agent-importing-title">
          <Loader2 size={14} className="animate-spin" />
          <span>{t("template.agentImporting.title")}</span>
          {modelLabel ? (
            <span className="ti-agent-importing-model">{modelLabel}</span>
          ) : null}
        </div>
        <p className="ti-agent-importing-message">
          {message || t("template.agentImporting.subtitle")}
        </p>
        <p className="ti-agent-importing-hint">{t("template.agentImporting.hint")}</p>
      </div>
      <button
        type="button"
        onClick={onCancel}
        className="ti-focusable ti-agent-importing-cancel"
        aria-label={t("common.cancel")}
      >
        <XIcon size={13} />
        <span>{t("common.cancel")}</span>
      </button>
    </div>
  );
}
