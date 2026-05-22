import { useEffect, useState } from "react";
import type { PreviewSlide } from "../../lib/types";

interface SlideVisualProps {
  slide: PreviewSlide;
  className?: string;
}

export function SlideVisual({ slide, className }: SlideVisualProps) {
  const [imageFailed, setImageFailed] = useState(false);
  useEffect(() => {
    setImageFailed(false);
  }, [slide.render_url, slide.index]);

  if (slide.render_url && !imageFailed) {
    return (
      <div className={className}>
        <img src={slide.render_url} alt="" draggable={false} onError={() => setImageFailed(true)} />
      </div>
    );
  }
  return <div className={className} dangerouslySetInnerHTML={{ __html: sanitizeSvgFallback(slide.content) }} />;
}

function sanitizeSvgFallback(svg: string) {
  return (svg ?? "")
    .replace(/<script[\s\S]*?>[\s\S]*?<\/script>/gi, "")
    .replace(/\s(r[xy])="([^"]*\s+[^"]*)"/gi, (_match, attr: string, value: string) => {
      const first = value.trim().split(/\s+/)[0];
      return first ? ` ${attr}="${first}"` : "";
    });
}
