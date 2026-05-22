import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, CircleDashed, Loader2, RotateCcw, SkipForward } from "lucide-react";

import { useLocale, type Locale } from "../../i18n";
import { translateTemplateImportMessage } from "../../lib/i18nStatus";
import type { ImportStatus } from "../../lib/types";

export interface ProgressViewProps {
  status: ImportStatus;
  onRetry: (stepId: string) => void;
  mode?: "direct" | "llm";
}

type StepStatus = "pending" | "active" | "complete" | "error" | "skipped";

interface StepLike {
  id: string;
  label?: string;
  status?: string;
  message?: string;
  error?: string | null;
  error_kind?: string;
  started_at?: number;
}

const STEP_ORDER = ["uploaded", "analyzing", "rendering", "detecting_assets", "baseline_preview", "llm_review", "review"] as const;
const DIRECT_STEP_ORDER = ["uploaded", "analyzing", "rendering", "review"] as const;

/**
 * ProgressView (Task 16.3)
 *
 * Step chips driven by `ImportStatus.steps`. Long-running active step
 * surfaces `step.message`; an `error` step surfaces a localized
 * `error_kind` description plus a "Retry this stage" button (Req 1.5/1.6).
 */
export function ProgressView({ status, onRetry, mode = "llm" }: ProgressViewProps) {
  const { t, locale } = useLocale();
  const [now, setNow] = useState(() => Date.now() / 1000);

  // Tick every second so "active" elapsed time triggers the >10s message.
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(id);
  }, []);

  const steps = useMemo<StepLike[]>(() => {
    const provided = (status.steps ?? []) as StepLike[];
    const byId = new Map(provided.map((s) => [s.id, s]));
    const order = mode === "direct" ? DIRECT_STEP_ORDER : STEP_ORDER;
    return order.map((id) => byId.get(id) ?? { id, label: t(`template.step.${id}`), status: "pending" });
  }, [mode, status.steps, t]);

  const overallProgress = Math.max(0, Math.min(1, status.progress ?? 0));
  const overallPct = Math.round(overallProgress * 100);

  return (
    <div className="flex flex-col gap-4">
      <div>
        <div className="mb-2 flex items-center justify-between text-sm">
          <strong style={{ color: "var(--ti-text)" }}>
            {translateTemplateImportMessage(status.message, locale) || t("template.processing")}
          </strong>
          <span style={{ color: "var(--ti-muted)" }}>{overallPct}%</span>
        </div>
        <div
          role="progressbar"
          aria-valuenow={overallPct}
          aria-valuemin={0}
          aria-valuemax={100}
          className="h-2 w-full overflow-hidden rounded-full"
          style={{ background: "var(--ti-surface-inset)" }}
        >
          <div
            className="h-full rounded-full transition-[width] duration-300"
            style={{
              width: `${Math.max(2, overallPct)}%`,
              background: status.status === "error" ? "var(--ti-danger)" : "var(--ti-accent)",
            }}
          />
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {steps.map((step) => (
          <StepChip key={step.id} step={step} t={t} />
        ))}
      </div>

      <ActiveDetail steps={steps} now={now} t={t} locale={locale} />

      <ErrorPanel status={status} steps={steps} onRetry={onRetry} t={t} locale={locale} />
    </div>
  );
}

function StepChip({ step, t }: { step: StepLike; t: (key: string) => string }) {
  const status = (step.status as StepStatus) ?? "pending";
  const tone = chipTone(status);
  const translated = t(`template.step.${step.id}`);
  const label = translated !== `template.step.${step.id}` ? translated : step.label || step.id;
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium"
      style={{
        borderColor: tone.border,
        background: tone.bg,
        color: tone.fg,
      }}
      title={status}
    >
      <StepIcon status={status} />
      <span>{label}</span>
    </span>
  );
}

function StepIcon({ status }: { status: StepStatus }) {
  switch (status) {
    case "complete":
      return <CheckCircle2 size={13} />;
    case "active":
      return <Loader2 size={13} className="animate-spin" />;
    case "error":
      return <AlertTriangle size={13} />;
    case "skipped":
      return <SkipForward size={13} />;
    case "pending":
    default:
      return <CircleDashed size={13} />;
  }
}

