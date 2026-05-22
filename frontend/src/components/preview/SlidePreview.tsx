import type { PreviewSlide } from "../../lib/types";
import { useLocale } from "../../i18n";
import { SlideVisual } from "./SlideVisual";

interface SlidePreviewProps {
  slides: PreviewSlide[];
  selectedSlide?: PreviewSlide;
  onSelect: (slide: PreviewSlide) => void;
}

export function SlidePreview({ slides, selectedSlide, onSelect }: SlidePreviewProps) {
  const { t } = useLocale();
  return (
    <section className="panel slide-preview-panel">
      <div className="panel-header-row">
        <div>
          <p className="panel-title">{t("preview.title")}</p>
          <p className="muted-copy">{t("preview.body")}</p>
          <p className="panel-support-text">
            {slides.length > 0 ? `${slides.length} ${t("preview.slides")}` : t("preview.emptyState")}
          </p>
        </div>
      </div>
      <div className="thumbnail-grid">
        {slides.map((slide) => (
          <button
            key={slide.index}
            type="button"
            className={`thumbnail-card ${selectedSlide?.index === slide.index ? "thumbnail-card-active" : ""}`}
            onClick={() => onSelect(slide)}
            title={`PPT ${slide.index}`}
          >
            <SlideVisual slide={slide} className="thumbnail-svg" />
            <div className="thumbnail-caption">
              <strong>{`PPT ${slide.index}`}</strong>
              <span>{slide.name}</span>
            </div>
          </button>
        ))}
      </div>
    </section>
  );
}
