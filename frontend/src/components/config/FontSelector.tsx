import { ChevronDown, Plus, Settings2, X } from "lucide-react";
import { useCallback, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useLocale } from "../../i18n";

interface FontSelectorProps {
  /** Simple mode value — single CSS font-family stack */
  value: string;
  onChange: (value: string) => void;
  /** Advanced mode — 4-dimension font control */
  headingFont?: string;
  onHeadingFontChange?: (value: string) => void;
  bodyFont?: string;
  onBodyFontChange?: (value: string) => void;
  cjkHeadingFont?: string;
  onCjkHeadingFontChange?: (value: string) => void;
  cjkBodyFont?: string;
  onCjkBodyFontChange?: (value: string) => void;
}

interface FontCategory {
  label: string;
  labelKey: string;
  fonts: string[];
}

const FONT_CATEGORIES: FontCategory[] = [
  {
    label: "中文无衬线",
    labelKey: "font.catZhSans",
    fonts: ["Microsoft YaHei", "Noto Sans SC", "PingFang SC", "Source Han Sans SC", "DengXian", "STHeiti"],
  },
  {
    label: "中文衬线",
    labelKey: "font.catZhSerif",
    fonts: ["SimSun", "KaiTi", "Noto Serif SC", "Source Han Serif SC", "STSong", "STKaiti", "FangSong"],
  },
  {
    label: "英文无衬线",
    labelKey: "font.catEnSans",
    fonts: ["Arial", "Calibri", "Segoe UI", "Helvetica", "Helvetica Neue", "Inter", "Roboto", "SF Pro"],
  },
  {
    label: "英文衬线",
    labelKey: "font.catEnSerif",
    fonts: ["Times New Roman", "Georgia", "Cambria", "Garamond", "Palatino"],
  },
  {
    label: "代码",
    labelKey: "font.catMono",
    fonts: ["Consolas", "SF Mono", "Monaco", "Menlo", "Courier New", "Cascadia Code"],
  },
  {
    label: "CSS Generic",
    labelKey: "font.catGeneric",
    fonts: ["sans-serif", "serif", "monospace"],
  },
];

