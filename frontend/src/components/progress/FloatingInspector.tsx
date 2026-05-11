import { type ReactNode, useEffect } from "react";
import { X } from "lucide-react";
import { useLocale } from "../../i18n";

interface FloatingInspectorProps {
  open: boolean;
  title: string;
  icon?: ReactNode;
  onClose: () => void;
  children: ReactNode;
}

export function FloatingInspector({ open, title, icon, onClose, children }: FloatingInspectorProps) {
  const { t } = useLocale();

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, open]);

  if (!open) return null;

  return (
    <div className="floating-inspector-backdrop" role="presentation" onMouseDown={onClose}>
      <aside
        className="floating-inspector-panel"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="floating-inspector-header">
          <div className="panel-title-row">
            {icon}
            <p className="panel-title">{title}</p>
          </div>
          <button type="button" className="icon-btn" aria-label={t("common.close")} onClick={onClose}>
            <X size={17} />
          </button>
        </div>
        <div className="floating-inspector-body">{children}</div>
      </aside>
    </div>
  );
}
