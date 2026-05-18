/*
 * ─────────────────────────────────────────────────────────────────────────────
 *  SlideStage — coordinate contract (CRITICAL)
 * ─────────────────────────────────────────────────────────────────────────────
 *
 *  The annotation overlay is a separate <svg> with viewBox="0 0 1280 720"
 *  drawn on top of the preview. The user drags a rectangle on the
 *  overlay, expressed in viewBox pixels:
 *
 *    bbox_view = { x: viewX, y: viewY, width: w, height: h }   in [0..1280]/[0..720]
 *
 *  We persist + send the rectangle in NORMALIZED coordinates instead:
 *
 *    bbox_norm = {
 *      x:      viewX  / 1280,
 *      y:      viewY  /  720,
 *      width:  w      / 1280,
 *      height: h      /  720,
 *    }   ∈ [0, 1]
 *
 *  When rendering an existing annotation back onto the overlay, we go
 *  the other way (multiply by viewBox dims). When the LLM client serializes
 *  it, the same bbox_norm is used (× 100 → percent string).
 *
 *  ⛔ DO NOT introduce a second pixel-coordinate path. Every place that
 *     touches annotation geometry MUST use bbox_norm as the single source
 *     of truth:
 *       - frontend overlay rendering        ← bbox_norm × viewBox dims
 *       - frontend POST /annotation         ← bbox_norm
 *       - backend review.json storage       ← bbox_norm verbatim
 *       - backend LLM prompt serialization  ← bbox_norm × 100 (percent)
 *
 *  This guarantees the rectangle the user draws at e.g. (10%, 20%, 30%, 5%)
 *  shows up at exactly the same screen location regardless of preview
 *  size, and is described to the LLM with the same percentages.
 *
 * ─────────────────────────────────────────────────────────────────────────────
 */

import { useCallback, useMemo, useRef, useState } from "react";
import {
  BringToFront,
  Copy,
  Eye,
  EyeOff,
  Image as ImageIcon,
  Layers,
  MousePointer2,
  PenSquare,
  Pencil,
  Redo2,
  RotateCcw,
  Save,
  SendToBack,
  Square,
  Table2,
  Trash2,
  Type,
  Undo2,
} from "lucide-react";

import { useLocale } from "../../i18n";
import type { TemplateImportSlide, UserAnnotation } from "../../lib/types";
import type { EditorCommandType, EditorState } from "../preview/KonvaSlideEditor";

const VIEWBOX_W = 1280;
const VIEWBOX_H = 720;
const MIN_DRAG_PX = 16; // viewBox pixels — below this we discard the drag

export type StageMode = "select" | "annotate" | "edit";

export interface SlideStageProps {
  slide: TemplateImportSlide | null;
  templatedSvg?: string | null;
  annotations: UserAnnotation[];
  mode: StageMode;
  onModeChange: (mode: StageMode) => void;
  onCreateAnnotation: (
    bbox_norm: { x: number; y: number; width: number; height: number },
    note: string,
    linkedElementId?: string | null,
  ) => void;
  onUpdateAnnotation: (id: string, patch: Partial<UserAnnotation>) => void;
  onDeleteAnnotation: (id: string) => void;
  onResolveAll?: () => void;
  onSelectElement?: (elementId: string | null) => void;
  slideCount?: number;
  unfilledPlaceholders?: string[];
  editorState?: EditorState;
  onEditorCommand?: (command: EditorCommandType) => void;
  className?: string;
  /** Hide the built-in toolbar (callers can render
   * :func:`SlideStageToolbar` separately for a custom layout). */
  toolbarHidden?: boolean;
  /** Externally controlled "show templated" toggle. */
  showTemplated?: boolean;
  /** Callback paired with ``showTemplated`` for external control. */
  onShowTemplatedChange?: (next: boolean) => void;
}

export interface SlideStageToolbarProps {
  mode: StageMode;
  onModeChange: (mode: StageMode) => void;
  showTemplated: boolean;
  onShowTemplatedChange: (next: boolean) => void;
  templatedAvailable: boolean;
  onResolveAll?: () => void;
  unfilledPlaceholders?: string[];
  editorState?: EditorState;
  onEditorCommand?: (command: EditorCommandType) => void;
  className?: string;
}

