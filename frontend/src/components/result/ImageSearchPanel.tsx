import { Download, HelpCircle, ImagePlus, Key, Loader2, RotateCcw, Search, X } from "lucide-react";
import { useCallback, useRef, useState } from "react";
import { useLocale } from "../../i18n";
import type { ImageSearchResultItem } from "../../lib/types";

const TAVILY_KEY_STORAGE = "paper-ppt-agent-image-search-tavily-key";
const SERPAPI_KEY_STORAGE = "paper-ppt-agent-image-search-serpapi-key";
const ROUTING_PROFILE_STORAGE_KEY = "paper-ppt-agent-routing-profiles-v1";

interface RoutingProfile {
  model: string;
  baseUrl?: string;
  apiKey: string;
}

function readSavedKey(storageKey: string): string {
  try {
    return window.localStorage.getItem(storageKey) ?? "";
  } catch {
    return "";
  }
}

function saveKey(storageKey: string, value: string) {
  try {
    window.localStorage.setItem(storageKey, value);
  } catch {
    // ignore
  }
}

function readLlmProfile(): { provider: string; model: string; apiKey: string; baseUrl?: string } | null {
  try {
    const raw = window.localStorage.getItem(ROUTING_PROFILE_STORAGE_KEY);
    if (!raw) return null;
    const profiles = JSON.parse(raw) as Record<string, RoutingProfile>;
    for (const [provider, p] of Object.entries(profiles)) {
      if (p?.apiKey) {
        return { provider, model: p.model, apiKey: p.apiKey, baseUrl: p.baseUrl };
      }
    }
  } catch {
    // ignore
  }
  return null;
}

interface ImageSearchPanelProps {
  jobId: string;
  slideIndex: number;
  slideTitle?: string;
  onImageApplied: () => void;
}

