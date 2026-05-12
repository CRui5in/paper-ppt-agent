import { useCallback, useEffect, useState } from "react";
import { deleteVersion, fetchVersion, listVersions } from "../../lib/api";
import type { VersionDetailResponse, VersionItem } from "../../lib/types";
import { useLocale } from "../../i18n";
import { Loader2, RefreshCw, X } from "lucide-react";
import { Button } from "../ui/button";

interface VersionHistoryProps {
  jobId: string | null;
  onError?: (message: string) => void;
}

export function VersionHistory({ jobId, onError }: VersionHistoryProps) {
  const { t } = useLocale();
  const [versions, setVersions] = useState<VersionItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<VersionDetailResponse | null>(null);
  const [openedSlideIndex, setOpenedSlideIndex] = useState<number | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const loadVersions = useCallback(async () => {
    if (!jobId) return;
    setLoading(true);
    setError(null);
    try {
      const response = await listVersions(jobId);
      setVersions(response.versions);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to load versions.";
      setError(message);
      onError?.(message);
    } finally {
      setLoading(false);
    }
  }, [jobId]);

  useEffect(() => {
    void loadVersions();
  }, [loadVersions]);

  const handleOpen = async (version: VersionItem) => {
    if (!jobId) return;
    setDetailLoading(true);
    setError(null);
    try {
      const detail = await fetchVersion(jobId, version.name);
      setSelected(detail);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to load version.";
      setError(message);
      onError?.(message);
    } finally {
      setDetailLoading(false);
    }
  };

  const handleDelete = async (version: VersionItem) => {
    if (!jobId) return;
    // eslint-disable-next-line no-alert
    if (!window.confirm(t("versions.confirmDelete"))) return;
    try {
      await deleteVersion(jobId, version.name);
      if (selected?.name === version.name) {
        setSelected(null);
        setOpenedSlideIndex(null);
      }
      await loadVersions();
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to delete version.";
      setError(message);
      onError?.(message);
    }
  };

  if (!jobId) return null;

  return (
    <section className="versions-panel">
      <div className="versions-header">
        <h2>{t("versions.title")}</h2>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="versions-refresh-button"
          onClick={() => void loadVersions()}
          disabled={loading}
        >
          <RefreshCw size={14} className={loading ? "spin" : undefined} />
          {loading ? t("versions.loading") : t("versions.refresh")}
        </Button>
      </div>
      {error ? <p className="error-text">{error}</p> : null}
      {loading && versions.length === 0 ? (
        <div className="versions-list">
          <div className="versions-loading-row motion-skeleton" />
          <div className="versions-loading-row motion-skeleton" />
        </div>
      ) : versions.length === 0 ? (
        <p className="versions-empty muted-copy">{t("versions.empty")}</p>
      ) : (
        <ul className="versions-list">
          {versions.map((version) => (
            <li key={version.name} className="versions-item">
              <div className="versions-item-main">
                <strong>{t("versions.round")} #{version.round}</strong>
                <span className="muted-copy">
                  {version.slide_count} {t("versions.slides")}
                </span>
                {version.created_at ? (
                  <span className="muted-copy versions-timestamp">
                    {new Date(version.created_at * 1000).toLocaleString()}
                  </span>
                ) : null}
              </div>
              <div className="versions-item-actions">
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => void handleOpen(version)}
                  disabled={detailLoading}
                >
                  {detailLoading ? <Loader2 size={13} className="spin" /> : null}
                  {t("versions.view")}
                </button>
                <button
                  type="button"
                  className="ghost-button ghost-danger"
                  onClick={() => void handleDelete(version)}
                >
                  {t("versions.delete")}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {detailLoading && !selected ? (
        <div className="versions-detail">
          <div className="versions-loading-frame motion-skeleton" />
        </div>
      ) : selected ? (
        <div className="versions-detail">
          <div className="versions-detail-header">
            <strong>{selected.name}</strong>
            <button
              type="button"
              className="ghost-button"
              onClick={() => setSelected(null)}
            >
              {t("versions.close")}
            </button>
          </div>
          <div className="versions-slide-grid">
            {selected.slides.map((slide) => (
              <button
                type="button"
                key={slide.index}
                className="versions-slide versions-slide-button"
                onClick={() => setOpenedSlideIndex(slide.index)}
              >
                <div
                  className="versions-slide-frame"
                  dangerouslySetInnerHTML={{ __html: slide.content }}
                />
                <div className="versions-slide-caption">
                  #{slide.index} {slide.name}
                </div>
              </button>
            ))}
          </div>
        </div>
      ) : null}
      {selected && openedSlideIndex !== null ? (
        <div className="versions-slide-overlay" role="dialog" aria-modal="true" onClick={() => setOpenedSlideIndex(null)}>
          <button
            type="button"
            className="versions-slide-overlay-close"
            aria-label={t("versions.close")}
            onClick={(event) => {
              event.stopPropagation();
              setOpenedSlideIndex(null);
            }}
          >
            <X size={18} />
          </button>
          <div
            className="versions-slide-overlay-frame"
            onClick={(event) => event.stopPropagation()}
            dangerouslySetInnerHTML={{
              __html: selected.slides.find((slide) => slide.index === openedSlideIndex)?.content ?? "",
            }}
          />
        </div>
      ) : null}
    </section>
  );
}
