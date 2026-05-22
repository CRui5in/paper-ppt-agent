import { type ReactNode } from "react";
import {
  BarChart3,
  ChevronDown,
  Download,
  Eye,
  Image as ImageIcon,
  Maximize2,
  MessageSquareText,
  Minus,
  MoreHorizontal,
  MousePointer2,
  Omega,
  Play,
  Plus,
  Redo2,
  Save,
  Search,
  Square,
  Table2,
  Type,
  Undo2,
  Upload,
  Video,
} from "lucide-react";
import { useLocale } from "../../i18n";
import type { TemplatePageType } from "../../lib/types";
import { useContainedSlideFrame } from "./useContainedSlideFrame";

function sanitizeSvg(svg: string): string {
  return (svg ?? "")
    .replace(/<\s*(script|foreignObject|iframe|object|embed|link|meta|base)\b[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi, "")
    .replace(/<\s*(script|foreignObject|iframe|object|embed|link|meta|base)\b[^>]*\/\s*>/gi, "")
    .replace(/\son[a-z0-9:_-]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, "")
    .replace(/\s+(href|xlink:href)\s*=\s*(?:"\s*javascript:[^"]*"|'\s*javascript:[^']*'|javascript:[^\s>]+)/gi, ' href="#"');
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
  const { containerRef, frameStyle } = useContainedSlideFrame();
  const safe = sanitizeSvg(svg ?? "");
  return (
    <div ref={containerRef} className="templates-big-preview">
      <div className="templates-big-preview-frame" style={frameStyle}>
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
    <div className="generate-pptist-empty-shell templates-disabled-workbench" aria-disabled="true">
      <DisabledTemplateWorkbenchHeader />
      <div className="generate-pptist-empty-rail">
        <button
          type="button"
          className="generate-pptist-empty-thumb generate-pptist-empty-thumb-active"
          disabled
        >
          <span>01</span>
          <div />
        </button>
      </div>
      <div className="generate-pptist-empty-body">
        <div className="generate-pptist-empty-canvas">
          <span>{t("preview.emptyState")}</span>
        </div>
        <label className="generate-pptist-empty-notes">
          <span>{t("preview.notesPlaceholder")}</span>
          <em>0 / 1000</em>
        </label>
      </div>
    </div>
  );
}

export function TemplateImportingState({ children }: { children: ReactNode }) {
  return (
    <div className="generate-pptist-empty-shell templates-disabled-workbench templates-importing-workbench">
      <DisabledTemplateWorkbenchHeader />
      <div className="generate-pptist-empty-rail">
        <button
          type="button"
          className="generate-pptist-empty-thumb generate-pptist-empty-thumb-active"
          disabled
        >
          <span>01</span>
          <div className="motion-skeleton" />
        </button>
      </div>
      <div className="generate-pptist-empty-body">
        <div className="generate-pptist-empty-canvas templates-importing-workbench-canvas">
          {children}
        </div>
        <label className="generate-pptist-empty-notes">
          <span />
          <em>0 / 1000</em>
        </label>
      </div>
    </div>
  );
}

function DisabledTemplateWorkbenchHeader() {
  const { t } = useLocale();
  return (
    <div className="generate-pptist-disabled-header" aria-disabled="true">
      <div className="generate-pptist-disabled-title" aria-hidden="true" />

      <div className="generate-pptist-disabled-tool">
        <div className="generate-pptist-disabled-left-tools">
          <DisabledToolbarButton icon={<Undo2 size={16} />} label={t("editor.undo")} compact />
          <DisabledToolbarButton icon={<Redo2 size={16} />} label={t("editor.redo")} compact />
          <span className="generate-pptist-disabled-divider" />
          <DisabledToolbarButton icon={<MoreHorizontal size={16} />} label={t("pptist.more")} compact />
          <DisabledToolbarButton icon={<MessageSquareText size={16} />} label={t("pptist.comments")} compact />
          <DisabledToolbarButton icon={<MousePointer2 size={16} />} label={t("pptist.selectionPane")} compact />
          <DisabledToolbarButton icon={<Search size={16} />} label={t("pptist.searchReplace")} compact />
        </div>

        <div className="generate-pptist-disabled-insert-tools">
          <DisabledToolbarButton icon={<Type size={16} />} label={t("pptist.textbox")} caret />
          <DisabledToolbarButton icon={<Square size={16} />} label={t("editor.shapeTool")} caret />
          <DisabledToolbarButton icon={<ImageIcon size={16} />} label={t("editor.pictureTool")} caret />
          <DisabledToolbarButton icon={<Minus size={16} />} label={t("pptist.line")} />
          <DisabledToolbarButton icon={<BarChart3 size={16} />} label={t("pptist.chart")} />
          <DisabledToolbarButton icon={<Table2 size={16} />} label={t("editor.tableTool")} />
          <DisabledToolbarButton icon={<span className="generate-pptist-sigma">Σ</span>} label={t("pptist.formula")} />
          <DisabledToolbarButton icon={<Video size={16} />} label={t("pptist.media")} />
          <DisabledToolbarButton icon={<Omega size={16} />} label={t("pptist.symbol")} />
        </div>

        <div className="generate-pptist-disabled-right-tools">
          <DisabledToolbarButton icon={<Minus size={15} />} label={t("pptist.zoomOut")} compact />
          <span className="generate-pptist-disabled-scale">100%</span>
          <DisabledToolbarButton icon={<Plus size={15} />} label={t("pptist.zoomIn")} compact />
          <DisabledToolbarButton icon={<Maximize2 size={15} />} label={t("editor.fit")} compact />
        </div>
      </div>

      <div className="generate-pptist-disabled-actions">
        <DisabledToolbarButton icon={<Save size={16} />} label={t("editor.save")} />
        <DisabledToolbarButton icon={<Play size={16} />} label={t("preview.slideshow")} caret compact />
        <span className="generate-pptist-disabled-divider" />
        <DisabledToolbarButton icon={<Upload size={16} />} label={t("template.importAction")} caret className="generate-pptist-disabled-primary" />
        <DisabledToolbarButton icon={<Eye size={16} />} label={t("pptist.properties")} />
        <a
          className="generate-pptist-disabled-github"
          href="https://github.com/pipipi-pikachu/PPTist"
          target="_blank"
          rel="noreferrer"
          title="PPTist by pipipi-pikachu"
        >
          <GitHubMark />
        </a>
      </div>
    </div>
  );
}

function DisabledToolbarButton({
  icon,
  label,
  caret,
  compact,
  className,
}: {
  icon: ReactNode;
  label: string;
  caret?: boolean;
  compact?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      className={`generate-pptist-disabled-button ${compact ? "generate-pptist-disabled-button-compact" : ""} ${className ?? ""}`}
      disabled
      title={label}
    >
      {icon}
      {!compact ? <span>{label}</span> : null}
      {caret ? <ChevronDown size={13} /> : null}
    </button>
  );
}

function GitHubMark() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="17"
      height="17"
      aria-hidden="true"
      focusable="false"
      fill="currentColor"
    >
      <path d="M12 2C6.48 2 2 6.58 2 12.21c0 4.51 2.87 8.33 6.84 9.68.5.09.68-.22.68-.49 0-.24-.01-.88-.01-1.73-2.78.62-3.37-1.37-3.37-1.37-.45-1.18-1.11-1.49-1.11-1.49-.91-.63.07-.62.07-.62 1 .07 1.53 1.05 1.53 1.05.89 1.56 2.34 1.11 2.91.85.09-.66.35-1.11.63-1.37-2.22-.26-4.56-1.14-4.56-5.06 0-1.12.39-2.03 1.03-2.75-.1-.26-.45-1.3.1-2.71 0 0 .84-.27 2.75 1.05A9.29 9.29 0 0 1 12 6.91c.85 0 1.71.12 2.51.34 1.9-1.32 2.74-1.05 2.74-1.05.55 1.41.2 2.45.1 2.71.64.72 1.03 1.63 1.03 2.75 0 3.93-2.34 4.8-4.57 5.05.36.32.68.94.68 1.9 0 1.37-.01 2.47-.01 2.81 0 .27.18.59.69.49A10.15 10.15 0 0 0 22 12.21C22 6.58 17.52 2 12 2Z" />
    </svg>
  );
}
