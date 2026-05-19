import type {
  ImportStatus,
  TemplateAgentEvent,
  TemplateReview,
  TemplateReviewDraft,
} from "../../lib/types";

/**
 * One row of the AgentActivityStream — Claude-Code-style live feed.
 *
 * The data is *derived* from current state every render rather than
 * stored, so the stream is a pure projection of (status, review, draft).
 * Sort order is timestamp DESC, capped to the most recent 30 entries.
 */
export interface AgentActivity {
  id: string;
  /** Source kind drives the icon/colour bar on the row. */
  kind:
    | "pipeline"
    | "llm"
    | "user"
    | "assistant"
    | "info";
  /** Visual state for left edge color bar + spinner. */
  state: "active" | "done" | "error" | "warning" | "info";
  label: string;
  /** Optional secondary line (gray) shown under the label, truncated. */
  detail?: string;
  /** ms epoch — only used for sort + tooltip. */
  timestamp: number;
  /**
   * True for low-signal rows (tool calls, system frames). The UI groups
   * consecutive collapsible rows into a single expandable section so the
   * primary Agent message stream isn't drowned out.
   */
  collapsible?: boolean;
  /**
   * True when the row carries the Agent's assistant message and should be
   * rendered in full (no width clamp, no detail truncation).
   */
  primary?: boolean;
}

const MAX_ENTRIES = 80;

interface BuildActivityOptions {
  mode?: "classic" | "agent" | "direct";
  agentEvents?: TemplateAgentEvent[];
  llmEvents?: TemplateAgentEvent[];
  t?: (key: string) => string;
}

function tsFromSec(seconds: number | undefined | null): number {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) {
    return Date.now();
  }
  // Backend emits seconds since epoch; normalise to ms.
  return seconds < 1e12 ? seconds * 1000 : seconds;
}

function pipelineState(status: string | undefined): AgentActivity["state"] {
  switch (status) {
    case "complete":
      return "done";
    case "active":
      return "active";
    case "error":
      return "error";
    case "skipped":
      return "warning";
    default:
      return "info";
  }
}

function trimText(value: string, max = 80): string {
  const cleaned = value.replace(/\s+/g, " ").trim();
  return cleaned.length > max ? `${cleaned.slice(0, max - 1)}…` : cleaned;
}

function tr(t: BuildActivityOptions["t"], key: string, fallback: string, params?: Record<string, string | number>): string {
  const template = t?.(key);
  const value = template && template !== key ? template : fallback;
  if (!params) return value;
  return Object.entries(params).reduce(
    (text, [name, replacement]) => text.split(`{${name}}`).join(String(replacement)),
    value,
  );
}

function pageTypeLabel(pageType: string, t?: BuildActivityOptions["t"]): string {
  return tr(t, `template.page.${pageType}`, pageType);
}

function agentEventState(event: TemplateAgentEvent): AgentActivity["state"] {
  if (event.type === "error" || event.status === "error") return "error";
  if (event.type === "complete" || event.status === "complete") return "done";
  if (event.type === "status" || event.type === "tool" || event.type === "stderr") {
    return "active";
  }
  return "info";
}

function agentEventLabel(event: TemplateAgentEvent, t?: BuildActivityOptions["t"]): string {
  if (event.type === "tool") {
    const tool = (event.data && typeof event.data === "object" && (event.data as Record<string, unknown>).tool) || null;
    return typeof tool === "string" && tool
      ? tr(t, "template.activity.toolCallNamed", "Calling {tool}", { tool })
      : tr(t, "template.activity.toolCall", "Tool call");
  }
  if (event.type === "message") return "Agent";
  if (event.type === "stderr") return tr(t, "template.activity.agentLog", "Agent log");
  if (event.type === "system") return tr(t, "template.activity.agentSystem", "Agent system");
  if (event.type === "result") return tr(t, "template.activity.agentResult", "Agent result");
  return "Agent";
}

function isShellTool(_tool: string): boolean {
  return false;
}
void isShellTool;

