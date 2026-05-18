/**
 * Detect whether to ask the LLM for a Chinese or English reply.
 *
 * Mirrors the backend implementation in
 * ``backend/generator/template_import/llm_client.detect_user_language``
 * EXACTLY so the frontend hint and the actual LLM system prompt always
 * agree:
 *
 *   - Count CJK code points in U+3400-U+9FFF and U+F900-U+FAFF
 *   - Skip whitespace
 *   - When the ratio CJK / total non-whitespace is ≥ 0.3 → "zh"
 *   - Empty / null input → "en"
 */
export function detectUserLanguage(text: string | null | undefined): "zh" | "en" {
  if (!text) return "en";
  let cjk = 0;
  let total = 0;
  for (const ch of text) {
    if (/\s/.test(ch)) continue;
    total += 1;
    const cp = ch.codePointAt(0) ?? 0;
    if ((cp >= 0x3400 && cp <= 0x9fff) || (cp >= 0xf900 && cp <= 0xfaff)) {
      cjk += 1;
    }
  }
  if (total === 0) return "en";
  return cjk / total >= 0.3 ? "zh" : "en";
}
