import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  ArrowUpDown,
  Bookmark,
  Bot,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleDot,
  KeyRound,
  Loader2,
  MessageSquareText,
  Send,
  User,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { useLocale } from "../../i18n";
import type {
  TemplateAgentConfig,
  TemplateAgentEvent,
  TemplateAgentStatus,
} from "../../lib/types";
import type { AgentActivity } from "./agentActivity";

interface ChatMessage {
  role: "user" | "assistant" | "system" | string;
  content: string;
  created_at?: number;
  meta?: Record<string, unknown>;
}

export interface CollabPanelProps {
  conversation: ChatMessage[];
  activityEvents?: AgentActivity[];
  /** Raw agent SDK events used to derive usage / cost (Agent mode). */
  agentEvents?: TemplateAgentEvent[];
  replyLanguage: "zh" | "en";
  loading: boolean;
  mode: "classic" | "agent";
  onModeChange: (mode: "classic" | "agent") => void;
  modeLocked?: boolean;
  agentConfig: TemplateAgentConfig;
  onAgentConfigChange: (config: TemplateAgentConfig) => void;
  agentStatus?: TemplateAgentStatus | null;
  onSendFeedback: (text: string) => Promise<void> | void;
  modelConfigured: boolean;
  className?: string;
  /** Number of user-drawn annotations on the active import. */
  annotationCount?: number;
  /** Resolved model label for the status footer (e.g. ``Claude Sonnet 4.5``). */
  modelLabel?: string;
}

/**
 * Right-pane chat for the template-import flow. The reply-language
 * hint above the textarea is computed by the parent via
 * `detectUserLanguage` so it always matches what the backend will use.
 */
