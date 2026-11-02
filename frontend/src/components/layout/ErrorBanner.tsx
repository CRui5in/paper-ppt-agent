import { useEffect, useMemo, useState } from "react";
import { AlertOctagon, Copy, Check, X, ChevronDown, ChevronUp } from "lucide-react";
import { useGeneration } from "../../hooks/useGeneration";
import { useLocale } from "../../i18n";
import { HoverTooltip } from "../common/HoverTooltip";
import { describeRuntimeError } from "../../lib/runtimeErrors";

/**
 * Show runtime causes and recovery steps above expandable original errors.
 */
export function ErrorBanner() {
  const error = useGeneration((state) => state.error);
  const dismissError = useGeneration((state) => state.dismissError);
  const { t, locale } = useLocale();
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);

  // Reset transient UI state whenever a new error replaces a previous one.
  useEffect(() => {
    setExpanded(false);
    setCopied(false);
  }, [error]);

  const isMultiline = useMemo(
    () => Boolean(error && (error.includes("\n") || error.length > 160)),
    [error],
  );
  const diagnosis = useMemo(() => error ? describeRuntimeError(error, locale) : null, [error, locale]);

  if (!error) return null;

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(diagnosis ? `${diagnosis.summary}\n${diagnosis.action}\n\n${error}` : error);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      // Clipboard may be blocked (e.g. http-only context). Silently no-op:
      // the message is still visible inside the banner so the user can
      // select it manually.
    }
  };

  return (
    <div className="error-banner" role="alert" aria-live="assertive">
      <div className="error-banner-inner">
        <AlertOctagon size={20} className="error-banner-icon" aria-hidden />
        <div className="error-banner-body">
          <div className="error-banner-title">{t("error.title")}</div>
          {diagnosis ? (
            <>
              <p className="error-banner-summary">{diagnosis.summary}</p>
              <p className="error-banner-guidance">{diagnosis.action}</p>
            </>
          ) : null}
          {!diagnosis || expanded ? <pre
            className={`error-banner-message${expanded ? " error-banner-message-expanded" : ""}`}
          >
            {error}
          </pre> : null}
        </div>
        <div className="error-banner-actions">
          {diagnosis || isMultiline ? (
            <HoverTooltip content={expanded ? t("error.collapse") : t("error.expand")}>
              <button
                type="button"
                className="error-banner-action"
                onClick={() => setExpanded((v) => !v)}
                aria-label={expanded ? t("error.collapse") : t("error.expand")}
              >
                {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                <span>{expanded ? t("error.collapse") : t("error.expand")}</span>
              </button>
            </HoverTooltip>
          ) : null}
          <HoverTooltip content={t("error.copy")}>
            <button
              type="button"
              className="error-banner-action"
              onClick={handleCopy}
              aria-label={t("error.copy")}
            >
              {copied ? <Check size={16} /> : <Copy size={16} />}
              <span>{copied ? t("error.copied") : t("error.copy")}</span>
            </button>
          </HoverTooltip>
          <HoverTooltip content={t("error.dismiss")}>
            <button
              type="button"
              className="error-banner-close"
              onClick={dismissError}
              aria-label={t("error.dismiss")}
            >
              <X size={18} />
            </button>
          </HoverTooltip>
        </div>
      </div>
    </div>
  );
}
