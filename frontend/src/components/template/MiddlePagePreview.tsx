import { useLocale } from "../../i18n";
import type { TemplatePageType } from "../../lib/types";

function sanitizeSvg(svg: string): string {
  return (svg ?? "")
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "")
    .replace(/\son\w+\s*=\s*"[^"]*"/gi, "")
    .replace(/\son\w+\s*=\s*'[^']*'/gi, "")
    .replace(/javascript:/gi, "");
}

export interface BigPreviewProps {
  svg: string | undefined;
  pageType: TemplatePageType;
}

/**
 * Full-bleed 16:9 preview used when the user is browsing an existing
 * template. The active page-type label is rendered as a small chip in
 * the upper-left corner so the rail at the bottom can drive it.
 */
export function BigPreview({ svg, pageType }: BigPreviewProps) {
  const { t } = useLocale();
  const safe = sanitizeSvg(svg ?? "");
  return (
    <div className="templates-big-preview">
      <div className="templates-big-preview-frame">
        {safe ? (
          <div
            className="templates-big-preview-svg"
            dangerouslySetInnerHTML={{ __html: safe }}
          />
        ) : (
          <div className="templates-big-preview-empty">
            <span>{t("template.previewSlideMissing")}</span>
          </div>
        )}
        <span className="templates-big-preview-chip">
          {t(`templates.preview.tilelabel.${pageType}`)}
        </span>
      </div>
    </div>
  );
}

/**
 * Centered empty-state for the middle column when the user has no
 * template selected and no import in flight. Matches the workspace's
 * empty stage: a calm full-bleed canvas with a single muted hint.
 */
export function MiddleEmptyState() {
  const { t } = useLocale();
  return (
    <div className="templates-canvas-empty">
      <span className="templates-canvas-empty-text">
        {t("templates.empty.title")}
      </span>
    </div>
  );
}