/** Pretty-print a tool call's input so the row is readable instead of dumping raw JSON. */
function describeToolInput(input: unknown): string {
  if (input == null) return "";
  if (typeof input === "string") return trimText(input, 160);
  if (typeof input !== "object") return String(input);

  const obj = input as Record<string, unknown>;

  // Path-like fields: show only the basename so the row stays compact.
  for (const key of ["file_path", "path"]) {
    const value = obj[key];
    if (typeof value === "string" && value.trim()) {
      const parts = value.replace(/\\/g, "/").split("/").filter(Boolean);
      const tail = parts.slice(-2).join("/") || value;
      return trimText(tail, 80);
    }
  }
  // Pattern/query/command-like fields: show as-is, trimmed.
  const command = obj.command;
  if (typeof command === "string" && command.trim()) {
    return trimText(command, 64);
  }
  for (const key of ["pattern", "query", "command", "url"]) {
    const value = obj[key];
    if (typeof value === "string" && value.trim()) {
      return trimText(value, 120);
    }
  }
  // List-like fields: show count.
  for (const key of ["todos", "edits"]) {
    const value = obj[key];
    if (Array.isArray(value) && value.length > 0) {
      return `${key} ×${value.length}`;
    }
  }
  // Fallback: short JSON summary.
  try {
    return trimText(JSON.stringify(obj), 120);
  } catch {
    return "";
  }
}

/** Hide low-signal SDK events that mostly clutter the feed. */
function isNoiseAgentEvent(event: TemplateAgentEvent): boolean {
  if (event.type === "usage") return true;
  if (["snapshot", "status", "complete", "cancelled"].includes(event.type)) return true;
  // ``result`` is the SDK's terminal envelope: it carries the full assistant
  // text again (already rendered as a primary message bubble) plus duration /
  // turn metadata. If we let it flow into the activity stream the group
  // summary picks it up and shows ``已执行 Agent 结果<huge markdown>`` instead
  // of the actual most-recent tool call.
  if (event.type === "result") return true;
  if (event.type === "system") {
    const message = (event.message ?? "").trim().toLowerCase();
    // Bare lifecycle echoes ("status", "running", "init", etc.) carry no info.
    if (!message || ["status", "running", "init", "system"].includes(message)) {
      return true;
    }
  }
  if (event.type === "stderr") {
    const message = (event.message ?? "").trim();
    if (!message) return true;
  }
  return false;
}

/**
 * Build the activity feed from current state. The function is pure and
 * stable for memoisation: identical inputs produce identical outputs.
 */
