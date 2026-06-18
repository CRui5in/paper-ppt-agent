import { useRef, useState } from "react";
import { UploadCloud } from "lucide-react";
import { useLocale } from "../../i18n";

interface UploadZoneProps {
  /** Single-file callback (legacy). Still fired when provided. */
  onFileSelect?: (file: File) => void;
  /** Multi-file callback (Sources panel). Preferred when present. */
  onFilesSelect?: (files: File[]) => void;
  /** Allowed extensions in the file picker. */
  accept?: string;
  /** Override the body copy under the title. */
  bodyKey?: string;
}

export function UploadZone({
  onFileSelect,
  onFilesSelect,
  accept = ".pdf,.tex,.zip,.tgz,.tar.gz,.md,.markdown,.txt",
  bodyKey = "upload.body",
}: UploadZoneProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const { t } = useLocale();
  const [isDragging, setIsDragging] = useState(false);

  const dispatch = (files: File[] | FileList | null | undefined) => {
    if (!files || files.length === 0) return;
    const list = Array.from(files);
    if (onFilesSelect) {
      onFilesSelect(list);
    } else if (onFileSelect) {
      // Legacy single-file behavior: only the first file.
      onFileSelect(list[0]);
    }
  };

  return (
    <section className="panel panel-emphasis">
      <div
        className={`upload-zone ${isDragging ? "upload-zone-dragging" : ""}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setIsDragging(true);
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setIsDragging(false);
          dispatch(e.dataTransfer.files);
        }}
        role="button"
        tabIndex={0}
      >
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "0.75rem", padding: "1rem 0" }}>
          <UploadCloud size={40} color="var(--accent)" strokeWidth={1.5} />
          <div className="upload-copy" style={{ textAlign: "center" }}>
            <p className="panel-title">{t("upload.title")}</p>
            <p className="muted-copy">{t(bodyKey)}</p>
          </div>
        </div>
      </div>
      <input
        ref={inputRef}
        type="file"
        accept={accept}
        multiple={Boolean(onFilesSelect)}
        className="hidden-input"
        onChange={(e) => {
          dispatch(e.target.files);
          e.currentTarget.value = "";
        }}
      />
    </section>
  );
}