function chipTone(status: StepStatus): { border: string; bg: string; fg: string } {
  switch (status) {
    case "complete":
      return {
        border: "color-mix(in srgb, var(--ti-success) 40%, var(--ti-line))",
        bg: "color-mix(in srgb, var(--ti-success) 12%, transparent)",
        fg: "var(--ti-success)",
      };
    case "active":
      return {
        border: "color-mix(in srgb, var(--ti-accent) 50%, var(--ti-line))",
        bg: "color-mix(in srgb, var(--ti-accent) 12%, transparent)",
        fg: "var(--ti-accent)",
      };
    case "error":
      return {
        border: "color-mix(in srgb, var(--ti-danger) 55%, var(--ti-line))",
        bg: "color-mix(in srgb, var(--ti-danger) 14%, transparent)",
        fg: "var(--ti-danger)",
      };
    case "skipped":
      return {
        border: "color-mix(in srgb, var(--ti-warning) 40%, var(--ti-line))",
        bg: "color-mix(in srgb, var(--ti-warning) 10%, transparent)",
        fg: "var(--ti-warning)",
      };
    case "pending":
    default:
      return {
        border: "var(--ti-line)",
        bg: "var(--ti-surface-inset)",
        fg: "var(--ti-muted)",
      };
  }
}

function ActiveDetail({
  steps,
  now,
  t,
  locale,
}: {
  steps: StepLike[];
  now: number;
  t: (key: string) => string;
  locale: Locale;
}) {
  const active = steps.find((s) => s.status === "active");
  if (!active) return null;
  const elapsed = active.started_at ? Math.max(0, now - active.started_at) : 0;
  // Surface the message after 10s (Req 1.3) or whenever the backend already
  // populated it.
  const showMessage = active.message && (elapsed >= 10 || true);
  if (!showMessage) return null;
  const translated = t(`template.step.${active.id}`);
  const label = translated !== `template.step.${active.id}` ? translated : active.label || active.id;
  return (
    <div
      className="rounded-[var(--ti-radius-md,10px)] border px-3 py-2 text-sm"
      style={{
        borderColor: "color-mix(in srgb, var(--ti-accent) 30%, var(--ti-line))",
        background: "color-mix(in srgb, var(--ti-accent) 6%, transparent)",
        color: "var(--ti-text)",
      }}
    >
      <span className="mr-2 font-semibold" style={{ color: "var(--ti-accent)" }}>
        {label}
      </span>
      <span>{translateTemplateImportMessage(active.message, locale)}</span>
    </div>
  );
}

function ErrorPanel({
  status,
  steps,
  onRetry,
  t,
  locale,
}: {
  status: ImportStatus;
  steps: StepLike[];
  onRetry: (stepId: string) => void;
  t: (key: string) => string;
  locale: Locale;
}) {
  const errored = steps.find((s) => s.status === "error");
  const topLevelError = status.status === "error" ? status.error : null;
  if (!errored && !topLevelError) return null;

  const stepId = errored?.id ?? "";
  const errorKind = errored?.error_kind || inferErrorKind(stepId);
  const message = translateTemplateImportMessage(errored?.error || errored?.message || topLevelError, locale) || t("template.importError");

  return (
    <div
      role="alert"
      className="flex flex-col gap-2 rounded-[var(--ti-radius-md,10px)] border p-3 text-sm"
      style={{
        borderColor: "color-mix(in srgb, var(--ti-danger) 50%, var(--ti-line))",
        background: "color-mix(in srgb, var(--ti-danger) 8%, transparent)",
        color: "var(--ti-text)",
      }}
    >
      <div className="flex items-start gap-2">
        <AlertTriangle size={16} style={{ color: "var(--ti-danger)", flexShrink: 0, marginTop: 2 }} />
        <div className="flex flex-1 flex-col gap-0.5">
          <strong style={{ color: "var(--ti-danger)" }}>
            {t(`template.errorKind.${errorKind}`)}
          </strong>
          <span style={{ color: "var(--ti-muted)" }}>{message}</span>
        </div>
      </div>
      {stepId ? (
        <div className="flex justify-end">
          <button
            type="button"
            onClick={() => onRetry(stepId)}
            className="ti-focusable inline-flex items-center gap-1.5 rounded-[var(--ti-radius-sm,6px)] border px-3 py-1.5 text-xs font-semibold"
            style={{
              borderColor: "color-mix(in srgb, var(--ti-danger) 50%, var(--ti-line))",
              background: "var(--ti-surface)",
              color: "var(--ti-danger)",
            }}
          >
            <RotateCcw size={13} />
            {t("template.retryStage")}
          </button>
        </div>
      ) : null}
    </div>
  );
}

function inferErrorKind(stepId: string): string {
  switch (stepId) {
    case "rendering":
      return "render";
    case "detecting_assets":
    case "uploaded":
    case "analyzing":
      return "extraction";
    case "llm_review":
      return "llm";
    case "review":
      return "persistence";
    default:
      return "unknown";
  }
}