function splitFontStack(stack: string): string[] {
  if (!stack.trim()) return [];
  return stack
    .split(",")
    .map((f) => f.trim().replace(/^['"]|['"]$/g, ""))
    .filter(Boolean);
}

function joinFontStack(fonts: string[]): string {
  return fonts
    .map((f) => (f.includes(" ") ? `"${f}"` : f))
    .join(", ");
}

/** Single font stack picker (used for simple mode and each slot in advanced mode) */
function FontStackPicker({
  value,
  onChange,
  placeholder,
  trailingAction,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  trailingAction?: ReactNode;
}) {
  const { t } = useLocale();
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [expandedCats, setExpandedCats] = useState<Set<number>>(new Set());
  const [manualInput, setManualInput] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const selectedFonts = splitFontStack(value);

  const addFont = useCallback(
    (font: string) => {
      const trimmed = font.trim().replace(/^['"]|['"]$/g, "");
      if (!trimmed) return;
      const lower = trimmed.toLowerCase();
      if (selectedFonts.some((f) => f.toLowerCase() === lower)) return;
      onChange(joinFontStack([...selectedFonts, trimmed]));
    },
    [selectedFonts, onChange],
  );

  const removeFont = useCallback(
    (index: number) => {
      onChange(joinFontStack(selectedFonts.filter((_, i) => i !== index)));
    },
    [selectedFonts, onChange],
  );

  const toggleCat = (idx: number) => {
    setExpandedCats((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  };

  const handleManualAdd = () => {
    addFont(manualInput);
    setManualInput("");
    inputRef.current?.focus();
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem" }}>
      {/* Selected font tags */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "0.3rem",
          minHeight: 28,
          padding: selectedFonts.length ? "0.3rem" : 0,
          borderRadius: 10,
          background: selectedFonts.length ? "rgba(255,255,255,0.03)" : "transparent",
          border: selectedFonts.length ? "1px solid var(--line)" : "1px solid transparent",
        }}
      >
        {selectedFonts.length === 0 && (
          <span style={{ fontSize: "0.78rem", color: "var(--muted)", padding: "0.3rem 0", opacity: 0.6 }}>
            {placeholder || t("font.noSelection")}
          </span>
        )}
        {selectedFonts.map((font, i) => (
          <span
            key={`${font}-${i}`}
            style={{
              display: "inline-flex", alignItems: "center", gap: "0.25rem",
              padding: "0.2rem 0.45rem", borderRadius: 999,
              border: "1px solid rgba(255,139,71,0.24)", background: "rgba(255,139,71,0.08)",
              fontSize: "0.75rem", fontWeight: 600, color: "var(--text)", whiteSpace: "nowrap",
            }}
          >
            {font}
            <button
              type="button" onClick={() => removeFont(i)}
              style={{
                display: "inline-flex", alignItems: "center", justifyContent: "center",
                width: 14, height: 14, borderRadius: 999, border: "none",
                background: "rgba(255,255,255,0.08)", color: "var(--muted)", cursor: "pointer", padding: 0,
              }}
              aria-label={`Remove ${font}`}
            >
              <X size={9} />
            </button>
          </span>
        ))}
      </div>

      {/* Add font button + dropdown */}
      <div className="font-picker-action-row">
      <div style={{ position: "relative", minWidth: 0, flex: "1 1 auto" }}>
        <button
          type="button"
          onClick={() => setDropdownOpen(!dropdownOpen)}
          style={{
            display: "inline-flex", alignItems: "center", gap: "0.35rem",
            padding: "0.4rem 0.65rem", borderRadius: 10,
            border: "1px solid var(--line)", background: "rgba(255,255,255,0.03)",
            color: "var(--muted)", fontSize: "0.78rem", cursor: "pointer",
            width: "100%", justifyContent: "space-between",
          }}
        >
          <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}>
            <Plus size={13} />
            {t("font.addFont")}
          </span>
          <ChevronDown size={13} style={{ transform: dropdownOpen ? "rotate(180deg)" : "none", transition: "transform 150ms ease" }} />
        </button>

        {dropdownOpen && (
          <div
            style={{
              position: "absolute", top: "calc(100% + 4px)", left: 0, right: 0, zIndex: 10,
              maxHeight: 260, overflowY: "auto", borderRadius: 12,
              border: "1px solid var(--line)", background: "var(--surface-strong)",
              boxShadow: "0 16px 40px rgba(0,0,0,0.28)", padding: "0.35rem",
            }}
          >
            {FONT_CATEGORIES.map((cat, catIdx) => {
              const expanded = expandedCats.has(catIdx);
              return (
                <div key={catIdx}>
                  <button
                    type="button" onClick={() => toggleCat(catIdx)}
                    style={{
                      display: "flex", alignItems: "center", gap: "0.35rem",
                      width: "100%", padding: "0.4rem 0.5rem", border: "none", borderRadius: 6,
                      background: "transparent", color: "var(--text)", fontSize: "0.78rem",
                      fontWeight: 600, cursor: "pointer", textAlign: "left",
                    }}
                  >
                    <ChevronDown size={11} style={{ transform: expanded ? "rotate(0deg)" : "rotate(-90deg)", transition: "transform 120ms ease", flexShrink: 0 }} />
                    {t(cat.labelKey) || cat.label}
                    <span style={{ marginLeft: "auto", fontSize: "0.65rem", color: "var(--muted)", fontWeight: 400 }}>{cat.fonts.length}</span>
                  </button>
                  {expanded && (
                    <div style={{ display: "flex", flexWrap: "wrap", gap: "0.25rem", padding: "0.25rem 0.35rem 0.4rem 1.4rem" }}>
                      {cat.fonts.map((font) => {
                        const sel = selectedFonts.some((f) => f.toLowerCase() === font.toLowerCase());
                        return (
                          <button
                            key={font} type="button" disabled={sel}
                            onClick={() => addFont(font)}
                            style={{
                              padding: "0.25rem 0.5rem", borderRadius: 6,
                              border: sel ? "1px solid rgba(255,139,71,0.12)" : "1px solid var(--line)",
                              background: sel ? "rgba(255,139,71,0.06)" : "rgba(255,255,255,0.02)",
                              color: sel ? "var(--muted)" : "var(--text)",
                              fontSize: "0.75rem", cursor: sel ? "default" : "pointer",
                              opacity: sel ? 0.5 : 1, whiteSpace: "nowrap",
                            }}
                          >
                            {font}
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}

            {/* Manual input */}
            <div style={{ display: "flex", gap: "0.3rem", padding: "0.35rem 0.35rem 0.15rem", borderTop: "1px solid var(--line)", marginTop: "0.25rem" }}>
              <input
                ref={inputRef} type="text" value={manualInput}
                onChange={(e) => setManualInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); handleManualAdd(); } }}
                placeholder={t("font.manualPlaceholder")}
                style={{
                  flex: 1, padding: "0.35rem 0.5rem", borderRadius: 6,
                  border: "1px solid var(--line)", background: "rgba(255,255,255,0.03)",
                  color: "var(--text)", fontSize: "0.75rem", outline: "none",
                }}
              />
              <button
                type="button" onClick={handleManualAdd} disabled={!manualInput.trim()}
                style={{
                  padding: "0.35rem 0.55rem", borderRadius: 6,
                  border: "1px solid rgba(255,139,71,0.24)", background: "rgba(255,139,71,0.1)",
                  color: "var(--accent)", fontSize: "0.75rem", fontWeight: 600,
                  cursor: manualInput.trim() ? "pointer" : "not-allowed",
                  opacity: manualInput.trim() ? 1 : 0.4, whiteSpace: "nowrap",
                }}
              >
                {t("font.add")}
              </button>
            </div>
          </div>
        )}
      </div>
      {trailingAction ? <div className="font-picker-trailing-action">{trailingAction}</div> : null}
      </div>

      {/* Preview */}
      {selectedFonts.length > 0 && (
        <span style={{ fontSize: "0.68rem", color: "var(--muted)", fontFamily: "var(--mono)", wordBreak: "break-all", opacity: 0.7 }}>
          {joinFontStack(selectedFonts)}
        </span>
      )}
    </div>
  );
}

/** Main FontSelector — simple mode + optional advanced toggle */
export function FontSelector({
  value, onChange,
  headingFont, onHeadingFontChange,
  bodyFont, onBodyFontChange,
  cjkHeadingFont, onCjkHeadingFontChange,
  cjkBodyFont, onCjkBodyFontChange,
}: FontSelectorProps) {
  const { t } = useLocale();
  const hasAdvanced = onHeadingFontChange && onBodyFontChange;
  // Derive advanced mode: if any advanced font is set, auto-enable advanced view
  const hasAdvancedFonts = Boolean(headingFont || bodyFont || cjkHeadingFont || cjkBodyFont);
  const [advancedToggled, setAdvancedToggled] = useState(false);
  const advanced = advancedToggled || hasAdvancedFonts;

  return (
    <div className="font-selector-shell">
      <div className="font-selector-topline">
        {!advanced && (
          <FontStackPicker
            value={value}
            onChange={onChange}
            trailingAction={
              hasAdvanced ? (
                <button
                  type="button"
                  className="font-advanced-toggle"
                  onClick={() => setAdvancedToggled(true)}
                >
                  <Settings2 size={12} />
                  {t("font.advancedOff")}
                </button>
              ) : null
            }
          />
        )}
      {hasAdvanced && advanced && (
          <button
            type="button"
            className="font-advanced-toggle font-advanced-toggle-active"
            onClick={() => setAdvancedToggled(false)}
          >
            <Settings2 size={12} />
            {t("font.advancedOn")}
          </button>
      )}
      </div>

      {hasAdvanced && advanced && (
            <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem" }}>
                <div>
                  <label style={{ fontSize: "0.72rem", color: "var(--muted)", fontWeight: 500, marginBottom: "0.2rem", display: "block" }}>
                    {t("font.headingWestern")}
                  </label>
                  <FontStackPicker
                    value={headingFont || ""}
                    onChange={onHeadingFontChange!}
                    placeholder={t("font.headingPlaceholder")}
                  />
                </div>
                <div>
                  <label style={{ fontSize: "0.72rem", color: "var(--muted)", fontWeight: 500, marginBottom: "0.2rem", display: "block" }}>
                    {t("font.bodyWestern")}
                  </label>
                  <FontStackPicker
                    value={bodyFont || ""}
                    onChange={onBodyFontChange!}
                    placeholder={t("font.bodyPlaceholder")}
                  />
                </div>
                {onCjkHeadingFontChange && onCjkBodyFontChange && (
                  <>
                    <div>
                      <label style={{ fontSize: "0.72rem", color: "var(--muted)", fontWeight: 500, marginBottom: "0.2rem", display: "block" }}>
                        {t("font.headingCJK")}
                      </label>
                      <FontStackPicker
                        value={cjkHeadingFont || ""}
                        onChange={onCjkHeadingFontChange}
                        placeholder={t("font.headingPlaceholder")}
                      />
                    </div>
                    <div>
                      <label style={{ fontSize: "0.72rem", color: "var(--muted)", fontWeight: 500, marginBottom: "0.2rem", display: "block" }}>
                        {t("font.bodyCJK")}
                      </label>
                      <FontStackPicker
                        value={cjkBodyFont || ""}
                        onChange={onCjkBodyFontChange}
                        placeholder={t("font.bodyPlaceholder")}
                      />
                    </div>
                  </>
                )}
              </div>
              <p style={{ fontSize: "0.68rem", color: "var(--muted)", margin: 0, opacity: 0.7 }}>
                {t("font.advancedHint")}
              </p>
            </div>
      )}
    </div>
  );
}