export function CollabPanel({
  conversation,
  activityEvents = [],
  agentEvents = [],
  replyLanguage: _replyLanguage,
  loading,
  mode,
  onModeChange,
  modeLocked = false,
  agentConfig,
  onAgentConfigChange,
  agentStatus,
  onSendFeedback,
  modelConfigured,
  className,
  annotationCount = 0,
  modelLabel,
}: CollabPanelProps) {
  const { t } = useLocale();
  const [draft, setDraft] = useState("");
  const [pendingUser, setPendingUser] = useState<ChatMessage | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);

  // Latest "usage" event drives the cost / token panel. Backend keeps
  // running totals so we just take the newest payload.
  const usage = useMemo(() => {
    for (let i = agentEvents.length - 1; i >= 0; i -= 1) {
      const event = agentEvents[i];
      if (event.type !== "usage") continue;
      const data = (event.data ?? {}) as Record<string, unknown>;
      return {
        model: typeof data.model === "string" ? data.model : null,
        input_tokens: Number(data.input_tokens ?? 0),
        output_tokens: Number(data.output_tokens ?? 0),
        cache_read_input_tokens: Number(data.cache_read_input_tokens ?? 0),
        cache_creation_input_tokens: Number(data.cache_creation_input_tokens ?? 0),
        total_cost_usd: Number(data.total_cost_usd ?? 0),
        num_turns: Number(data.num_turns ?? 0),
        duration_ms: Number(data.duration_ms ?? 0),
      };
    }
    return null;
  }, [agentEvents]);

  // Prefer the model name reported by the SDK over anything the UI
  // assumed (e.g. the static "Claude Code" preset label).
  const resolvedModelLabel = mode === "agent" ? usage?.model || modelLabel : modelLabel;

  // Determine whether to show the "thinking..." bubble. Keep it visible while
  // the model is between tool calls/messages; hide it only when a secondary
  // tool row is actively executing.
  const agentRunning = agentStatus?.status === "running" || agentStatus?.status === "queued";
  const visibleActivityEvents = useMemo(() => {
    if (mode !== "agent" || (!loading && !agentRunning)) return activityEvents;
    return activityEvents;
  }, [activityEvents, agentRunning, loading, mode]);

  const hasActiveSecondaryEvent = useMemo(
    () => visibleActivityEvents.some((event) => event.collapsible && event.state === "active"),
    [visibleActivityEvents],
  );
  const showThinking = mode === "agent" && (loading || agentRunning) && !hasActiveSecondaryEvent;

  // Once the saved conversation reflects the optimistic message (matched on
  // content + role), drop our local copy.
  useEffect(() => {
    if (!pendingUser) return;
    const matched = conversation.some(
      (msg) => msg.role === "user" && msg.content === pendingUser.content,
    );
    if (matched) setPendingUser(null);
  }, [conversation, pendingUser]);

  // Reset pending message and clear stale thinking state when leaving the
  // import (or finishing a run that produced no message).
  useEffect(() => {
    if (!loading && !agentRunning) {
      setPendingUser(null);
    }
  }, [loading, agentRunning]);
  const timeline = useMemo(() => {
    type MessageItem = {
      type: "message";
      key: string;
      timestamp: number;
      message: ChatMessage;
    };
    type ActivityItem = {
      type: "activity";
      key: string;
      timestamp: number;
      event: AgentActivity;
    };
    type GroupItem = {
      type: "group";
      key: string;
      timestamp: number;
      events: AgentActivity[];
    };
    type ThinkingItem = {
      type: "thinking";
      key: string;
      timestamp: number;
    };

    // Collect contents of streamed primary agent events so we can drop the
    // saved-conversation duplicate the backend appends on completion.
    const primaryAgentTexts = new Set<string>();
    visibleActivityEvents.forEach((event) => {
      if (event.primary && event.detail) {
        primaryAgentTexts.add(event.detail.trim());
      }
    });

    const messages: MessageItem[] = conversation
      .slice(-30)
      .filter((message) => {
        if (message.role !== "assistant") return true;
        const meta = (message.meta ?? {}) as Record<string, unknown>;
        const isAgent = meta.mode === "agent" || Boolean(meta.agent_job_id);
        if (!isAgent) return true;
        return !primaryAgentTexts.has(message.content.trim());
      })
      .map((message, index) => ({
        type: "message",
        key: `message:${index}:${message.created_at ?? "na"}`,
        timestamp: normalizeTimestamp(message.created_at) || Date.now() + index,
        message,
      }));
    if (pendingUser) {
      messages.push({
        type: "message",
        key: `pending:${pendingUser.created_at ?? "now"}`,
        timestamp: normalizeTimestamp(pendingUser.created_at) || Date.now(),
        message: pendingUser,
      });
    }

    const activities: ActivityItem[] = visibleActivityEvents
      .filter((event) => !event.id.startsWith("conv:"))
      .slice(-40)
      .map((event) => ({
        type: "activity",
        key: `activity:${event.id}`,
        timestamp: event.timestamp,
        event,
      }));

    const merged: Array<MessageItem | ActivityItem | GroupItem | ThinkingItem> = [
      ...messages,
      ...activities,
    ].sort((a, b) => a.timestamp - b.timestamp);

    // Walk the merged stream and fold consecutive collapsible activities
    // into a single group so the feed is dominated by primary messages.
    const out: Array<MessageItem | ActivityItem | GroupItem | ThinkingItem> = [];
    for (const item of merged) {
      if (item.type === "activity" && item.event.collapsible) {
        const prev = out[out.length - 1];
        if (prev && prev.type === "group") {
          prev.events.push(item.event);
          prev.timestamp = Math.max(prev.timestamp, item.timestamp);
          continue;
        }
        out.push({
          type: "group",
          key: `group:${item.event.id}`,
          timestamp: item.timestamp,
          events: [item.event],
        });
        continue;
      }
      out.push(item);
    }

    if (showThinking) {
      out.push({
        type: "thinking",
        key: "thinking-indicator",
        timestamp: Date.now(),
      });
    }
    return out.slice(-60);
  }, [conversation, pendingUser, showThinking, visibleActivityEvents]);

  useEffect(() => {
    const node = scrollerRef.current;
    if (!node) return;
    // Use auto (instant) — smooth scroll combined with rapid streaming
    // updates causes visible overlap / jitter.
    const frame = window.requestAnimationFrame(() => {
      node.scrollTop = node.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [timeline.length, loading]);

  const send = async () => {
    if (!draft.trim() || loading || !modelConfigured) return;
    const text = draft.trim();
    setDraft("");
    // Optimistic user bubble: surface the message immediately and clear it
    // automatically once the saved conversation contains the same content.
    setPendingUser({
      role: "user",
      content: text,
      created_at: Date.now() / 1000,
      meta: { mode },
    });
    try {
      await onSendFeedback(text);
    } catch {
      // Drop the optimistic copy if the call fails; the parent will surface
      // the error separately.
      setPendingUser(null);
    }
  };

  return (
    <aside
      className={`ti-console-panel flex h-full flex-col ${className ?? ""}`}
      style={{ background: "var(--ti-surface)" }}
      aria-label={t("template.collab.label")}
    >
      <section className="flex flex-1 flex-col gap-2 p-3 min-h-0">
        <CollabModeControls
          mode={mode}
          onModeChange={onModeChange}
          modeLocked={modeLocked}
          agentConfig={agentConfig}
          onAgentConfigChange={onAgentConfigChange}
          disabled={loading}
          agentStatus={agentStatus}
        />
        <div
          ref={scrollerRef}
          className="ti-console-timeline"
          style={{ scrollbarGutter: "stable" }}
        >
          {timeline.length === 0 ? (
            <p className="text-xs" style={{ color: "var(--ti-muted)" }}>
              {t("template.collab.empty")}
            </p>
          ) : (
            timeline.map((item) =>
              item.type === "message" ? (
                <ChatBubble key={item.key} message={item.message} />
              ) : item.type === "thinking" ? (
                <ThinkingBubble key={item.key} />
              ) : item.type === "group" ? (
                <ActivityGroup key={item.key} events={item.events} />
              ) : (
                <ActivityLine key={item.key} event={item.event} />
              ),
            )
          )}
        </div>
        <div
          className="ti-console-composer"
          style={{ borderColor: "var(--ti-line)", background: "var(--ti-surface)" }}
        >
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void send();
              }
            }}
            placeholder={t("template.feedbackPlaceholder")}
            rows={3}
            disabled={loading || !modelConfigured}
            className="ti-focusable ti-console-composer-textarea"
            style={{ color: "var(--ti-text)" }}
          />
          <div className="ti-console-composer-footer">
            <div className="ti-console-composer-meta">
              <span className="ti-console-meta-pill" title={t("template.collab.annotations")}>
                <Bookmark size={10} />
                <span>{annotationCount}</span>
              </span>
              {resolvedModelLabel ? (
                <span
                  className="ti-console-meta-pill"
                  title={t("template.collab.model")}
                >
                  <Bot size={10} />
                  <span className="ti-console-meta-text">{resolvedModelLabel}</span>
                </span>
              ) : null}
              {mode === "agent" && usage && (usage.input_tokens > 0 || usage.output_tokens > 0) ? (
                <span
                  className="ti-console-meta-pill"
                  title={tokensTooltip(t, usage)}
                >
                  <ArrowUpDown size={10} />
                  <span>
                    {formatTokens(usage.input_tokens)} / {formatTokens(usage.output_tokens)}
                  </span>
                </span>
              ) : null}
            </div>
            <button
              type="button"
              onClick={() => void send()}
              disabled={loading || !modelConfigured || !draft.trim()}
              className="ti-console-composer-send disabled:cursor-not-allowed disabled:opacity-50"
              style={{ background: "var(--ti-accent)", color: "var(--ti-accent-fg)" }}
            >
              {loading ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Send size={12} />
              )}
              <span>{t("template.collab.send")}</span>
            </button>
          </div>
        </div>
      </section>
    </aside>
  );
}

