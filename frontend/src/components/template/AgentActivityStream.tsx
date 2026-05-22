import { useEffect, useMemo, useRef } from "react";
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  CircleDot,
  Loader2,
  MessageSquareText,
  Sparkles,
  User,
} from "lucide-react";

import type { AgentActivity } from "./agentActivity";

export interface AgentActivityStreamProps {
  events: AgentActivity[];
  /** Heading shown above the stream. */
  title: string;
  /** Empty-state copy used when no events have been emitted yet. */
  emptyLabel: string;
  className?: string;
}

/**
 * Live agent activity feed (Claude-Code style).
 *
 * - Newest event at the top, slides in from the top edge with a brief
 *   accent flash (CSS animation `ti-activity-slide-in`).
 * - Active row gets a `Loader2` spinner on the right and a soft accent
 *   background flash via `[data-state="active"]`.
 * - Each row is a single line; details show on hover via the native
 *   `title` attribute.
 * - Scrolls to the top automatically when a new event arrives.
 */
export function AgentActivityStream({
  events,
  title,
  emptyLabel,
  className,
}: AgentActivityStreamProps) {
  const scrollerRef = useRef<HTMLOListElement | null>(null);
  const lastTopIdRef = useRef<string | null>(null);

  // Scroll to top whenever the leading event changes (i.e. a new entry
  // arrived). We avoid scrolling on every render to let users browse
  // older events without being yanked back up.
  useEffect(() => {
    const topId = events[0]?.id ?? null;
    if (topId && topId !== lastTopIdRef.current) {
      scrollerRef.current?.scrollTo({ top: 0, behavior: "smooth" });
      lastTopIdRef.current = topId;
    }
  }, [events]);

  const items = useMemo(() => events, [events]);

  return (
    <section className={`ti-activity-section ${className ?? ""}`}>
      <header className="ti-activity-header">
        <Sparkles size={13} className="ti-activity-header-icon" />
        <h3 className="ti-activity-heading">{title}</h3>
        <span className="ti-activity-count">{items.length}</span>
      </header>
      {items.length === 0 ? (
        <p className="ti-activity-empty">{emptyLabel}</p>
      ) : (
        <ol ref={scrollerRef} className="ti-activity-list" aria-live="polite">
          {items.map((event) => (
            <li
              key={event.id}
              className="ti-activity-row"
              data-state={event.state}
              data-kind={event.kind}
              title={event.detail ? `${event.label} — ${event.detail}` : event.label}
            >
              <span className="ti-activity-bar" aria-hidden="true" />
              <span className="ti-activity-icon" aria-hidden="true">
                <ActivityIcon event={event} />
              </span>
              <span className="ti-activity-label">{event.label}</span>
              {event.detail ? (
                <span className="ti-activity-detail">{event.detail}</span>
              ) : null}
              {event.state === "active" ? (
                <Loader2
                  size={11}
                  className="ti-activity-spinner animate-spin"
                />
              ) : null}
            </li>
          ))}
        </ol>
      )}
    </section>
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
      return event.state === "done" ? (
        <CheckCircle2 size={11} />
      ) : (
        <CircleDot size={11} />
      );
    case "info":
    default:
      return <CircleDot size={11} />;
  }
}
