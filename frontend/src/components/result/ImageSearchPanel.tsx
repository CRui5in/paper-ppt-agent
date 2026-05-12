import { Download, ImagePlus, KeyRound, Loader2, RotateCcw, Search, X } from "lucide-react";
import { useCallback, useRef, useState } from "react";
import { useLocale } from "../../i18n";
import { applySearchImage, searchImages, undoSearchImage } from "../../lib/api";
import type { ImageSearchResultItem } from "../../lib/types";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Input } from "../ui/input";

const TAVILY_KEY_STORAGE = "paper-ppt-agent-image-search-tavily-key";
const SERPAPI_KEY_STORAGE = "paper-ppt-agent-image-search-serpapi-key";
const ROUTING_PROFILE_STORAGE_KEY = "paper-ppt-agent-routing-profiles-v1";

interface RoutingProfile {
  model: string;
  baseUrl?: string;
  apiKey: string;
}

interface ImageSearchPanelProps {
  jobId: string;
  slideIndex: number;
  slideTitle?: string;
  onClose: () => void;
  onImageApplied: () => Promise<void> | void;
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
    // Browser storage may be unavailable in private or locked-down contexts.
  }
}

function readLlmProfile(): { provider: string; model: string; apiKey: string; baseUrl?: string } | null {
  try {
    const raw = window.localStorage.getItem(ROUTING_PROFILE_STORAGE_KEY);
    if (!raw) return null;
    const profiles = JSON.parse(raw) as Record<string, RoutingProfile>;
    for (const [provider, profile] of Object.entries(profiles)) {
      if (profile?.apiKey) {
        return { provider, model: profile.model, apiKey: profile.apiKey, baseUrl: profile.baseUrl };
      }
    }
  } catch {
    // Ignore malformed local storage.
  }
  return null;
}

function errorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  try {
    const parsed = JSON.parse(message) as { detail?: string };
    return parsed.detail || message;
  } catch {
    return message;
  }
}