export function buildAgentActivityEvents(
  status: ImportStatus | null,
  review: TemplateReview | null,
  draft: TemplateReviewDraft,
  options: BuildActivityOptions = {},
): AgentActivity[] {
  const events: AgentActivity[] = [];
  const now = Date.now();
  const mode = options.mode ?? "classic";
  const agentEvents = options.agentEvents ?? [];
  const llmEvents = options.llmEvents ?? [];
  const t = options.t;

  // 1. Pipeline step events.
  const steps = mode === "classic" ? (status?.steps ?? []) as Array<{
    id: string;
    label?: string;
    status?: string;
    message?: string;
    started_at?: number;
    completed_at?: number;
    error?: string | null;
  }> : [];
  steps.forEach((step, idx) => {
    const state = pipelineState(step.status);
    const ts =
      tsFromSec(step.completed_at) ||
      tsFromSec(step.started_at) ||
      now - (steps.length - idx) * 800;
    events.push({
      id: `pipeline:${step.id}:${step.status}`,
      kind: "pipeline",
      state,
      label: tr(t, `template.step.${step.id}`, step.label || step.id),
      detail: step.error || step.message || undefined,
      timestamp: ts,
    });
  });

  // Top-level error from import status.
  if (mode === "classic" && status?.status === "error" && status.error) {
    events.push({
      id: `pipeline:error:${status.import_id}`,
      kind: "pipeline",
      state: "error",
      label: status.message || tr(t, "template.importError", "Import failed"),
      detail: status.error,
      timestamp: now,
    });
  }

  // 2. LLM trace.
  const trace = mode === "classic" ? review?.llm_trace : undefined;
  if (trace) {
    const iter = trace.iteration ?? 0;
    const ts = tsFromSec(trace.updated_at) || now;
    if (trace.user_feedback) {
      events.push({
        id: `llm:feedback:${iter}`,
        kind: "user",
        state: "done",
        label: tr(t, "template.activity.userFeedback", "User feedback"),
        detail: trimText(trace.user_feedback),
        timestamp: ts - 50,
      });
    }
    events.push({
      id: `llm:call:${iter}`,
      kind: "llm",
      state: trace.changed ? "done" : trace.retried_no_change ? "warning" : "info",
      label: tr(t, "template.activity.llmCallNumber", "LLM call #{iter}", { iter: iter || "—" }),
      detail: trace.changed
        ? tr(t, "template.activity.llmChanged", "Changes applied")
        : trace.retried_no_change
          ? tr(t, "template.activity.llmNoChange", "Retried with no visible change")
          : tr(t, "template.activity.llmResponse", "Response received"),
      timestamp: ts,
    });
    const patches = trace.rule_patches?.length ?? 0;
    if (patches > 0) {
      events.push({
        id: `llm:patches:${iter}`,
        kind: "llm",
        state: "done",
        label: tr(t, "template.activity.rulePatches", "Rule patches x{count}", { count: patches }),
        detail: trace.rule_patches?.slice(0, 3).join(" · "),
        timestamp: ts + 50,
      });
    }
  }

  // 4. Annotations the user has authored — surface count + most recent note.
  const annotations = mode === "classic" ? draft.annotations ?? review?.annotations ?? [] : [];
  if (annotations.length > 0) {
    const last = annotations[annotations.length - 1];
    events.push({
      id: `user:annotations:${annotations.length}`,
      kind: "user",
      state: "done",
      label: tr(t, "template.activity.annotationsCreated", "You created {count} annotations", { count: annotations.length }),
      detail: last ? trimText(last.note, 60) : undefined,
      timestamp: tsFromSec(last?.created_at) || now,
    });
  }

  // 5. Page-type assignments — show "completed" once each selected slot is set.
  const selections = mode === "classic" ? draft.page_selections ?? {} : {};
  (["cover", "toc", "chapter", "content", "ending"] as const).forEach((pt, i) => {
    const slide = selections[pt];
    if (typeof slide === "number" && slide > 0) {
      events.push({
        id: `user:assign:${pt}:${slide}`,
        kind: "user",
        state: "done",
        label: tr(t, "template.activity.pageAssigned", "{pageType} assigned", {
          pageType: pageTypeLabel(pt, t),
        }),
        detail: tr(t, "template.activity.slideNumber", "Slide {slide}", { slide }),
        timestamp: now - 200 + i,
      });
    }
  });

  // 6. Final saved confirmation.
  if (mode === "classic" && status?.status === "complete" && status.template_id) {
    events.push({
      id: `pipeline:complete:${status.template_id}`,
      kind: "pipeline",
      state: "done",
      label: tr(t, "template.activity.templateSaved", "Template saved"),
      detail: status.label || status.template_id,
      timestamp: now,
    });
  }

  llmEvents.slice(-18).forEach((event) => {
    if (mode !== "classic" || event.type === "ping") return;
    const message = event.message || event.error || event.type;
    const state: AgentActivity["state"] =
      event.status === "error"
        ? "error"
        : event.status === "complete"
          ? "done"
          : "active";
    events.push({
      id: `llm-stream:${event.seq ?? event.ts ?? message}`,
      kind: event.stage === "user" ? "user" : "llm",
      state,
      label:
        event.stage === "user"
          ? tr(t, "template.chatUser", "You")
          : event.stage === "preview"
            ? tr(t, "template.preview", "Preview")
            : event.stage === "draft"
              ? tr(t, "template.activity.draft", "Draft")
              : "LLM",
      detail: trimText(message, 90),
      timestamp: tsFromSec(event.ts) || now,
    });
  });

  if (mode === "agent") {
    const visibleAgentEvents = agentEvents.filter(
      (event) => event.type !== "ping" && !isNoiseAgentEvent(event),
    );
    // Pre-pass: collect tool_use_ids whose ToolResult has arrived so we can
    // mark the corresponding running rows as complete / errored. Without
    // this every Read / Edit stays "正在执行" until the whole agent run
    // finishes, which doesn't match Claude Code's per-tool feedback.
    const toolStatusById = new Map<string, "complete" | "error">();
    for (const event of visibleAgentEvents) {
      if (event.type !== "tool") continue;
      if (event.status !== "complete" && event.status !== "error") continue;
      const data = event.data as Record<string, unknown> | undefined;
      const id = typeof data?.tool_use_id === "string" ? data.tool_use_id : "";
      if (id) {
        toolStatusById.set(id, event.status as "complete" | "error");
      }
    }

    // Build rows for non-lifecycle events, then collapse consecutive duplicates
    // (same kind + same label + same detail) so the feed doesn't stack.
    const rawRows: AgentActivity[] = [];
    visibleAgentEvents
      .filter((event) => {
        // Skip ToolResult-style frames: they only carry status updates that
        // we already folded into ``toolStatusById`` above.
        if (event.type !== "tool") return true;
        return event.status !== "complete" && event.status !== "error";
      })
      .slice(-40)
      .forEach((event) => {
        let detail = "";
        const isPrimary = event.type === "message";
        let label = agentEventLabel(event, t);
        let state = agentEventState(event);
        if (event.type === "tool") {
          const data = event.data as Record<string, unknown> | undefined;
          const tool = typeof data?.tool === "string" ? data.tool : "";
          const input = describeToolInput(data?.input);
          const toolUseId = typeof data?.tool_use_id === "string" ? data.tool_use_id : "";
          const matchedStatus = toolUseId ? toolStatusById.get(toolUseId) : undefined;
          if (matchedStatus === "complete") {
            state = "done";
            label = tr(t, "template.activity.executed", "Executed");
          } else if (matchedStatus === "error") {
            state = "error";
            label = tr(t, "template.activity.executionFailed", "Execution failed");
          } else {
            label = tr(t, "template.activity.executing", "Running");
          }
          detail = [tool, input].filter(Boolean).join(" ");
        } else if (isPrimary) {
          // Don't trim the primary Agent message — the UI renders it as a
          // full markdown bubble.
          detail = (event.message ?? "").trim();
        } else {
          const message = event.message || event.error || event.type;
          detail = trimText(message, 200);
        }
        rawRows.push({
          id: `agent:${event.seq ?? event.ts ?? `${event.type}:${detail}`}`,
          kind: isPrimary ? "assistant" : event.type === "tool" ? "pipeline" : "llm",
          state,
          label,
          detail: detail || undefined,
          timestamp: tsFromSec(event.ts) || now,
          // Tool / system / stderr / result rows are secondary — let the UI fold them.
          collapsible: !isPrimary,
          primary: isPrimary,
        });
      });

    const deduped: AgentActivity[] = [];
    for (const row of rawRows) {
      const prev = deduped[deduped.length - 1];
      if (
        prev &&
        prev.kind === row.kind &&
        prev.label === row.label &&
        prev.detail === row.detail
      ) {
        // Keep the newer timestamp + state; drop the duplicate row.
        prev.timestamp = Math.max(prev.timestamp, row.timestamp);
        prev.state = row.state;
        continue;
      }
      deduped.push(row);
    }
    // Keep the most recent 40 rows so streaming Agent messages aren't dropped.
    const tail = deduped.slice(-40);
    // When the agent run is finished (complete/error/cancelled), no more
    // tool frames will arrive — flip any still-spinning rows to done so the
    // UI doesn't keep ticking forever.
    tail.forEach((row) => events.push(row));
  }

  // Sort timestamp DESC, dedupe by id (last write wins).
  const byId = new Map<string, AgentActivity>();
  for (const e of events) byId.set(e.id, e);
  const list = Array.from(byId.values()).sort(
    (a, b) => b.timestamp - a.timestamp,
  );
  return list.slice(0, MAX_ENTRIES);
}
