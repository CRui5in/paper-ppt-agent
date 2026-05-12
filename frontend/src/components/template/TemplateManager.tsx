import { useCallback, useEffect, useRef, useState } from "react";
import { Layers, Loader2, Trash2, Upload, X } from "lucide-react";
import { useLocale } from "../../i18n";
import {
  deleteTemplate,
  fetchImportStatus,
  fetchImportedTemplates,
  fetchTemplatePreview,
  uploadTemplatePptx,
} from "../../lib/api";
import type { ImportStatus, TemplatePreview, UserTemplateItem } from "../../lib/types";

interface TemplateManagerProps {
  open: boolean;
  onClose: () => void;
  onSelect: (templateId: string) => void;
}

type View = "list" | "importing" | "preview";

export function TemplateManager({ open, onClose, onSelect }: TemplateManagerProps) {
  const { t } = useLocale();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [view, setView] = useState<View>("list");
  const [imported, setImported] = useState<UserTemplateItem[]>([]);
  const [importStatus, setImportStatus] = useState<ImportStatus | null>(null);
  const [preview, setPreview] = useState<TemplatePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [importedLoading, setImportedLoading] = useState(false);
  const [previewLoadingId, setPreviewLoadingId] = useState<string | null>(null);

  const loadImported = useCallback(async () => {
    setImportedLoading(true);
    try {
      const list = await fetchImportedTemplates();
      setImported(list);
    } catch {
      /* ignore */
    } finally {
      setImportedLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) {
      void loadImported();
      setView("list");
      setPreview(null);
      setError(null);
    }
  }, [open, loadImported]);

  // Poll import status
  useEffect(() => {
    if (view !== "importing" || !importStatus?.import_id) return;
    if (importStatus.status === "complete" || importStatus.status === "error") return;

    const timer = setInterval(async () => {
      try {
        const status = await fetchImportStatus(importStatus.import_id);
        setImportStatus(status);
        if (status.status === "complete") {
          clearInterval(timer);
          // Load preview
          if (status.template_id) {
            try {
              const pv = await fetchTemplatePreview(status.template_id);
              setPreview(pv);
              setView("preview");
            } catch {
              setView("list");
            }
            void loadImported();
          }
        } else if (status.status === "error") {
          clearInterval(timer);
          setError(status.error || t("template.importError"));
        }
      } catch {
        /* ignore poll errors */
      }
    }, 2000);

    return () => clearInterval(timer);
  }, [view, importStatus, loadImported, t]);

  const handleFile = async (file: File) => {
    if (!file.name.toLowerCase().endsWith(".pptx")) {
      setError("Only .pptx files are accepted.");
      return;
    }
    setError(null);
    setView("importing");
    try {
      const resp = await uploadTemplatePptx(file);
      setImportStatus({ import_id: resp.import_id, status: "processing" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
      setView("list");
    }
  };

  const handleDelete = async (templateId: string) => {
    if (!window.confirm(t("template.deleteConfirm"))) return;
    try {
      await deleteTemplate(templateId);
      void loadImported();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    }
  };

  const handleUse = (templateId: string) => {
    onSelect(templateId);
    onClose();
  };

  const getExportModeLabel = (mode?: string) => {
    if (!mode || mode === "metadata-only") return t("template.exportMetadata");
    if (mode.startsWith("powerpoint")) return t("template.exportPowerpoint");
    if (mode.startsWith("pymupdf")) return t("template.exportPymupdf");
    return mode;
  };

  if (!open) return null;

  return (
    <div className="template-manager-overlay" onClick={onClose}>
      <div className="template-manager" onClick={(e) => e.stopPropagation()}>
        <div className="template-manager-header">
          <div className="panel-title-row">
            <Layers size={15} className="panel-title-icon" />
            <p className="panel-title">{t("template.title")}</p>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label={t("common.close")}>
            <X size={17} />
          </button>
        </div>

        <div className="template-manager-body">
          {error ? <p className="error-text">{error}</p> : null}

          {/* ── List view ── */}
          {view === "list" && (
            <>
              {/* Upload area */}
              <div
                className={`upload-zone ${isDragging ? "upload-zone-dragging" : ""}`}
                onClick={() => fileInputRef.current?.click()}
                onDragOver={(e) => {
                  e.preventDefault();
                  setIsDragging(true);
                }}
                onDragLeave={() => setIsDragging(false)}
                onDrop={(e) => {
                  e.preventDefault();
                  setIsDragging(false);
                  const file = e.dataTransfer.files[0];
                  if (file) void handleFile(file);
                }}
              >
                <Upload size={24} style={{ marginBottom: 8, opacity: 0.6 }} />
                <p className="upload-copy">{t("template.uploadHint")}</p>
                <p className="muted-copy">.pptx</p>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".pptx"
                  className="hidden-input"
                  onChange={(e) => {
                    const file = e.currentTarget.files?.[0];
                    if (file) void handleFile(file);
                    e.currentTarget.value = "";
                  }}
                />
              </div>

              {/* Imported templates list */}
              <div className="template-list">
                {importedLoading ? (
                  <>
                    <div className="template-preview-loading motion-skeleton" />
                    <div className="template-preview-loading motion-skeleton" />
                  </>
                ) : imported.length === 0 ? (
                  <p className="muted-copy" style={{ textAlign: "center", padding: "1rem" }}>
                    {t("template.noTemplates")}
                  </p>
                ) : (
                  imported.map((tmpl) => (
                    <div key={tmpl.template_id} className="template-list-item">
                      <div className="template-list-info">
                        <strong>{tmpl.label}</strong>
                        <span className="muted-copy">
                          {tmpl.slide_count ?? 0} {t("template.slideCount")}
                        </span>
                      </div>
                      <div className="template-list-actions">
                        <button
                          type="button"
                          className="primary-button"
                          style={{ padding: "6px 12px", fontSize: 13 }}
                          onClick={() => handleUse(tmpl.template_id)}
                        >
                          {t("template.useThis")}
                        </button>
                        <button
                          type="button"
                          className="ghost-button"
                          style={{ padding: "6px 8px" }}
                          onClick={() => {
                            setPreviewLoadingId(tmpl.template_id);
                            void fetchTemplatePreview(tmpl.template_id)
                              .then((pv) => {
                                setPreview(pv);
                                setView("preview");
                              })
                              .finally(() => setPreviewLoadingId(null));
                          }}
                          disabled={previewLoadingId === tmpl.template_id}
                        >
                          {previewLoadingId === tmpl.template_id ? <Loader2 size={14} className="spin" /> : null}
                          {t("template.preview")}
                        </button>
                        <button
                          type="button"
                          className="ghost-button danger-button"
                          style={{ padding: "6px 8px" }}
                          onClick={() => void handleDelete(tmpl.template_id)}
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </div>
                  ))
                )}
              </div>
            </>
          )}

          {/* ── Importing view ── */}
          {view === "importing" && (
            <div className="template-importing">
              <Loader2 size={32} className="spin" style={{ marginBottom: 12 }} />
              <p>{t("template.processing")}</p>
              {importStatus?.slide_count ? (
                <p className="muted-copy">{importStatus.slide_count} {t("template.slideCount")}</p>
              ) : null}
            </div>
          )}

          {/* ── Preview view ── */}
          {view === "preview" && preview && (
            <div className="template-preview">
              <div className="template-preview-header">
                <h3>{preview.label}</h3>
                {importStatus?.export_mode && (
                  <span className="muted-copy">
                    {t("template.exportMode")}: {getExportModeLabel(importStatus.export_mode)}
                  </span>
                )}
              </div>

              {/* Theme colors */}
              {preview.theme_colors && preview.theme_colors.length > 0 && (
                <div className="template-theme-colors">
                  <span>{t("template.themeColors")}:</span>
                  {preview.theme_colors.map((color, i) => (
                    <span
                      key={i}
                      className="template-color-swatch"
                      style={{ backgroundColor: color }}
                      title={color}
                    />
                  ))}
                </div>
              )}

              {/* SVG previews */}
              <div className="template-preview-slides">
                {preview.cover_svg && (
                  <div className="template-preview-slide">
                    <span className="muted-copy">Cover</span>
                    <div
                      className="template-preview-frame"
                      dangerouslySetInnerHTML={{ __html: preview.cover_svg }}
                    />
                  </div>
                )}
                {preview.content_svg && (
                  <div className="template-preview-slide">
                    <span className="muted-copy">Content</span>
                    <div
                      className="template-preview-frame"
                      dangerouslySetInnerHTML={{ __html: preview.content_svg }}
                    />
                  </div>
                )}
              </div>

              {/* Actions */}
              <div className="template-preview-actions">
                <button
                  type="button"
                  className="primary-button"
                  onClick={() => handleUse(preview.template_id)}
                >
                  {t("template.useThis")}
                </button>
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => {
                    setView("list");
                    setPreview(null);
                  }}
                >
                  {t("common.close")}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
