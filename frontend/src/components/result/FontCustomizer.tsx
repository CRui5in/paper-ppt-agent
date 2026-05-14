import { useState } from "react";
import { Loader2 } from "lucide-react";
import { useLocale } from "../../i18n";
import { updateSvgFonts, reexportPresentation } from "../../lib/api";
import type { UpdateFontsRequest } from "../../lib/types";
import {
  CJK_BODY_FONT_OPTIONS,
  CJK_HEADING_FONT_OPTIONS,
  WESTERN_BODY_FONT_OPTIONS,
  WESTERN_HEADING_FONT_OPTIONS,
  fontPreviewFamily,
} from "../../lib/fontOptions";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select";

interface FontOption { label: string; value: string }

interface Props {
  jobId: string;
  onReexported: (outputPath: string) => void;
}

function withDefaultOption(options: FontOption[], label: string): FontOption[] {
  return [{ label, value: "" }, ...options];
}

export function FontCustomizer({ jobId, onReexported }: Props) {
  const { t } = useLocale();
  const [wh, setWh] = useState("");
  const [wb, setWb] = useState("");
  const [ch, setCh] = useState("");
  const [cb, setCb] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ replaced: number } | null>(null);

  const anySet = wh || wb || ch || cb;

  const handleApply = async () => {
    if (!anySet) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const config: UpdateFontsRequest = {};
      if (wh) config.western_heading = wh;
      if (wb) config.western_body = wb;
      if (ch) config.cjk_heading = ch;
      if (cb) config.cjk_body = cb;

      const resp = await updateSvgFonts(jobId, config);
      setResult({ replaced: resp.svg_fonts_replaced });

      // Auto re-export
      const reexportResp = await reexportPresentation(jobId);
      onReexported(reexportResp.output_path);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Font update failed.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <section className="font-customizer-panel">
      <div className="font-customizer-heading">
        <p>{t("result.fontsBody")}</p>
      </div>

      {[
        { key: "wh", label: t("result.fontsWesternHeading"), value: wh, setter: setWh, options: withDefaultOption(WESTERN_HEADING_FONT_OPTIONS, t("font.noSelection")) },
        { key: "wb", label: t("result.fontsWesternBody"), value: wb, setter: setWb, options: withDefaultOption(WESTERN_BODY_FONT_OPTIONS, t("font.noSelection")) },
        { key: "ch", label: t("result.fontsCJKHeading"), value: ch, setter: setCh, options: withDefaultOption(CJK_HEADING_FONT_OPTIONS, t("font.noSelection")) },
        { key: "cb", label: t("result.fontsCJKBody"), value: cb, setter: setCb, options: withDefaultOption(CJK_BODY_FONT_OPTIONS, t("font.noSelection")) },
      ].map((item) => (
        <div key={item.key} className="font-customizer-field">
          <label>{item.label}</label>
          <Select value={item.value || "__default__"} onValueChange={(value) => item.setter(value === "__default__" ? "" : value)} disabled={loading}>
            <SelectTrigger style={{ fontFamily: fontPreviewFamily(item.value) }}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
            {item.options.map((opt) => (
              <SelectItem key={opt.value || "__default__"} value={opt.value || "__default__"} style={{ fontFamily: fontPreviewFamily(opt.value) }}>
                {opt.label}
              </SelectItem>
            ))}
            </SelectContent>
          </Select>
        </div>
      ))}

      <div className="font-customizer-actions">
        {error && <p className="error-text">{error}</p>}
        {result && (
          <p className="font-customizer-success">
            {t("result.fontsApplied").replace("{n}", String(result.replaced))}
          </p>
        )}
        <button
          type="button"
          className="primary-button"
          onClick={handleApply}
          disabled={loading || !anySet}
        >
          {loading ? <Loader2 size={15} className="spin" /> : null}
          {loading ? t("result.fontsLoading") : t("result.fontsApply")}
        </button>
      </div>
    </section>
  );
}