interface InProgressRect {
  startView: { x: number; y: number };
  currentView: { x: number; y: number };
}

export function SlideStage(props: SlideStageProps) {
  const {
    slide,
    templatedSvg,
    annotations,
    mode,
    onModeChange,
    onCreateAnnotation,
    onUpdateAnnotation,
    onDeleteAnnotation,
    onResolveAll,
    onSelectElement,
    slideCount: _slideCount,
    unfilledPlaceholders,
    editorState,
    onEditorCommand,
    className,
    toolbarHidden = false,
    showTemplated: showTemplatedProp,
    onShowTemplatedChange,
  } = props;
  const { t } = useLocale();

  const [showTemplatedInternal, setShowTemplatedInternal] = useState(false);
  const showTemplated = showTemplatedProp ?? showTemplatedInternal;
  const setShowTemplated = (next: boolean) => {
    if (onShowTemplatedChange) onShowTemplatedChange(next);
    else setShowTemplatedInternal(next);
  };
  const [drag, setDrag] = useState<InProgressRect | null>(null);
  const [pendingRect, setPendingRect] = useState<{
    x: number;
    y: number;
    width: number;
    height: number;
    linkedElementId?: string | null;
  } | null>(null);
  const [pendingNote, setPendingNote] = useState("");
  const [popoverId, setPopoverId] = useState<string | null>(null);
  const overlayRef = useRef<SVGSVGElement | null>(null);
  const backgroundRef = useRef<HTMLDivElement | null>(null);

  const slideAnnotations = useMemo(
    () => annotations.filter((a) => a.slide_index === (slide?.index ?? -1)),
    [annotations, slide?.index],
  );

  const backgroundSvg = (showTemplated && templatedSvg ? templatedSvg : slide?.preview_svg) ?? "";

  const clientToView = useCallback((clientX: number, clientY: number) => {
    const node = overlayRef.current;
    if (!node) return { x: 0, y: 0 };
    const rect = node.getBoundingClientRect();
    const px = ((clientX - rect.left) / rect.width) * VIEWBOX_W;
    const py = ((clientY - rect.top) / rect.height) * VIEWBOX_H;
    return {
      x: clamp(px, 0, VIEWBOX_W),
      y: clamp(py, 0, VIEWBOX_H),
    };
  }, []);

  const onMouseDown = (event: React.MouseEvent<SVGSVGElement>) => {
    if (mode !== "annotate" || pendingRect) return;
    if (event.button !== 0) return;
    const start = clientToView(event.clientX, event.clientY);
    setDrag({ startView: start, currentView: start });
    setPopoverId(null);
  };
  const onMouseMove = (event: React.MouseEvent<SVGSVGElement>) => {
    if (!drag) return;
    const cur = clientToView(event.clientX, event.clientY);
    setDrag({ ...drag, currentView: cur });
  };
  const onMouseUp = (event: React.MouseEvent<SVGSVGElement>) => {
    if (!drag) return;
    const { startView, currentView } = drag;
    const x = Math.min(startView.x, currentView.x);
    const y = Math.min(startView.y, currentView.y);
    const w = Math.abs(currentView.x - startView.x);
    const h = Math.abs(currentView.y - startView.y);
    setDrag(null);
    if (w < MIN_DRAG_PX || h < MIN_DRAG_PX) {
      const picked = pickElementRectAtPoint(
        event.clientX,
        event.clientY,
        backgroundRef.current,
        overlayRef.current,
      );
      if (picked) {
        setPendingRect(picked);
        setPendingNote("");
      }
      return;
    }
    setPendingRect({ x, y, width: w, height: h });
    setPendingNote("");
  };

  const handleSavePending = () => {
    if (!pendingRect || !slide) return;
    const note = pendingNote.trim();
    if (!note) return;
    onCreateAnnotation(
      {
        x: pendingRect.x / VIEWBOX_W,
        y: pendingRect.y / VIEWBOX_H,
        width: pendingRect.width / VIEWBOX_W,
        height: pendingRect.height / VIEWBOX_H,
      },
      note,
      pendingRect.linkedElementId,
    );
    setPendingRect(null);
    setPendingNote("");
  };

  const handleCancelPending = () => {
    setPendingRect(null);
    setPendingNote("");
  };

  return (
    <div className={`flex h-full flex-col ${className ?? ""}`}>
      {!toolbarHidden ? (
        <SlideStageToolbar
          mode={mode}
          onModeChange={onModeChange}
          showTemplated={showTemplated}
          onShowTemplatedChange={setShowTemplated}
          templatedAvailable={Boolean(templatedSvg)}
          onResolveAll={onResolveAll}
          unfilledPlaceholders={unfilledPlaceholders}
          editorState={editorState}
          onEditorCommand={onEditorCommand}
        />
      ) : null}

      {/* Stage */}
      <div className="relative flex-1 overflow-hidden p-4">
        <div className="ti-stage-container relative mx-auto" style={{ maxWidth: "100%", maxHeight: "100%" }}>
          {/* Background slide preview */}
          <div
            ref={backgroundRef}
            className="absolute inset-0 [&>svg]:h-full [&>svg]:w-full"
            // SVG sanitized via sanitizeSvg.
            dangerouslySetInnerHTML={{ __html: sanitizeSvg(backgroundSvg) }}
            onClick={(e) => {
              if (mode !== "select") return;
              if (!onSelectElement) return;
              const target = e.target as Element | null;
              const elementId = target?.getAttribute?.("data-element-id") ?? null;
              onSelectElement(elementId);
            }}
          />

          {/* Annotation overlay */}
          <svg
            ref={overlayRef}
            className="ti-stage-overlay absolute inset-0"
            viewBox={`0 0 ${VIEWBOX_W} ${VIEWBOX_H}`}
            preserveAspectRatio="none"
            style={{
              pointerEvents: mode === "annotate" || pendingRect ? "auto" : "none",
              cursor: mode === "annotate" && !pendingRect ? "crosshair" : "default",
            }}
            onMouseDown={onMouseDown}
            onMouseMove={onMouseMove}
            onMouseUp={onMouseUp}
            onMouseLeave={() => setDrag(null)}
          >
            {slideAnnotations.map((a, i) => (
              <PersistedRect
                key={a.annotation_id}
                annotation={a}
                index={i + 1}
                onClick={() => setPopoverId(a.annotation_id)}
              />
            ))}

            {drag ? <LiveRect drag={drag} /> : null}
            {pendingRect ? (
              <rect
                x={pendingRect.x}
                y={pendingRect.y}
                width={pendingRect.width}
                height={pendingRect.height}
                fill="color-mix(in srgb, var(--ti-warning) 18%, transparent)"
                stroke="var(--ti-warning)"
                strokeWidth={1.5}
              />
            ) : null}
          </svg>

          {/* Pending-annotation note editor */}
          {pendingRect ? (
            <PendingNoteEditor
              rect={pendingRect}
              note={pendingNote}
              onChangeNote={setPendingNote}
              onSave={handleSavePending}
              onCancel={handleCancelPending}
            />
          ) : null}

          {/* Existing annotation popover */}
          {popoverId ? (
            <AnnotationPopover
              annotation={slideAnnotations.find((a) => a.annotation_id === popoverId) ?? null}
              onClose={() => setPopoverId(null)}
              onUpdate={onUpdateAnnotation}
              onDelete={(id) => {
                onDeleteAnnotation(id);
                setPopoverId(null);
              }}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}

// ── Subcomponents ─────────────────────────────────────────────────────────

export function SlideStageToolbar({
  mode,
  onModeChange,
  showTemplated,
  onShowTemplatedChange,
  templatedAvailable,
  onResolveAll,
  unfilledPlaceholders,
  editorState,
  onEditorCommand,
  className,
}: SlideStageToolbarProps) {
  const { t } = useLocale();
  const [showUnfilled, setShowUnfilled] = useState(false);
  return (
    <div
      className={`flex flex-wrap items-center gap-2 border-b px-3 py-2 ${className ?? ""}`}
      style={{ borderColor: "var(--ti-line)", background: "var(--ti-surface)" }}
    >
      <ModeToggle mode={mode} onChange={onModeChange} />
      {mode === "edit" && editorState && onEditorCommand ? (
        <TemplateEditToolbar editorState={editorState} onCommand={onEditorCommand} />
      ) : null}
      <button
        type="button"
        onClick={() => onShowTemplatedChange(!showTemplated)}
        className="ti-focusable inline-flex items-center gap-1 rounded-[var(--ti-radius-sm,6px)] border px-2 py-1 text-xs font-semibold"
        style={{
          borderColor: "var(--ti-line)",
          background: showTemplated ? "var(--ti-accent)" : "var(--ti-surface)",
          color: showTemplated ? "var(--ti-accent-fg)" : "var(--ti-text)",
        }}
        disabled={!templatedAvailable}
        title={t("template.toolbar.showTemplated")}
      >
        {showTemplated ? <EyeOff size={12} /> : <Eye size={12} />}
        {t("template.toolbar.showTemplated")}
      </button>
      {onResolveAll ? (
        <button
          type="button"
          onClick={onResolveAll}
          className="ti-focusable inline-flex items-center gap-1 rounded-[var(--ti-radius-sm,6px)] border px-2 py-1 text-xs font-semibold"
          style={{
            borderColor: "var(--ti-line)",
            background: "var(--ti-surface)",
            color: "var(--ti-text)",
          }}
        >
          <RotateCcw size={12} />
          {t("template.toolbar.resolveAll")}
        </button>
      ) : null}
      {unfilledPlaceholders && unfilledPlaceholders.length > 0 ? (
        <div className="relative">
          <button
            type="button"
            onClick={() => setShowUnfilled((v) => !v)}
            className="ti-focusable inline-flex items-center gap-1 rounded-full border px-2 py-1 text-[11px] font-medium"
            style={{
              borderColor: "color-mix(in srgb, var(--ti-warning) 50%, var(--ti-line))",
              background: "color-mix(in srgb, var(--ti-warning) 14%, transparent)",
              color: "var(--ti-warning)",
            }}
          >
            {t("template.unfilledCount").replace("{n}", String(unfilledPlaceholders.length))}
          </button>
          {showUnfilled ? (
            <div
              className="absolute right-0 z-30 mt-1 w-56 rounded-[var(--ti-radius-sm,6px)] border p-2 text-xs"
              style={{
                background: "var(--ti-surface)",
                borderColor: "var(--ti-line)",
                boxShadow: "0 6px 18px rgba(0,0,0,0.12)",
              }}
            >
              <ul className="flex flex-col gap-1">
                {unfilledPlaceholders.map((name) => (
                  <li key={name} style={{ color: "var(--ti-text)" }}>
                    <code style={{ background: "var(--ti-surface-inset)", padding: "1px 4px", borderRadius: 4 }}>
                      {`{{${name}}}`}
                    </code>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ModeToggle({ mode, onChange }: { mode: StageMode; onChange: (m: StageMode) => void }) {
  const { t } = useLocale();
  return (
    <div
      role="tablist"
      className="inline-flex overflow-hidden rounded-[var(--ti-radius-sm,6px)] border"
      style={{ borderColor: "var(--ti-line)", background: "var(--ti-surface-inset)" }}
    >
      {(["select", "annotate", "edit"] as StageMode[]).map((m) => {
        const active = mode === m;
        return (
          <button
            key={m}
            role="tab"
            aria-selected={active}
            type="button"
            onClick={() => onChange(m)}
            className="ti-focusable inline-flex items-center gap-1 px-2.5 py-1 text-xs font-semibold"
            style={{
              background: active ? "var(--ti-surface)" : "transparent",
              color: active ? "var(--ti-accent)" : "var(--ti-muted)",
            }}
          >
            {m === "select" ? <MousePointer2 size={12} /> : m === "annotate" ? <PenSquare size={12} /> : <Pencil size={12} />}
            {t(`template.toolbar.${m}`)}
          </button>
        );
      })}
    </div>
  );
}

function TemplateEditToolbar({
  editorState,
  onCommand,
}: {
  editorState: EditorState;
  onCommand: (command: EditorCommandType) => void;
}) {
  const { t } = useLocale();
  const run = (command: EditorCommandType) => onCommand(command);
  return (
    <div className="template-edit-toolbar" aria-label={t("template.toolbar.edit")}>
      <button type="button" title={t("editor.textTool")} onClick={() => run("addText")}><Type size={13} /></button>
      <button type="button" title={t("editor.shapeTool")} onClick={() => run("addRect")}><Square size={13} /></button>
      <button type="button" title={t("editor.pictureTool")} onClick={() => run("addImage")}><ImageIcon size={13} /></button>
      <button type="button" title={t("editor.tableTool")} onClick={() => run("addTable")}><Table2 size={13} /></button>
      <span className="toolbar-divider" />
      <button type="button" title={t("editor.undo")} disabled={!editorState.canUndo} onClick={() => run("undo")}><Undo2 size={13} /></button>
      <button type="button" title={t("editor.redo")} disabled={!editorState.canRedo} onClick={() => run("redo")}><Redo2 size={13} /></button>
      <button type="button" title={t("editor.duplicate")} disabled={!editorState.selectedType} onClick={() => run("duplicate")}><Copy size={13} /></button>
      <button type="button" title={t("editor.delete")} disabled={!editorState.selectedType} onClick={() => run("delete")}><Trash2 size={13} /></button>
      <span className="toolbar-divider" />
      <button type="button" title={t("editor.sendBackward")} disabled={!editorState.selectedType} onClick={() => run("backward")}><SendToBack size={13} /></button>
      <button type="button" title={t("editor.bringForward")} disabled={!editorState.selectedType} onClick={() => run("forward")}><BringToFront size={13} /></button>
      <button type="button" title={t("editor.autosave")} onClick={() => run("toggleAutosave")}>
        <Layers size={13} />
        <span>{editorState.autoSave ? t("editor.autosave") : t("editor.manual")}</span>
      </button>
      <button type="button" title={t("editor.saveEdits")} disabled={editorState.saveState === "saving"} onClick={() => run("save")}>
        <Save size={13} />
        <span>{editorState.saveState === "saving" ? t("editor.saving") : editorState.saveState === "saved" ? t("editor.saved") : t("editor.save")}</span>
      </button>
    </div>
  );
}

function PersistedRect({
  annotation,
  index,
  onClick,
}: {
  annotation: UserAnnotation;
  index: number;
  onClick: () => void;
}) {
  const x = annotation.bbox_norm.x * VIEWBOX_W;
  const y = annotation.bbox_norm.y * VIEWBOX_H;
  const w = annotation.bbox_norm.width * VIEWBOX_W;
  const h = annotation.bbox_norm.height * VIEWBOX_H;
  const resolved = annotation.resolved;
  return (
    <g style={{ cursor: "pointer" }} onClick={onClick}>
      <title>{`${index}. ${annotation.note}`}</title>
      <rect
        className={`ti-annotation-rect${resolved ? " is-resolved" : ""}`}
        x={x}
        y={y}
        width={w}
        height={h}
        fill={
          resolved
            ? "color-mix(in srgb, var(--ti-success) 10%, transparent)"
            : "color-mix(in srgb, var(--ti-warning) 18%, transparent)"
        }
        stroke={resolved ? "var(--ti-success)" : "var(--ti-warning)"}
        strokeWidth={1.5}
        opacity={resolved ? 0.6 : 1}
      />
      <g transform={`translate(${x + 4}, ${y + 4})`}>
        <rect
          width={20}
          height={20}
          rx={4}
          fill={resolved ? "var(--ti-success)" : "var(--ti-warning)"}
        />
        <text
          x={10}
          y={14}
          textAnchor="middle"
          fontSize={11}
          fontWeight={700}
          fill="white"
        >
          {resolved ? "✓" : index}
        </text>
      </g>
    </g>
  );
}

function LiveRect({ drag }: { drag: InProgressRect }) {
  const x = Math.min(drag.startView.x, drag.currentView.x);
  const y = Math.min(drag.startView.y, drag.currentView.y);
  const w = Math.abs(drag.currentView.x - drag.startView.x);
  const h = Math.abs(drag.currentView.y - drag.startView.y);
  return (
    <rect
      x={x}
      y={y}
      width={w}
      height={h}
      fill="color-mix(in srgb, var(--ti-accent) 12%, transparent)"
      stroke="var(--ti-accent)"
      strokeWidth={1.5}
      strokeDasharray="6 4"
    />
  );
}

function PendingNoteEditor({
  rect,
  note,
  onChangeNote,
  onSave,
  onCancel,
}: {
  rect: { x: number; y: number; width: number; height: number };
  note: string;
  onChangeNote: (note: string) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  const { t } = useLocale();
  // Anchor the editor relative to the rect, in the overlay's % space so it
  // scales with the stage container. Default below the rect; flip above
  // when there's <30% room below so the textarea + buttons stay visible
  // even for annotations near the bottom of the stage.
  const bottomYPct = ((rect.y + rect.height) / VIEWBOX_H) * 100;
  const flipUp = bottomYPct > 70;
  const left = `${(rect.x / VIEWBOX_W) * 100}%`;
  const top = flipUp
    ? `${(rect.y / VIEWBOX_H) * 100}%`
    : `${bottomYPct}%`;
  return (
    <div
      className="absolute z-20 flex w-72 flex-col gap-2 rounded-[var(--ti-radius-md,10px)] border p-2 shadow-lg"
      style={{
        left,
        top,
        transform: flipUp ? "translateY(calc(-100% - 8px))" : "translateY(8px)",
        background: "var(--ti-surface)",
        borderColor: "var(--ti-line)",
        color: "var(--ti-text)",
      }}
    >
      <textarea
        value={note}
        onChange={(e) => onChangeNote(e.target.value)}
        placeholder={t("template.annotation.notePlaceholder")}
        rows={2}
        autoFocus
        className="ti-focusable w-full resize-none rounded-[var(--ti-radius-sm,6px)] border p-1.5 text-sm"
        style={{
          borderColor: "var(--ti-line)",
          background: "var(--ti-surface)",
          color: "var(--ti-text)",
        }}
      />
      <div className="flex justify-end gap-1">
        <button
          type="button"
          onClick={onCancel}
          className="ti-focusable rounded-[var(--ti-radius-sm,6px)] border px-2 py-1 text-xs"
          style={{ borderColor: "var(--ti-line)", color: "var(--ti-muted)" }}
        >
          {t("template.annotation.cancel")}
        </button>
        <button
          type="button"
          onClick={onSave}
          disabled={!note.trim()}
          className="ti-focusable rounded-[var(--ti-radius-sm,6px)] px-2 py-1 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-60"
          style={{ background: "var(--ti-accent)", color: "var(--ti-accent-fg)" }}
        >
          {t("template.annotation.save")}
        </button>
      </div>
    </div>
  );
}

function AnnotationPopover({
  annotation,
  onClose,
  onUpdate,
  onDelete,
}: {
  annotation: UserAnnotation | null;
  onClose: () => void;
  onUpdate: (id: string, patch: Partial<UserAnnotation>) => void;
  onDelete: (id: string) => void;
}) {
  const { t } = useLocale();
  if (!annotation) return null;
  // Mirror PendingNoteEditor: flip popover above the annotation when it
  // sits in the lower 30% of the stage so action buttons stay visible.
  const bottomYPct = (annotation.bbox_norm.y + annotation.bbox_norm.height) * 100;
  const flipUp = bottomYPct > 70;
  const left = `${annotation.bbox_norm.x * 100}%`;
  const top = flipUp
    ? `${annotation.bbox_norm.y * 100}%`
    : `${bottomYPct}%`;
  return (
    <div
      className="absolute z-20 flex w-64 flex-col gap-2 rounded-[var(--ti-radius-md,10px)] border p-2 shadow-lg"
      style={{
        left,
        top,
        transform: flipUp ? "translateY(calc(-100% - 8px))" : "translateY(8px)",
        background: "var(--ti-surface)",
        borderColor: "var(--ti-line)",
        color: "var(--ti-text)",
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <p
        className="text-sm"
        style={{
          textDecoration: annotation.resolved ? "line-through" : "none",
          color: annotation.resolved ? "var(--ti-muted)" : "var(--ti-text)",
        }}
      >
        {annotation.note}
      </p>
      <div className="flex justify-between gap-1">
        <button
          type="button"
          onClick={() =>
            onUpdate(annotation.annotation_id, { resolved: !annotation.resolved })
          }
          className="ti-focusable rounded-[var(--ti-radius-sm,6px)] border px-2 py-1 text-xs font-semibold"
          style={{
            borderColor: "var(--ti-line)",
            color: annotation.resolved ? "var(--ti-muted)" : "var(--ti-success)",
            background: "var(--ti-surface)",
          }}
        >
          {annotation.resolved
            ? t("template.annotation.unresolve")
            : t("template.annotation.resolve")}
        </button>
        <button
          type="button"
          onClick={() => onDelete(annotation.annotation_id)}
          aria-label={t("template.annotation.delete")}
          className="ti-focusable inline-flex items-center gap-1 rounded-[var(--ti-radius-sm,6px)] border px-2 py-1 text-xs font-semibold"
          style={{
            borderColor: "color-mix(in srgb, var(--ti-danger) 40%, var(--ti-line))",
            color: "var(--ti-danger)",
            background: "var(--ti-surface)",
          }}
        >
          <Trash2 size={12} />
          {t("template.annotation.delete")}
        </button>
        <button
          type="button"
          onClick={onClose}
          className="ti-focusable rounded-[var(--ti-radius-sm,6px)] border px-2 py-1 text-xs"
          style={{ borderColor: "var(--ti-line)", color: "var(--ti-muted)" }}
        >
          {t("common.close")}
        </button>
      </div>
    </div>
  );
}

// ── helpers ──────────────────────────────────────────────────────────────

function pickElementRectAtPoint(
  clientX: number,
  clientY: number,
  background: HTMLDivElement | null,
  overlay: SVGSVGElement | null,
): { x: number; y: number; width: number; height: number; linkedElementId?: string | null } | null {
  if (!background || !overlay) return null;
  const overlayRect = overlay.getBoundingClientRect();
  const picked = document
    .elementsFromPoint(clientX, clientY)
    .find((element) => background.contains(element) && isAnnotatableSvgElement(element));
  if (!picked) return null;
  const rect = picked.getBoundingClientRect();
  if (rect.width < 4 || rect.height < 4) return null;
  const areaRatio = (rect.width * rect.height) / Math.max(1, overlayRect.width * overlayRect.height);
  if (areaRatio > 0.92) return null;
  const x = clamp(((rect.left - overlayRect.left) / overlayRect.width) * VIEWBOX_W, 0, VIEWBOX_W);
  const y = clamp(((rect.top - overlayRect.top) / overlayRect.height) * VIEWBOX_H, 0, VIEWBOX_H);
  const right = clamp(((rect.right - overlayRect.left) / overlayRect.width) * VIEWBOX_W, 0, VIEWBOX_W);
  const bottom = clamp(((rect.bottom - overlayRect.top) / overlayRect.height) * VIEWBOX_H, 0, VIEWBOX_H);
  const width = Math.max(4, right - x);
  const height = Math.max(4, bottom - y);
  return {
    x,
    y,
    width,
    height,
    linkedElementId:
      picked.getAttribute("data-element-id") ||
      picked.getAttribute("id") ||
      picked.tagName.toLowerCase(),
  };
}

function isAnnotatableSvgElement(element: Element): boolean {
  const tag = element.tagName.toLowerCase();
  if (["svg", "g", "defs", "clippath", "title"].includes(tag)) return false;
  if (["text", "image", "rect", "path", "polygon", "polyline", "circle", "ellipse", "line", "foreignobject"].includes(tag)) {
    const style = window.getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity || "1") > 0.01;
  }
  return false;
}

function clamp(value: number, lo: number, hi: number): number {
  return Math.min(Math.max(value, lo), hi);
}

function sanitizeSvg(svg: string): string {
  return (svg ?? "")
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "")
    .replace(/\son\w+\s*=\s*"[^"]*"/gi, "")
    .replace(/\son\w+\s*=\s*'[^']*'/gi, "")
    .replace(/javascript:/gi, "");
}