function CollabModeControls({
  mode,
  onModeChange,
  modeLocked,
  agentConfig,
  onAgentConfigChange,
  disabled,
  agentStatus,
}: {
  mode: "classic" | "agent";
  onModeChange: (mode: "classic" | "agent") => void;
  modeLocked: boolean;
  agentConfig: TemplateAgentConfig;
  onAgentConfigChange: (config: TemplateAgentConfig) => void;
  disabled: boolean;
  agentStatus?: TemplateAgentStatus | null;
}) {
  const setConfig = (patch: Partial<TemplateAgentConfig>) => {
    onAgentConfigChange({ ...agentConfig, ...patch });
  };
  return (
    <div className="flex flex-col gap-2">
      <div
        className="grid grid-cols-2 rounded-[var(--ti-radius-sm,6px)] border p-0.5"
        style={{ borderColor: "var(--ti-line)", background: "var(--ti-surface-inset)" }}
      >
        {(["classic", "agent"] as const).map((item) => {
          const active = mode === item;
          const agentBusy =
            item === "agent" &&
            (agentStatus?.status === "running" || agentStatus?.status === "queued");
          return (
            <button
              key={item}
              type="button"
              disabled={disabled || modeLocked}
              onClick={() => onModeChange(item)}
              className="ti-focusable inline-flex items-center justify-center gap-1 rounded px-2 py-1 text-[11px] font-semibold"
              style={{
                background: active ? "var(--ti-surface)" : "transparent",
                color: active ? "var(--ti-text)" : "var(--ti-muted)",
              }}
              aria-pressed={active}
            >
              {agentBusy ? (
                <Loader2 size={11} className="animate-spin" />
              ) : item === "agent" ? (
                <Bot size={11} />
              ) : (
                <MessageSquareText size={11} />
              )}
              {item === "agent" ? "Agent" : "LLM"}
            </button>
          );
        })}
      </div>

      {mode === "agent" ? (
        <div
          className="flex flex-col gap-2 rounded-[var(--ti-radius-sm,6px)] border p-2"
          style={{ borderColor: "var(--ti-line)", background: "var(--ti-surface-inset)" }}
        >
          <div className="grid grid-cols-2 gap-1">
            <ConfigButton
              active={agentConfig.mode === "claude_code"}
              disabled={disabled}
              label="Claude 配置"
              onClick={() => setConfig({ mode: "claude_code" })}
            />
            <ConfigButton
              active={agentConfig.mode === "custom"}
              disabled={disabled}
              label="自定义端点"
              onClick={() => setConfig({ mode: "custom" })}
            />
          </div>
          {agentConfig.mode === "custom" ? (
            <div className="grid gap-1.5">
              <AgentInput
                icon={<KeyRound size={11} />}
                placeholder="API Key"
                type="password"
                value={agentConfig.api_key ?? ""}
                disabled={disabled}
                onChange={(value) => setConfig({ api_key: value })}
              />
              <AgentInput
                placeholder="Base URL"
                value={agentConfig.base_url ?? ""}
                disabled={disabled}
                onChange={(value) => setConfig({ base_url: value })}
              />
              <AgentInput
                placeholder="Model"
                value={agentConfig.model ?? ""}
                disabled={disabled}
                onChange={(value) => setConfig({ model: value })}
              />
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ConfigButton({
  active,
  disabled,
  label,
  onClick,
}: {
  active: boolean;
  disabled: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="ti-focusable rounded border px-2 py-1 text-[11px] font-semibold"
      style={{
        borderColor: active ? "var(--ti-accent)" : "var(--ti-line)",
        background: active ? "color-mix(in srgb, var(--ti-accent) 12%, var(--ti-surface))" : "var(--ti-surface)",
        color: active ? "var(--ti-accent)" : "var(--ti-muted)",
      }}
      aria-pressed={active}
    >
      {label}
    </button>
  );
}

function AgentInput({
  icon,
  placeholder,
  type = "text",
  value,
  disabled,
  onChange,
}: {
  icon?: ReactNode;
  placeholder: string;
  type?: string;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <label
      className="flex items-center gap-1 rounded border px-2 py-1"
      style={{ borderColor: "var(--ti-line)", background: "var(--ti-surface)" }}
    >
      {icon}
      <input
        type={type}
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        onChange={(event) => onChange(event.currentTarget.value)}
        className="min-w-0 flex-1 bg-transparent text-[11px] outline-none"
        style={{ color: "var(--ti-text)" }}
      />
    </label>
  );
}

function ActivityLine({ event }: { event: AgentActivity }) {
  if (event.primary && event.detail) {
    // Primary Agent assistant message: render full markdown bubble.
    return (
      <ChatBubble
        message={{
          role: "assistant",
          content: event.detail,
          created_at: event.timestamp,
        }}
      />
    );
  }
  return (
    <div className="ti-console-activity" data-state={event.state} data-kind={event.kind}>
      <span className="ti-console-activity-icon" aria-hidden="true">
        <ActivityIcon event={event} />
      </span>
      <span className="ti-console-activity-label">{event.label}</span>
      <span className="ti-console-activity-copy">
        {event.detail ? <em>{event.detail}</em> : null}
      </span>
      {event.state === "active" ? (
        <Loader2 size={11} className="animate-spin" />
      ) : null}
    </div>
  );
}

function ActivityGroup({ events }: { events: AgentActivity[] }) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  if (events.length === 0) return null;
  const last = events[events.length - 1];
  const isActive = events.some((e) => e.state === "active");
  const hasError = events.some((e) => e.state === "error");
  const summaryState = hasError ? "error" : isActive ? "active" : "done";
  const summaryLabel =
    hasError && events.length === 1
      ? last.label
      : isActive
        ? t("template.collab.steps")
        : t("template.collab.stepsDone");
  const recentOperation = formatActivitySummary(
    [...events].reverse().find((event) => event.kind !== "assistant" && event.detail) ?? last,
  );
  const summaryDetail =
    hasError && events.length === 1 && last.detail && !last.detail.startsWith("{")
      ? last.detail
      : recentOperation;
  return (
    <div className="ti-console-group" data-open={open ? "true" : "false"}>
      <button
        type="button"
        className="ti-console-group-summary"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        data-state={summaryState}
      >
        <span className="ti-console-group-chevron" aria-hidden="true">
          {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
        </span>
        <span className="ti-console-activity-label">{summaryLabel}</span>
        <span className="ti-console-activity-copy">
          {summaryDetail ? <em>{summaryDetail}</em> : null}
        </span>
        {isActive ? (
          <Loader2 size={11} className="animate-spin" />
        ) : hasError ? (
          <AlertTriangle size={11} />
        ) : (
          <CheckCircle2 size={11} />
        )}
      </button>
      {open ? (
        <div className="ti-console-group-body">
          {events.map((event) => (
            <ActivityLine key={event.id} event={event} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function formatActivitySummary(event: AgentActivity): string {
  const label = event.label
    .replace(/^(正在执行|已执行|调用)\s+/, "")
    .trim();
  const detail = event.detail && !event.detail.trim().startsWith("{")
    ? event.detail.trim()
    : "";
  if (!label || label === "Agent") return detail;
  return [label, detail].filter(Boolean).join(" ");
}

function ChatBubble({ message }: { message: ChatMessage }) {
  const { t } = useLocale();
  const isUser = message.role === "user";
  return (
    <div className="ti-bubble-row" data-role={isUser ? "user" : "assistant"}>
      <div className="ti-bubble">
        <div className="ti-bubble-header">
          <span className="ti-bubble-avatar" aria-hidden="true">
            {isUser ? <User size={11} /> : <Bot size={11} />}
          </span>
          <span className="ti-bubble-name">
            {isUser ? t("template.chatUser") : t("template.chatAssistant")}
          </span>
        </div>
        {isUser ? (
          <p className="ti-bubble-content">{message.content}</p>
        ) : (
          <div className="ti-bubble-content ti-markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
          </div>
        )}
      </div>
    </div>
  );
}

function ThinkingBubble() {
  const { t } = useLocale();
  return (
    <div className="ti-bubble-row" data-role="assistant">
      <div className="ti-bubble" data-thinking="true">
        <div className="ti-bubble-header">
          <span className="ti-bubble-avatar" aria-hidden="true">
            <Bot size={11} />
          </span>
          <span className="ti-bubble-name">{t("template.chatAssistant")}</span>
        </div>
        <p className="ti-bubble-content ti-thinking">
          <span>{t("template.collab.thinking")}</span>
          <span className="ti-thinking-dots" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
        </p>
      </div>
    </div>
  );
}

function ActivityIcon({ event }: { event: AgentActivity }) {
  if (event.state === "error") return <AlertTriangle size={11} />;
  switch (event.kind) {
    case "llm":
      return <Bot size={11} />;
    case "user":
      return <User size={11} />;
    case "assistant":
      return <MessageSquareText size={11} />;
    case "pipeline":
      return event.state === "done" ? <CheckCircle2 size={11} /> : <CircleDot size={11} />;
    case "info":
    default:
      return <CircleDot size={11} />;
  }
}

function normalizeTimestamp(value: number | undefined): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return 0;
  return value < 1e12 ? value * 1000 : value;
}

function formatTokens(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 10_000) return `${(value / 1000).toFixed(1)}k`;
  if (value >= 1000) return `${(value / 1000).toFixed(2)}k`;
  return value.toLocaleString();
}

function tokensTooltip(
  t: (key: string) => string,
  usage: {
    input_tokens: number;
    output_tokens: number;
    cache_read_input_tokens: number;
    cache_creation_input_tokens: number;
  },
): string {
  const lines = [
    `${t("template.collab.tokensInput")}: ${usage.input_tokens.toLocaleString()}`,
    `${t("template.collab.tokensOutput")}: ${usage.output_tokens.toLocaleString()}`,
  ];
  if (usage.cache_read_input_tokens > 0) {
    lines.push(
      `${t("template.collab.tokensCacheRead")}: ${usage.cache_read_input_tokens.toLocaleString()}`,
    );
  }
  if (usage.cache_creation_input_tokens > 0) {
    lines.push(
      `${t("template.collab.tokensCacheCreate")}: ${usage.cache_creation_input_tokens.toLocaleString()}`,
    );
  }
  return lines.join("\n");
}
