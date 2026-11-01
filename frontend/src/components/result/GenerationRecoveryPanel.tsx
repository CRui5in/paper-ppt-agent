import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Loader2, Play, RotateCcw } from "lucide-react";
import { useGeneration } from "../../hooks/useGeneration";
import { useLocale } from "../../i18n";
import { fetchGenerationFeedback, fetchGenerationRecovery, saveGenerationFeedback } from "../../lib/api";
import { translateStageStatus } from "../../lib/i18nStatus";
import type { GenerationFeedback, GenerationRecovery } from "../../lib/types";

export function GenerationRecoveryPanel({ jobId, status }: { jobId?: string; status?: string }) {
  const { t, locale } = useLocale();
  const navigate = useNavigate();
  const continueGeneration = useGeneration((state) => state.continueGeneration);
  const [recovery, setRecovery] = useState<GenerationRecovery | null>(null);
  const [feedback, setFeedback] = useState("");
  const [records, setRecords] = useState<GenerationFeedback[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const failed = status === "error" || status === "cancelled";

  useEffect(() => {
    let cancelled = false;
    setRecovery(null);
    setRecords([]);
    setError("");
    setFeedback("");
    if (jobId && failed) {
      Promise.all([fetchGenerationRecovery(jobId), fetchGenerationFeedback(jobId)])
        .then(([next, saved]) => {
          if (!cancelled) { setRecovery(next); setRecords(saved.feedback); }
        })
        .catch((err: unknown) => { if (!cancelled) setError(err instanceof Error ? err.message : String(err)); });
    }
    return () => { cancelled = true; };
  }, [jobId, failed]);

  if (!jobId || !failed) return null;

  const resume = async (mode: "resume" | "retry_failed") => {
    setPending(true);
    setError("");
    try {
      const nextId = await continueGeneration(jobId, mode, feedback);
      navigate(`/result?job=${nextId}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally { setPending(false); }
  };

  return (
    <section className="generation-recovery-panel" aria-label={t("recovery.title")}>
      <strong>{t("recovery.title")}</strong>
      {recovery ? <p>{t("recovery.checkpoint")
        .replace("{stage}", translateStageStatus(recovery.stage, locale, "progress"))
        .replace("{count}", String(recovery.completed_pages))}</p> : null}
      {recovery && !recovery.can_resume ? <p>{t("recovery.unavailable")}</p> : null}
      <textarea aria-label={t("recovery.feedback")} placeholder={t("recovery.feedback")} value={feedback}
        onChange={(event) => setFeedback(event.target.value)} maxLength={8000} rows={3} disabled={pending} />
      <button type="button" className="primary-button full-width" disabled={pending || !recovery?.can_resume} onClick={() => void resume("resume")}>
        {pending ? <Loader2 size={15} className="spin" /> : <Play size={15} />}{t("recovery.continue")}
      </button>
      {recovery?.can_retry ? <button type="button" className="secondary-button full-width" disabled={pending} onClick={() => void resume("retry_failed")}>
        <RotateCcw size={15} />{t("recovery.retry")}
      </button> : null}
      <p>{t(recovery?.backend === "agent" ? "recovery.agentHint" : "recovery.hint")}</p>
      <button type="button" className="secondary-button full-width" disabled={pending || !feedback.trim()} onClick={async () => {
        setPending(true); setError("");
        try {
          const record = await saveGenerationFeedback(jobId, feedback);
          setRecords((current) => [...current, record]); setFeedback("");
        } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
        finally { setPending(false); }
      }}>{t("recovery.saveFeedback")}</button>
      {records.length ? <details><summary>{t("recovery.savedFeedback")} ({records.length})</summary>
        {records.map((record) => <p key={record.id}>{record.message}</p>)}
      </details> : null}
      {error ? <p role="alert">{error}</p> : null}
    </section>
  );
}
