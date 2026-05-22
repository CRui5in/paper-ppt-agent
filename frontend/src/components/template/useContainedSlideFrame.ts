import { useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";

const SLIDE_ASPECT_RATIO = 16 / 9;

interface FrameSize {
  width: number;
  height: number;
}

export function useContainedSlideFrame() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [frameSize, setFrameSize] = useState<FrameSize | null>(null);

  useLayoutEffect(() => {
    const node = containerRef.current;
    if (!node || typeof window === "undefined") return;

    let raf = 0;

    const measure = () => {
      window.cancelAnimationFrame(raf);
      raf = window.requestAnimationFrame(() => {
        const rect = node.getBoundingClientRect();
        const computed = window.getComputedStyle(node);
        const paddingX = parseFloat(computed.paddingLeft || "0") + parseFloat(computed.paddingRight || "0");
        const paddingY = parseFloat(computed.paddingTop || "0") + parseFloat(computed.paddingBottom || "0");
        const availableWidth = Math.max(0, rect.width - paddingX);
        const availableHeight = Math.max(0, rect.height - paddingY);

        if (availableWidth <= 0 || availableHeight <= 0) {
          setFrameSize(null);
          return;
        }

        let width = availableWidth;
        let height = width / SLIDE_ASPECT_RATIO;
        if (height > availableHeight) {
          height = availableHeight;
          width = height * SLIDE_ASPECT_RATIO;
        }

        const next = {
          width: Math.floor(width),
          height: Math.floor(height),
        };
        setFrameSize((prev) => {
          if (prev && Math.abs(prev.width - next.width) < 1 && Math.abs(prev.height - next.height) < 1) {
            return prev;
          }
          return next;
        });
      });
    };

    measure();
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    observer?.observe(node);
    window.addEventListener("resize", measure);

    return () => {
      window.cancelAnimationFrame(raf);
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, []);

  const frameStyle = useMemo<CSSProperties>(
    () =>
      frameSize
        ? { width: frameSize.width, height: frameSize.height }
        : { width: "100%", aspectRatio: "16 / 9" },
    [frameSize],
  );

  return { containerRef, frameStyle };
}
