import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import {
  AlertTriangle,
  ArrowUpDown,
  AtSign,
  Bookmark,
  Bot,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleDot,
  FileCheck2,
  File as FileIcon,
  Folder,
  Eye,
  EyeOff,
  Loader2,
  MessageSquareText,
  Send,
  Square,
  User,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { useLocale } from "../../i18n";
import { HoverTooltip } from "../common/HoverTooltip";
import { fetchTemplateImportFiles } from "../../lib/api";
import type {
  ImportStatus,
  TemplateAgentConfig,
  TemplateAgentEvent,
  TemplateAgentStatus,
  TemplateImportFileItem,
  TemplatePageType,
  TemplateReview,
  TemplateReviewDraft,
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
  mode: "agent" | "direct";
  onModeChange: (mode: "agent" | "direct") => void;
  modeLocked?: boolean;
  agentConfig: TemplateAgentConfig;
  onAgentConfigChange: (config: TemplateAgentConfig) => void;
  agentModelOptions?: string[];
  agentStatus?: TemplateAgentStatus | null;
  agentCancelPending?: boolean;
  onSendFeedback: (text: string) => Promise<void> | void;
  onStopAgent?: () => Promise<void> | void;
  importId?: string | null;
  contextAttachments?: Array<{ id: string; label: string; detail?: string }>;
  modelConfigured: boolean;
  className?: string;
  /** Number of user-drawn annotations on the active import. */
  annotationCount?: number;
  /** Resolved model label for the status footer (e.g. ``Claude Sonnet 4.5``). */
  modelLabel?: string;
  importStatus?: ImportStatus | null;
  review?: TemplateReview | null;
  draftState?: TemplateReviewDraft;
  directDesignSpec?: string;
  directImportBusy?: boolean;
  onConfirmDirectImport?: () => Promise<void> | void;
  onEditDirectImport?: () => void;
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
  agentModelOptions,
  agentStatus,
  agentCancelPending = false,
  onSendFeedback,
  onStopAgent,
  importId,
  contextAttachments = [],
  modelConfigured,
  className,
  annotationCount = 0,
  modelLabel,
  importStatus,
  review,
  draftState,
  directDesignSpec = "",
  directImportBusy = false,
  onConfirmDirectImport,
  onEditDirectImport,
}: CollabPanelProps) {
  const { t } = useLocale();
  const [draft, setDraft] = useState("");
  const [mentions, setMentions] = useState<Array<{ label: string; path: string }>>([]);
  const [mentionOpen, setMentionOpen] = useState(false);
  const [pendingUser, setPendingUser] = useState<ChatMessage | null>(null);
  const [showAgentCredential, setShowAgentCredential] = useState(false);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const directScrollerRef = useRef<HTMLDivElement | null>(null);

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
  const canStopAgent = mode === "agent" && (loading || agentRunning || agentCancelPending) && Boolean(onStopAgent);
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
      (msg) => msg.role === "user" && stripReferencedFiles(msg.content) === pendingUser.content,
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

  useEffect(() => {
    if (mode !== "direct" || !directDesignSpec) return;
    const scroller = directScrollerRef.current;
    if (!scroller) return;
    requestAnimationFrame(() => {
      scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
    });
  }, [directDesignSpec, mode]);
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

  const chatTimeline = useMemo(() => {
    const messages = conversation.slice(-30).map((message, index) => ({
      key: `message:${index}:${message.created_at ?? "na"}`,
      timestamp: normalizeTimestamp(message.created_at) || Date.now() + index,
      message,
    }));
    if (pendingUser) {
      messages.push({
        key: `pending:${pendingUser.created_at ?? "now"}`,
        timestamp: normalizeTimestamp(pendingUser.created_at) || Date.now(),
        message: pendingUser,
      });
    }
    return messages.sort((a, b) => a.timestamp - b.timestamp);
  }, [conversation, pendingUser]);

  useEffect(() => {
    const node = scrollerRef.current;
    if (!node) return;
    // Use auto (instant) — smooth scroll combined with rapid streaming
    // updates causes visible overlap / jitter.
    const frame = window.requestAnimationFrame(() => {
      node.scrollTop = node.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [timeline.length, chatTimeline.length, loading]);

  const send = async () => {
    if (mode === "direct") return;
    if (canStopAgent) {
      await onStopAgent?.();
      return;
    }
    if (!draft.trim() && mentions.length === 0 && contextAttachments.length === 0) return;
    if (!modelConfigured) return;
    const text = draft.trim();
    const uniqueMentions = mentions.filter(
      (mention, index, arr) => arr.findIndex((item) => item.path === mention.path) === index,
    );
    const mentionBlock = uniqueMentions.length > 0
      ? `Referenced files:\n${uniqueMentions.map((item) => `- ${item.label}: ${item.path}`).join("\n")}`
      : "";
    const contextText = contextAttachments.map((item) => item.label).join(" ");
    const displayText = text || uniqueMentions.map((item) => item.label).join(" ") || contextText;
    const agentText =
      mode === "agent" && mentionBlock
        ? [displayText, mentionBlock].filter(Boolean).join("\n\n")
        : text;
    setDraft("");
    setMentions([]);
    // Optimistic user bubble: surface the message immediately and clear it
    // automatically once the saved conversation contains the same content.
    setPendingUser({
      role: "user",
      content: displayText,
      created_at: Date.now() / 1000,
      meta: { mode },
    });
    try {
      await onSendFeedback(agentText);
    } catch {
      // Drop the optimistic copy if the call fails; the parent will surface
      // the error separately.
      setPendingUser(null);
    }
  };

  const composer = (
    <div
      className="ti-console-composer"
      style={{ borderColor: "var(--ti-line)", background: "var(--ti-surface)" }}
    >
      {mentions.length > 0 || contextAttachments.length > 0 ? (
        <div className="ti-console-mentions">
          {contextAttachments.map((item) => (
            <HoverTooltip key={item.id} content={item.detail ?? item.label} className="ti-inline-tooltip-trigger">
              <span className="ti-console-mention-chip ti-console-context-chip">
                <ArrowUpDown size={11} />
                {item.label}
              </span>
            </HoverTooltip>
          ))}
          {mentions.map((mention) => (
            <HoverTooltip key={mention.path} content={mention.path} className="ti-inline-tooltip-trigger">
              <span className="ti-console-mention-chip">
                <FileIcon size={11} />
                {mention.label}
                <button
                  type="button"
                  onClick={() => setMentions((prev) => prev.filter((item) => item.path !== mention.path))}
                  aria-label={t("template.collab.removeMention")}
                >
                  <X size={10} />
                </button>
              </span>
            </HoverTooltip>
          ))}
        </div>
      ) : null}
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
        disabled={!modelConfigured}
        className="ti-focusable ti-console-composer-textarea"
        style={{ color: "var(--ti-text)" }}
      />
      <div className="ti-console-composer-footer">
        <div className="ti-console-composer-meta">
          <HoverTooltip content={t("template.collab.annotations")} className="ti-inline-tooltip-trigger">
            <span className="ti-console-meta-pill">
              <Bookmark size={10} />
              <span>{annotationCount}</span>
            </span>
          </HoverTooltip>
          {mode === "agent" && importId ? (
            <div className="ti-console-mention-wrap">
              <HoverTooltip content={t("template.collab.attachFile")} className="ti-inline-tooltip-trigger">
                <button
                  type="button"
                  className="ti-console-mention-button"
                  onClick={() => setMentionOpen((open) => !open)}
                  aria-expanded={mentionOpen}
                >
                  <AtSign size={11} />
                </button>
              </HoverTooltip>
              {mentionOpen ? (
                <FileMentionPopover
                  importId={importId}
                  onSelect={(file) => {
                    const label = `@${file.name}`;
                    setMentions((prev) =>
                      prev.some((item) => item.path === file.path)
                        ? prev
                        : [...prev, { label, path: file.path }],
                    );
                    setMentionOpen(false);
                  }}
                />
              ) : null}
            </div>
          ) : null}
          {resolvedModelLabel ? (
            <HoverTooltip content={t("template.collab.model")} className="ti-inline-tooltip-trigger">
              <span className="ti-console-meta-pill">
                <Bot size={10} />
                <span className="ti-console-meta-text">{resolvedModelLabel}</span>
              </span>
            </HoverTooltip>
          ) : null}
          {mode === "agent" ? (
            <HoverTooltip content={tokensTooltip(t, usage)} className="ti-inline-tooltip-trigger">
              <span className="ti-console-meta-pill">
                <ArrowUpDown size={10} />
                <span>
                  {formatTokens(usage?.input_tokens ?? 0)} / {formatTokens(usage?.output_tokens ?? 0)}
                </span>
              </span>
            </HoverTooltip>
          ) : null}
        </div>
        <button
          type="button"
          onClick={() => void send()}
          disabled={!modelConfigured || (!canStopAgent && !draft.trim() && mentions.length === 0 && contextAttachments.length === 0)}
          className="ti-console-composer-send disabled:cursor-not-allowed disabled:opacity-50"
          data-busy={canStopAgent ? "true" : "false"}
          style={{ background: "var(--ti-accent)", color: "var(--ti-accent-fg)" }}
        >
          {canStopAgent ? (
            agentCancelPending ? (
              <Loader2 size={12} className="animate-spin" />
            ) : (
              <Square size={12} fill="currentColor" />
            )
          ) : loading ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <Send size={12} />
          )}
          <span>{agentCancelPending ? t("template.collab.stopping") : canStopAgent ? t("template.collab.stop") : t("template.collab.send")}</span>
        </button>
      </div>
    </div>
  );

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
          agentModelOptions={agentModelOptions}
          showAgentCredential={showAgentCredential}
          onToggleAgentCredential={() => setShowAgentCredential((value) => !value)}
          disabled={loading}
          agentStatus={agentStatus}
        />
        {mode === "direct" ? (
          <div ref={directScrollerRef} className="ti-console-timeline ti-direct-review-timeline">
            {directDesignSpec ? (
              <DirectDesignSpecReview
                markdown={directDesignSpec}
                busy={directImportBusy}
                onConfirm={onConfirmDirectImport}
                onEdit={onEditDirectImport}
              />
            ) : loading ? (
              <div className="ti-direct-import-status">
                <Loader2 size={14} className="animate-spin" />
                <span>{t("template.directGeneratingDesignSpec")}</span>
              </div>
            ) : null}
          </div>
        ) : (
          <>
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
          </>
        )}
        {mode === "agent" ? composer : null}
      </section>
    </aside>
  );
}

const TEMPLATE_PAGE_TYPES: TemplatePageType[] = ["cover", "toc", "chapter", "content", "ending"];

function LlmCollabWorkspace({
  status,
  review,
  draft,
  chatItems,
  loading,
  scrollerRef,
}: {
  status?: ImportStatus | null;
  review?: TemplateReview | null;
  draft?: TemplateReviewDraft;
  chatItems: Array<{ key: string; message: ChatMessage }>;
  loading: boolean;
  scrollerRef: RefObject<HTMLDivElement | null>;
}) {
  const { t } = useLocale();
  const effectiveDraft = draft ?? review?.draft ?? {};
  const actions = effectiveDraft.element_actions ?? [];
  const replaceCount = actions.filter((action) => action.action === "replace_with_placeholder").length;
  const removeCount = actions.filter((action) => action.action === "remove").length;
  const keepCount = actions.filter((action) => action.action === "keep").length;
  const changed = Boolean(review?.llm_trace?.changed ?? review?.llm?.changed);
  const llmComplete = review?.llm?.status === "complete";
  const selections = effectiveDraft.page_selections ?? {};
  const steps = status?.steps ?? [];

  return (
    <div className="ti-llm-workspace">
      <section className="ti-llm-card ti-llm-progress-card" aria-label={t("template.llmFlow.progress")}>
        <div className="ti-llm-section-head">
          <span>{t("template.llmFlow.progress")}</span>
          {status?.stage ? <em>{t(`template.step.${status.stage}`)}</em> : null}
        </div>
        <div className="ti-llm-step-list">
          {steps.length > 0 ? (
            steps.map((step) => (
              <div className="ti-llm-step" data-state={step.status ?? "info"} key={step.id}>
                <span className="ti-llm-step-icon" aria-hidden="true">
                  {step.status === "complete" ? (
                    <CheckCircle2 size={12} />
                  ) : step.status === "active" ? (
                    <Loader2 size={12} className="animate-spin" />
                  ) : (
                    <CircleDot size={12} />
                  )}
                </span>
                <span>{t(`template.step.${step.id}`)}</span>
              </div>
            ))
          ) : (
            <p className="ti-llm-empty">{t("template.llmFlow.noProgress")}</p>
          )}
        </div>
      </section>

      <section className="ti-llm-card ti-llm-review-card" aria-label={t("template.llmFlow.review")}>
        <div className="ti-llm-section-head">
          <span>{t("template.llmFlow.review")}</span>
          <em>{llmComplete ? t("template.llmFlow.reviewComplete") : loading ? t("template.llmFlow.reviewing") : t("template.llmFlow.waiting")}</em>
        </div>
        <div className="ti-llm-stat-grid">
          <Metric label={t("template.llmFlow.changed")} value={changed ? t("template.llmFlow.yes") : t("template.llmFlow.no")} />
          <Metric label={t("template.llmFlow.placeholders")} value={replaceCount} />
          <Metric label={t("template.llmFlow.removed")} value={removeCount} />
          <Metric label={t("template.llmFlow.kept")} value={keepCount} />
        </div>
        <div className="ti-llm-pages">
          {TEMPLATE_PAGE_TYPES.map((pageType) => {
            const slide = selections[pageType];
            return (
              <span key={pageType}>
                {t(`template.page.${pageType}`)}
                <b>{typeof slide === "number" && slide > 0 ? t("template.activity.slideNumber").replace("{slide}", String(slide)) : t("template.unassigned")}</b>
              </span>
            );
          })}
        </div>
      </section>

      <section className="ti-llm-chat-card" aria-label={t("template.llmFlow.feedback")}>
        <div className="ti-llm-section-head">
          <span>{t("template.llmFlow.feedback")}</span>
        </div>
        <div ref={scrollerRef} className="ti-llm-chat-scroll" style={{ scrollbarGutter: "stable" }}>
          {chatItems.length === 0 ? (
            <p className="ti-llm-empty" aria-hidden="true" />
          ) : (
            chatItems.map((item) => <ChatBubble key={item.key} message={item.message} />)
          )}
          {loading ? <ThinkingBubble /> : null}
        </div>
      </section>
    </div>
  );
}

function DirectDesignSpecReview({
  markdown,
  busy,
  onConfirm,
  onEdit,
}: {
  markdown: string;
  busy: boolean;
  onConfirm?: () => Promise<void> | void;
  onEdit?: () => void;
}) {
  const { t } = useLocale();
  return (
    <div className="ti-direct-design-review">
      <div className="ti-bubble-row" data-role="assistant">
        <div className="ti-bubble">
          <div className="ti-bubble-header">
            <span className="ti-bubble-avatar" aria-hidden="true">
              <Bot size={11} />
            </span>
            <span className="ti-bubble-name">{t("template.directDesignSpecPreview")}</span>
          </div>
          <div className="ti-bubble-content ti-markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>
          </div>
        </div>
      </div>
      <div className="ti-direct-review-actions">
        <button
          type="button"
          className="ti-focusable ti-direct-review-secondary"
          disabled={busy}
          onClick={onEdit}
        >
          <X size={13} />
          <span>{t("template.directReview.edit")}</span>
        </button>
        <button
          type="button"
          className="ti-focusable ti-direct-review-primary"
          disabled={busy || !onConfirm}
          onClick={() => void onConfirm?.()}
        >
          {busy ? <Loader2 size={13} className="animate-spin" /> : <CheckCircle2 size={13} />}
          <span>{t("template.directReview.confirm")}</span>
        </button>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <span className="ti-llm-metric">
      <em>{label}</em>
      <b>{value}</b>
    </span>
  );
}

function FileMentionPopover({
  importId,
  onSelect,
}: {
  importId: string;
  onSelect: (file: TemplateImportFileItem) => void;
}) {
  const { t } = useLocale();
  const [cwd, setCwd] = useState("");
  const [parent, setParent] = useState<string | null>(null);
  const [items, setItems] = useState<TemplateImportFileItem[]>([]);
  const [hoveredPreview, setHoveredPreview] = useState<TemplateImportFileItem | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchTemplateImportFiles(importId, cwd)
      .then((list) => {
        if (cancelled) return;
        setItems(list.items);
        setParent(list.parent ?? null);
        setHoveredPreview(null);
      })
      .catch((err) => {
        if (cancelled) return;
        setItems([]);
        setParent(null);
        setHoveredPreview(null);
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [cwd, importId]);

  return (
      <div className="ti-file-popover" role="dialog" aria-label={t("template.collab.fileBrowser")}>
        <div className="ti-file-popover-head">
        <HoverTooltip content={cwd || "."} className="ti-file-head-tooltip-trigger">
          <span>{cwd || "."}</span>
        </HoverTooltip>
        {parent !== null ? (
          <button type="button" onClick={() => setCwd(parent)}>{t("template.collab.upDir")}</button>
        ) : null}
      </div>
      <div className="ti-file-list">
        {loading ? (
          <div className="ti-file-empty"><Loader2 size={12} className="animate-spin" /> {t("template.collab.loadingFiles")}</div>
        ) : error ? (
          <div className="ti-file-empty">{error}</div>
        ) : items.length === 0 ? (
          <div className="ti-file-empty">{t("template.collab.noFiles")}</div>
        ) : (
          items.map((item) => (
            <HoverTooltip key={item.path} content={item.path} className="ti-file-item-tooltip-trigger">
              <button
                type="button"
                className="ti-file-item"
                onClick={() => {
                  if (item.type === "directory") setCwd(item.path);
                  else onSelect(item);
                }}
                onMouseEnter={() => setHoveredPreview(item.image && item.preview_url ? item : null)}
                onFocus={() => setHoveredPreview(item.image && item.preview_url ? item : null)}
              >
                {item.type === "directory" ? <Folder size={12} /> : <FileIcon size={12} />}
                <span>{item.name}</span>
                {item.type === "file" && item.size != null ? (
                  <em>{formatFileSize(item.size)}</em>
                ) : null}
              </button>
            </HoverTooltip>
          ))
        )}
      </div>
      <div className="ti-file-preview-pane" data-empty={hoveredPreview?.preview_url ? "false" : "true"}>
        {hoveredPreview?.preview_url ? (
          <>
            <img src={hoveredPreview.preview_url} alt="" />
            <span>{hoveredPreview.name}</span>
          </>
        ) : (
          <span>{t("template.collab.previewEmpty")}</span>
        )}
      </div>
    </div>
  );
}

function CollabModeControls({
  mode,
  onModeChange,
  modeLocked,
  agentConfig,
  onAgentConfigChange,
  agentModelOptions = [],
  showAgentCredential,
  onToggleAgentCredential,
  disabled,
  agentStatus,
}: {
  mode: "agent" | "direct";
  onModeChange: (mode: "agent" | "direct") => void;
  modeLocked: boolean;
  agentConfig: TemplateAgentConfig;
  onAgentConfigChange: (config: TemplateAgentConfig) => void;
  agentModelOptions?: string[];
  showAgentCredential: boolean;
  onToggleAgentCredential: () => void;
  disabled: boolean;
  agentStatus?: TemplateAgentStatus | null;
}) {
  const { t } = useLocale();
  const setConfig = (patch: Partial<TemplateAgentConfig>) => {
    onAgentConfigChange({ ...agentConfig, ...patch });
  };
  const agentModel = agentConfig.model ?? "";
  const isCustomAgent = Boolean(agentConfig.base_url?.trim());
  const agentCredential = agentConfig.api_key ?? agentConfig.auth_token ?? "";
  const setAgentCredential = (value: string) => {
    if (agentConfig.auth_token && !agentConfig.api_key) {
      setConfig({ auth_token: value });
    } else {
      setConfig({ api_key: value });
    }
  };
  return (
    <div className="flex flex-col gap-2">
      <div
        className="grid grid-cols-2 rounded-[var(--ti-radius-sm,6px)] border p-0.5"
        style={{ borderColor: "var(--ti-line)", background: "var(--ti-surface-inset)" }}
      >
        {(["direct", "agent"] as const).map((item) => {
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
                <FileCheck2 size={11} />
              )}
              {item === "agent" ? "Agent" : t("templates.upload.mode.direct")}
            </button>
          );
        })}
      </div>
      {mode === "agent" ? (
        <div className="ti-agent-runtime-fields">
          {isCustomAgent ? (
            <label className="ti-agent-model-field">
              <span>{t("template.collab.agentBaseUrl")}</span>
              <input
                type="text"
                value={agentConfig.base_url ?? ""}
                disabled={disabled}
                onChange={(event) => setConfig({ mode: event.target.value.trim() ? "custom" : "claude_code", base_url: event.target.value })}
                placeholder={t("template.collab.agentBaseUrlPlaceholder")}
              />
            </label>
          ) : null}
          <label className="ti-agent-model-field">
            <span>{t("template.collab.agentRuntimeModel")}</span>
            <input
              type="text"
              list="template-agent-model-options"
              value={agentModel}
              disabled={disabled}
              onChange={(event) => setConfig({ model: event.target.value })}
              placeholder={t("template.collab.agentRuntimeModelPlaceholder")}
            />
            {agentModelOptions.length > 0 ? (
              <datalist id="template-agent-model-options">
                {agentModelOptions.map((option) => (
                  <option key={option} value={option} />
                ))}
              </datalist>
            ) : null}
          </label>
          {isCustomAgent ? (
            <label className="ti-agent-model-field">
              <span>{t("template.collab.agentApiKey")}</span>
              <span className="ti-agent-secret-input">
                <input
                  type={showAgentCredential ? "text" : "password"}
                  value={agentCredential}
                  disabled={disabled}
                  onChange={(event) => setAgentCredential(event.target.value)}
                  placeholder={t("template.collab.agentApiKeyPlaceholder")}
                />
                <button
                  type="button"
                  disabled={disabled}
                  onClick={onToggleAgentCredential}
                  aria-label={showAgentCredential ? t("template.collab.hideApiKey") : t("template.collab.showApiKey")}
                >
                  {showAgentCredential ? <EyeOff size={12} /> : <Eye size={12} />}
                </button>
              </span>
            </label>
          ) : null}
        </div>
      ) : null}

    </div>
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
  // Always default to collapsed — show a single summary row ("正在执行
  // Read xxx" / "已执行 Edit yyy"). User can click to expand the full list.
  const [open, setOpen] = useState(false);
  if (events.length === 0) return null;
  const last = events[events.length - 1];
  const isActive = events.some((e) => e.state === "active");
  const hasError = events.some((e) => e.state === "error");
  const summaryState = hasError ? "error" : isActive ? "active" : "done";
  // Summary surfaces the most recent tool call: "正在执行 Read xxx" while
  // running, "已执行 Read xxx" once that tool finishes. Falls back to the
  // generic group label only when no tool row has any detail to show.
  const recent =
    [...events].reverse().find((event) => event.kind === "pipeline") ??
    [...events].reverse().find((event) => event.kind !== "assistant" && event.detail) ??
    last;
  const summaryLabel = hasError && events.length === 1
    ? last.label
    : recent.label || (isActive ? t("template.collab.steps") : t("template.collab.stepsDone"));
  const summaryDetail =
    hasError && events.length === 1 && last.detail && !last.detail.startsWith("{")
      ? last.detail
      : recent.detail && !recent.detail.startsWith("{")
        ? recent.detail
        : "";
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
    .replace(/^(正在执行|已执行|调用|Running|Executed|Calling)\s+/, "")
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
          <p className="ti-bubble-content">{stripReferencedFiles(message.content)}</p>
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

function stripReferencedFiles(value: string): string {
  return value.replace(/\n\nReferenced files:\n[\s\S]*$/u, "").trim();
}

function formatFileSize(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "";
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  if (value >= 1024) return `${Math.ceil(value / 1024)} KB`;
  return `${value} B`;
}

function tokensTooltip(
  t: (key: string) => string,
  usage: {
    input_tokens: number;
    output_tokens: number;
    cache_read_input_tokens: number;
    cache_creation_input_tokens: number;
  } | null,
): string {
  if (!usage) return t("template.collab.tokensThisTask");
  const lines = [
    t("template.collab.tokensThisTask"),
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