export function ImageSearchPanel({
  jobId,
  slideIndex,
  slideTitle,
  onClose,
  onImageApplied,
}: ImageSearchPanelProps) {
  const { t } = useLocale();
  const [query, setQuery] = useState("");
  const [tavilyKey, setTavilyKey] = useState(() => readSavedKey(TAVILY_KEY_STORAGE));
  const [serpapiKey, setSerpapiKey] = useState(() => readSavedKey(SERPAPI_KEY_STORAGE));
  const [keysOpen, setKeysOpen] = useState(false);
  const [results, setResults] = useState<ImageSearchResultItem[]>([]);
  const [selectedItem, setSelectedItem] = useState<ImageSearchResultItem | null>(null);
  const [searching, setSearching] = useState(false);
  const [applying, setApplying] = useState(false);
  const [undoing, setUndoing] = useState(false);
  const [hasApplied, setHasApplied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const busy = searching || applying || undoing;
  const hasLocalSearchKey = Boolean(tavilyKey.trim() || serpapiKey.trim());
  const effectiveQuery = query.trim() || slideTitle?.trim() || "";

  const handleSearch = useCallback(async () => {
    if (!effectiveQuery || busy) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setSearching(true);
    setError(null);
    setStatusMsg(null);
    setSelectedItem(null);
    try {
      const data = await searchImages(
        jobId,
        {
          query: effectiveQuery,
          slide_index: slideIndex,
          max_results: 10,
          tavily_api_key: tavilyKey.trim() || undefined,
          serpapi_key: serpapiKey.trim() || undefined,
        },
        { signal: controller.signal },
      );
      setResults(data.results ?? []);
      if (!data.results?.length) {
        setStatusMsg(t("imageSearch.noResults"));
      }
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") return;
      setError(errorMessage(err));
      setKeysOpen(true);
    } finally {
      setSearching(false);
    }
  }, [busy, effectiveQuery, jobId, serpapiKey, slideIndex, tavilyKey, t]);

  const handleApply = useCallback(async () => {
    if (!selectedItem || busy) return;
    setApplying(true);
    setError(null);
    setStatusMsg(null);
    const llm = readLlmProfile();
    try {
      const response = await applySearchImage(jobId, {
        image_url: selectedItem.url,
        slide_index: slideIndex,
        image_description: selectedItem.description || effectiveQuery,
        api_key: llm?.apiKey,
        provider: llm?.provider || "openai",
        model: llm?.model || "gpt-4o",
        base_url: llm?.baseUrl,
      });
      setHasApplied(true);
      setStatusMsg(response.action === "replaced" ? t("imageSearch.replaced") : t("imageSearch.inserted"));
      await onImageApplied();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setApplying(false);
    }
  }, [busy, effectiveQuery, jobId, onImageApplied, selectedItem, slideIndex, t]);

  const handleUndo = useCallback(async () => {
    if (busy) return;
    setUndoing(true);
    setError(null);
    setStatusMsg(null);
    try {
      await undoSearchImage(jobId);
      setHasApplied(false);
      setSelectedItem(null);
      setStatusMsg(t("imageSearch.undone"));
      await onImageApplied();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setUndoing(false);
    }
  }, [busy, jobId, onImageApplied, t]);

  const handleDownload = useCallback((item: ImageSearchResultItem) => {
    window.open(item.url, "_blank", "noopener,noreferrer");
  }, []);

  const saveKeys = useCallback(() => {
    saveKey(TAVILY_KEY_STORAGE, tavilyKey.trim());
    saveKey(SERPAPI_KEY_STORAGE, serpapiKey.trim());
    setKeysOpen(false);
    setStatusMsg(t("imageSearch.keysSaved"));
  }, [serpapiKey, tavilyKey, t]);

  return (
    <section className="image-search-drawer" aria-label={t("imageSearch.title")}>
      <div className="image-search-drawer-top">
        <div className="image-search-title-group">
          <ImagePlus size={16} />
          <strong>{t("imageSearch.title")}</strong>
          <Badge variant="muted">{t("common.experimental")}</Badge>
          <span>{t("imageSearch.slideLabel").replace("{index}", String(slideIndex))}</span>
        </div>
        <Button type="button" variant="ghost" size="icon" onClick={onClose} aria-label={t("common.close")}>
          <X size={15} />
        </Button>
      </div>

      <div className="image-search-controls">
        <div className="image-search-query">
          <Search size={14} />
          <Input
            value={query}
            disabled={busy}
            placeholder={slideTitle ? t("imageSearch.searchWithTitle").replace("{title}", slideTitle) : t("imageSearch.searchPlaceholder")}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                void handleSearch();
              }
            }}
          />
        </div>
        <Button type="button" size="sm" disabled={!effectiveQuery || busy} onClick={() => void handleSearch()}>
          {searching ? <Loader2 size={14} className="spin" /> : <Search size={14} />}
          {t("imageSearch.search")}
        </Button>
        <Button
          type="button"
          size="sm"
          variant={hasLocalSearchKey ? "outline" : "secondary"}
          onClick={() => setKeysOpen((open) => !open)}
        >
          <KeyRound size={14} />
          {t("imageSearch.keys")}
        </Button>
        <Button type="button" size="sm" disabled={!selectedItem || busy} onClick={() => void handleApply()}>
          {applying ? <Loader2 size={14} className="spin" /> : <ImagePlus size={14} />}
          {t("imageSearch.apply")}
        </Button>
        <Button type="button" size="sm" variant="outline" disabled={!hasApplied || busy} onClick={() => void handleUndo()}>
          {undoing ? <Loader2 size={14} className="spin" /> : <RotateCcw size={14} />}
          {t("imageSearch.undo")}
        </Button>
      </div>

      {keysOpen ? (
        <div className="image-search-keys">
          <label>
            <span>{t("options.tavilyApiKey")}</span>
            <Input type="password" value={tavilyKey} placeholder="tvly-..." onChange={(event) => setTavilyKey(event.target.value)} />
          </label>
          <label>
            <span>{t("options.serpApiKey")}</span>
            <Input type="password" value={serpapiKey} placeholder="serpapi..." onChange={(event) => setSerpapiKey(event.target.value)} />
          </label>
          <Button type="button" size="sm" variant="secondary" onClick={saveKeys}>
            {t("imageSearch.saveKeys")}
          </Button>
        </div>
      ) : null}

      {error ? <p className="image-search-message image-search-error">{error}</p> : null}
      {statusMsg ? <p className="image-search-message">{statusMsg}</p> : null}

      <div className="image-search-results" aria-busy={searching}>
        {searching ? (
          <div className="image-search-empty">
            <Loader2 size={16} className="spin" />
            <span>{t("imageSearch.searching")}</span>
          </div>
        ) : results.length ? (
          results.map((item, index) => (
            <article
              key={`${item.url}-${index}`}
              className={`image-search-result ${selectedItem?.url === item.url ? "image-search-result-selected" : ""}`}
            >
              <button type="button" onClick={() => setSelectedItem(item)} disabled={busy}>
                <img src={item.thumbnail || item.url} alt={item.description || `Result ${index + 1}`} loading="lazy" />
              </button>
              <div className="image-search-result-meta">
                <span>{item.source || t("common.unknown")}</span>
                <button type="button" onClick={() => handleDownload(item)} aria-label={t("imageSearch.openImage")}>
                  <Download size={12} />
                </button>
              </div>
            </article>
          ))
        ) : (
          <div className="image-search-empty">
            <ImagePlus size={16} />
            <span>{t("imageSearch.empty")}</span>
          </div>
        )}
      </div>
    </section>
  );
}
