/**
 * Session-scoped confirmation suppression.
 *
 * Confirmation dialogs for potentially disruptive actions (entering Agent
 * mode, switching to Codex, triggering design_spec generation) can be
 * suppressed for the rest of the browser session once the user explicitly
 * ticks "don't show this again".
 *
 * Storage uses sessionStorage so the suppression:
 *   - persists across page navigations / reloads within the same tab, and
 *   - resets when the tab or browser is closed (a fresh session re-shows the
 *     dialog), which matches "本会话只弹一次" (once per session).
 */

const STORAGE_KEY = "paper-ppt-agent-confirm-suppress";

export type ConfirmSuppressionKey =
  | "agentModeEntry"
  | "codexRuntime"
  | "templateDesignSpec";

function readStore(): Record<string, boolean> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function writeStore(data: Record<string, boolean>): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  } catch {
    // Ignore storage errors (private mode, quota, etc.).
  }
}

/** True if the user has suppressed the given confirmation this session. */
export function isConfirmSuppressed(key: ConfirmSuppressionKey): boolean {
  return Boolean(readStore()[key]);
}

/** Mark the given confirmation as suppressed for the rest of the session. */
export function suppressConfirm(key: ConfirmSuppressionKey): void {
  if (isConfirmSuppressed(key)) return;
  const data = readStore();
  data[key] = true;
  writeStore(data);
}