export function ImageSearchPanel({
  jobId,
  slideIndex,
  slideTitle,
  onImageApplied,
}: ImageSearchPanelProps) {
  const { t } = useLocale();
  const [query, setQuery] = useState("");
  const [tavilyKey, setTavilyKey] = useState(() => readSavedKey(TAVILY_KEY_STORAGE));
  const [serpapiKey, setSerpapiKey] = useState(() => readSavedKey(SERPAPI_KEY_STORAGE));
  const [showKeys, setShowKeys] = useState(false);
  const [results, setResults] = useState<ImageSearchResultItem[]>([]);
  const [searching, setSearching] = useState(false);
  const [applying, setApplying] = useState(false);
  const [undoing, setUndoing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [selectedItem, setSelectedItem] = useState<ImageSearchResultItem | null>(null);
  const [hasApplied, setHasApplied] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const hasSearchKey = tavilyKey.trim() || serpapiKey.trim();

  const handleSearch = useCallback(async () => {
    const trimmed = query.trim();
    if (!trimmed) return;

    if (!hasSearchKey) {
      setShowKeys(true);
      setError("Please enter a Tavily or SerpAPI key first.");
      return;
    }

    setSearching(true);
    setError(null);
    setStatusMsg(null);
    setResults([]);
    setSelectedItem(null);
    setHasApplied(false);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const resp = await fetch(`/api/image-search/${jobId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query: trimmed,
          slide_index: slideIndex,
          max_results: 8,
          tavily_api_key: tavilyKey.trim() || undefined,
          serpapi_key: serpapiKey.trim() || undefined,
        }),
        signal: controller.signal,
      });

      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.detail || `Search failed (${resp.status})`);
      }

      const data = await resp.json();
      setResults(data.results || []);
      if (!data.results?.length) {
        setStatusMsg("No images found. Try different keywords.");
      }
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        setError((err as Error).message);
      }
    } finally {
      setSearching(false);
    }
  }, [query, jobId, slideIndex, tavilyKey, serpapiKey, hasSearchKey]);

  const handleApply = useCallback(async () => {
    if (!selectedItem) return;

    setApplying(true);
    setError(null);
    setStatusMsg(null);

    const llm = readLlmProfile();

    try {
      const resp = await fetch(`/api/image-search/${jobId}/apply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          image_url: selectedItem.url,
          slide_index: slideIndex,
          image_description: selectedItem.description || query,
          api_key: llm?.apiKey,
          provider: llm?.provider || "openai",
          model: llm?.model || "gpt-4o",
          base_url: llm?.baseUrl,
        }),
      });

      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.detail || `Apply failed (${resp.status})`);
      }

      const data = await resp.json();
      setHasApplied(true);

      if (data.action === "replaced") {
        setStatusMsg("Image replaced successfully.");
      } else if (data.action?.startsWith("inserted")) {
        const method = data.action === "inserted_ai" ? "AI placement" : "auto placement";
        setStatusMsg(`Image inserted (${method}). Undo available.`);
      }

      onImageApplied();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setApplying(false);
    }
  }, [jobId, slideIndex, query, selectedItem, onImageApplied]);

  const handleUndo = useCallback(async () => {
    setUndoing(true);
    setError(null);
    setStatusMsg(null);

    try {
      const resp = await fetch(`/api/image-search/${jobId}/undo`, {
        method: "POST",
      });

      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.detail || `Undo failed (${resp.status})`);
      }

      setSelectedItem(null);
      setHasApplied(false);
      setStatusMsg("Undone. Slide restored to previous state.");
      onImageApplied();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setUndoing(false);
    }
  }, [jobId, onImageApplied]);

  const handleDownload = useCallback(async (item: ImageSearchResultItem) => {
    const url = item.url;
    if (!url) return;
    try {
      const resp = await fetch(url);
      const blob = await resp.blob();
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = blobUrl;
      // Guess extension from content-type
      const ext = blob.type.split("/")[1] || "png";
      a.download = `image.${ext}`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(blobUrl);
    } catch {
      // Fallback: open in new tab
      window.open(url, "_blank", "noopener");
    }
  }, []);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter") {
        e.preventDefault();
        handleSearch();
      }
    },
    [handleSearch],
  );

  const handleSaveKeys = useCallback(() => {
    saveKey(TAVILY_KEY_STORAGE, tavilyKey);
    saveKey(SERPAPI_KEY_STORAGE, serpapiKey);
    setShowKeys(false);
  }, [tavilyKey, serpapiKey]);

  const busy = searching || applying || undoing;

  return (
    <div className="image-search-panel">
      <div className="image-search-header">
        <ImagePlus size={16} />
        <span className="image-search-title">{t("imageSearch.title")}（{t("imageSearch.titleHint")}）</span>
        <span className="visual-qa-experimental">{t("common.experimental")}</span>
      </div>

      {/* ── Toolbar row ── */}
      <div className="image-search-toolbar">
        {/* Key config */}
        <button
          className={`image-search-tool-btn ${!hasSearchKey ? "image-search-tool-btn-warn" : ""}`}
          onClick={() => setShowKeys(!showKeys)}
          type="button"
          title="API Keys"
        >
          <Key size={14} />
          <span>{t("imageSearch.keyBtn")}</span>
        </button>

        {/* Apply button */}
        <button
          className="image-search-tool-btn image-search-tool-btn-primary"
          onClick={handleApply}
          disabled={!selectedItem || busy}
          type="button"
          title={t("imageSearch.applyTooltip")}
        >
          {applying ? <Loader2 size={13} className="spin" /> : <ImagePlus size={13} />}
          <span>{t("imageSearch.applyBtn")}</span>
        </button>

        <span
          className="image-search-tool-help"
          data-tooltip={t("imageSearch.applyTooltip")}
          aria-label={t("imageSearch.applyTooltip")}
          tabIndex={0}
          onClick={(e) => e.preventDefault()}
        >
          <HelpCircle size={13} />
        </span>

        {/* Slide label */}
        <span className="image-search-slide-label">
          {t("imageSearch.slideLabel")} {slideIndex}
        </span>

        {/* Undo button */}
        {hasApplied && (
          <button
            className="image-search-tool-btn"
            onClick={handleUndo}
            disabled={busy}
            type="button"
          >
            {undoing ? <Loader2 size={13} className="spin" /> : <RotateCcw size={13} />}
            <span>{t("imageSearch.undo")}</span>
          </button>
        )}

        {/* Search bar */}
        <div className="image-search-input-row">
          <Search size={13} className="image-search-input-icon" />
          <input
            type="text"
            className="image-search-input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={t("imageSearch.searchPlaceholder")}
            disabled={busy}
          />
          {query && (
            <button
              className="image-search-clear"
              onClick={() => {
                setQuery("");
                setResults([]);
                setSelectedItem(null);
                setHasApplied(false);
                setError(null);
                setStatusMsg(null);
              }}
              type="button"
            >
              <X size={11} />
            </button>
          )}
          <button
            className="image-search-btn"
            onClick={handleSearch}
            disabled={!query.trim() || busy}
            type="button"
          >
            {searching ? <Loader2 size={13} className="spin" /> : <Search size={13} />}
          </button>
        </div>
      </div>

      {/* ── Keys panel ── */}
      {showKeys && (
        <div className="image-search-keys">
          <div className="image-search-key-row">
            <label>Tavily Key</label>
            <input
              type="password"
              className="image-search-key-input"
              value={tavilyKey}
              onChange={(e) => setTavilyKey(e.target.value)}
              placeholder="tvly-..."
            />
          </div>
          <div className="image-search-key-row">
            <label>SerpAPI Key</label>
            <input
              type="password"
              className="image-search-key-input"
              value={serpapiKey}
              onChange={(e) => setSerpapiKey(e.target.value)}
              placeholder="serp-..."
            />
          </div>
          <button className="image-search-key-save" onClick={handleSaveKeys} type="button">
            Save
          </button>
          <p className="image-search-keys-hint">Keys saved in your browser only.</p>
        </div>
      )}

      {error && <p className="image-search-error">{error}</p>}
      {statusMsg && <p className="image-search-status">{statusMsg}</p>}

      {/* ── Results grid ── */}
      {results.length > 0 && (
        <div className="image-search-grid">
          {results.map((item, i) => (
            <div
              key={`${item.url}-${i}`}
              className={`image-search-thumb ${selectedItem?.url === item.url ? "image-search-thumb-selected" : ""}`}
            >
              <button
                className="image-search-thumb-img"
                onClick={() => setSelectedItem(item)}
                disabled={busy}
                type="button"
                title={item.description || item.url}
              >
                <img
                  src={item.thumbnail || item.url}
                  alt={item.description || `Result ${i + 1}`}
                  loading="lazy"
                  onError={(e) => {
                    (e.target as HTMLImageElement).style.display = "none";
                  }}
                />
              </button>
              <button
                className="image-search-thumb-download"
                onClick={(e) => {
                  e.stopPropagation();
                  handleDownload(item);
                }}
                type="button"
                title={t("imageSearch.download")}
              >
                <Download size={11} />
              </button>
              <span className="image-search-thumb-source">{item.source}</span>
            </div>
          ))}
        </div>
      )}

      {applying && (
        <p className="image-search-applying">
          <Loader2 size={14} className="spin" />
          {t("imageSearch.apply")}...
        </p>
      )}
    </div>
  );
}
