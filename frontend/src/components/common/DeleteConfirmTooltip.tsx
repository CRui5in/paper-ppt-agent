import { type RefObject, useEffect, useMemo } from "react";
import { createPortal } from "react-dom";
import { Loader2 } from "lucide-react";

interface DeleteConfirmTooltipProps {
  anchorRef: RefObject<HTMLElement | null>;
  message: string;
  confirmLabel: string;
  cancelLabel: string;
  loading?: boolean;
  onConfirm: () => void | Promise<void>;
  onCancel: () => void;
}

export function DeleteConfirmTooltip({
  anchorRef,
  message,
  confirmLabel,
  cancelLabel,
  loading = false,
  onConfirm,
  onCancel,
}: DeleteConfirmTooltipProps) {
  const position = useMemo(() => {
    const rect = anchorRef.current?.getBoundingClientRect();
    if (!rect) {
      return { top: 80, left: window.innerWidth - 220 };
    }
    const width = 206;
    return {
      top: Math.min(window.innerHeight - 112, rect.bottom + 8),
      left: Math.min(window.innerWidth - width - 12, Math.max(12, rect.right - width)),
    };
  }, [anchorRef]);

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onCancel();
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onCancel]);

  return createPortal(
    <div className="delete-confirm-tooltip" style={position} role="dialog" aria-modal="false">
      <p>{message}</p>
      <div className="delete-confirm-actions">
        <button type="button" className="delete-confirm-button" onClick={onCancel} disabled={loading}>
          {cancelLabel}
        </button>
        <button
          type="button"
          className="delete-confirm-button delete-confirm-button-danger"
          onClick={() => void onConfirm()}
          disabled={loading}
        >
          {loading ? <Loader2 size={12} className="spin" /> : null}
          {confirmLabel}
        </button>
      </div>
    </div>,
    document.body,
  );
}
