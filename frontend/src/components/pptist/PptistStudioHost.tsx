import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { useLocale } from "../../i18n";
import {
  mountPptistStudio,
  type MountedPptistStudio,
  type PptistStudioSource,
} from "../../pptist/paper-entry";

interface PptistStudioHostProps {
  source: PptistStudioSource;
  className?: string;
  downloadHref?: string;
  onConfirmImport?: () => Promise<void> | void;
  saveBeforeConfirmImport?: boolean;
  confirmImportDisabled?: boolean;
  confirmImportHint?: string;
  onCancelImport?: () => void;
  onReexport?: () => void;
  onDeleteRun?: () => Promise<void>;
  onSaved?: (result: unknown) => void;
  onError?: (message: string) => void;
}

export function PptistStudioHost({
  source,
  className,
  downloadHref,
  onConfirmImport,
  saveBeforeConfirmImport,
  confirmImportDisabled,
  confirmImportHint,
  onCancelImport,
  onReexport,
  onDeleteRun,
  onSaved,
  onError,
}: PptistStudioHostProps) {
  const { locale } = useLocale();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const studioRef = useRef<MountedPptistStudio | null>(null);
  const onSavedRef = useRef(onSaved);
  const onErrorRef = useRef(onError);
  const onReexportRef = useRef(onReexport);
  const onDeleteRunRef = useRef(onDeleteRun);
  const onConfirmImportRef = useRef(onConfirmImport);
  const onCancelImportRef = useRef(onCancelImport);
  const confirmImportDisabledRef = useRef(confirmImportDisabled);
  const confirmImportHintRef = useRef(confirmImportHint);
  const openingStatus = locale === "zh" ? "正在打开 PPTist Studio ..." : "Opening PPTist Studio ...";
  const [status, setStatus] = useState(openingStatus);
  const [error, setError] = useState<string | null>(null);
  const savedStatus = locale === "zh" ? "已保存" : "Saved";
  const isSavedStatus = status === savedStatus;
  const sourceRevision = "revision" in source ? source.revision ?? "" : "";
  const sourceKey = source.kind === "preview"
    ? `preview:${source.jobId}:${locale}:${sourceRevision}`
    : `templateImport:${source.importId}:${locale}:${sourceRevision}`;

  useEffect(() => {
    onSavedRef.current = onSaved;
  }, [onSaved]);

  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  useEffect(() => {
    onReexportRef.current = onReexport;
  }, [onReexport]);

  useEffect(() => {
    onDeleteRunRef.current = onDeleteRun;
  }, [onDeleteRun]);

  useEffect(() => {
    onConfirmImportRef.current = onConfirmImport;
  }, [onConfirmImport]);

  useEffect(() => {
    onCancelImportRef.current = onCancelImport;
  }, [onCancelImport]);

  useEffect(() => {
    confirmImportDisabledRef.current = confirmImportDisabled;
  }, [confirmImportDisabled]);

  useEffect(() => {
    confirmImportHintRef.current = confirmImportHint;
  }, [confirmImportHint]);

  useEffect(() => {
    const node = containerRef.current;
    if (!node) return undefined;

    setError(null);
    setStatus(openingStatus);
    const studio = mountPptistStudio(node, {
      source,
      downloadHref,
      locale,
      onReexport: onReexport ? () => onReexportRef.current?.() : undefined,
      onDeleteRun: onDeleteRun ? () => onDeleteRunRef.current?.() : undefined,
      onConfirmImport: onConfirmImport
        ? async () => {
            if (confirmImportDisabledRef.current) {
              const message = confirmImportHintRef.current || "";
              if (message) onErrorRef.current?.(message);
              return;
            }
            await onConfirmImportRef.current?.();
          }
        : undefined,
      saveBeforeConfirmImport: Boolean(saveBeforeConfirmImport),
      confirmImportDisabled: Boolean(confirmImportDisabled),
      confirmImportHint,
      onCancelImport: onCancelImport ? () => onCancelImportRef.current?.() : undefined,
      // PPTist renders its own in-editor busy/saved toast.  Once it starts
      // reporting status, hide the React shell status so the two overlays do
      // not stack on top of each other.
      onStatus: () => setStatus(""),
      onSaved: (result) => onSavedRef.current?.(result),
      onError: (message) => {
        setError(message);
        onErrorRef.current?.(message);
      },
    });
    studioRef.current = studio;

    return () => {
      studio.destroy();
      if (studioRef.current === studio) studioRef.current = null;
    };
  }, [sourceKey]);

  return (
    <section className={`pptist-studio-host ${className ?? ""}`}>
      {status || error ? (
        <div className={`pptist-studio-host-status ${error ? "pptist-studio-host-status-error" : ""}`}>
          {!error && status && !isSavedStatus ? <Loader2 size={14} className="pptist-studio-host-spinner" /> : null}
          <span>{error || status}</span>
        </div>
      ) : null}
      <div ref={containerRef} className="pptist-studio-mount" />
    </section>
  );
}
