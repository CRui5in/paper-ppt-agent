import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

/** Lightweight, dependency-free hover tooltip.
 *
 * Why not the native `title` attribute?
 *  - Native tooltips are uncontrollable: ~500ms hover delay, no styling, no
 *    multi-line wrapping, ignored on keyboard focus.
 *  - We want a consistent floating bubble that matches the dark UI and can
 *    show full text for any truncated label in the progress monitor.
 *
 * The tooltip is rendered into a portal anchored to `document.body` so it
 * never gets clipped by overflow:hidden parents (a common issue inside
 * panels with scroll bars or rounded corners).
 *
 * If `disabled` is true, or if the children's measured text fully fits on
 * one line, the tooltip never shows — there's nothing to reveal.
 */
interface HoverTooltipProps {
  /** Full text to reveal on hover. */
  content: string;
  /** Wrapper class — typically the `*-text` element you want to truncate. */
  className?: string;
  /** When true, never show the tooltip (e.g. content is empty). */
  disabled?: boolean;
  children: ReactNode;
}

const SHOW_DELAY_MS = 120;
const MARGIN = 8;
const MAX_WIDTH = 320;

export function HoverTooltip({ content, className, disabled, children }: HoverTooltipProps) {
  const wrapRef = useRef<HTMLSpanElement>(null);
  const timerRef = useRef<number | null>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  const computePosition = useCallback(() => {
    const node = wrapRef.current;
    if (!node) return;
    const rect = node.getBoundingClientRect();
    const viewportW = window.innerWidth;
    const viewportH = window.innerHeight;

    // Default: above the element, horizontally centered.
    let top = rect.top - MARGIN;
    let left = rect.left + rect.width / 2;

    // If we're too close to the top edge, flip below.
    if (top < 60) {
      top = rect.bottom + MARGIN;
    }
    // Clamp horizontally so the tooltip can't run off-screen.
    const halfMax = Math.min(MAX_WIDTH, viewportW - 24) / 2;
    left = Math.max(halfMax + 12, Math.min(viewportW - halfMax - 12, left));

    // Final vertical clamp so we never render below the viewport either.
    if (top > viewportH - 12) {
      top = viewportH - 12;
    }

    setPos({ top, left });
  }, []);

  const scheduleOpen = useCallback(() => {
    if (disabled || !content) return;
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
    }
    timerRef.current = window.setTimeout(() => {
      computePosition();
      setOpen(true);
    }, SHOW_DELAY_MS);
  }, [disabled, content, computePosition]);

  const close = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    setOpen(false);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onScroll = () => close();
    window.addEventListener("scroll", onScroll, { passive: true, capture: true });
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [open, close]);

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
      }
    };
  }, []);

  return (
    <>
      <span
        ref={wrapRef}
        className={className}
        onMouseEnter={scheduleOpen}
        onMouseLeave={close}
        onFocus={scheduleOpen}
        onBlur={close}
      >
        {children}
      </span>
      {open && pos
        ? createPortal(
            <div
              role="tooltip"
              className="hover-tooltip"
              style={{ top: pos.top, left: pos.left, maxWidth: MAX_WIDTH }}
            >
              {content}
            </div>,
            document.body,
          )
        : null}
    </>
  );
}
