"""Durable boundaries for provider generation stages and individual SVG pages."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from backend.parser.paper_model import ParsedPaper, PaperFigure, PaperSection, PaperTable
from backend.runtime.io import _write_text_atomic


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GenerationCheckpoints:
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.directory = project_dir / "checkpoints"

    def read(self, name: str) -> dict | None:
        path = self.directory / f"{name}.json"
        if not path.exists():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("version") != 1:
            raise ValueError(f"Unsupported generation checkpoint: {name}")
        for relative, expected in record.get("artifacts", {}).items():
            artifact = self.project_dir / relative
            if not artifact.is_file() or digest(artifact) != expected:
                raise ValueError(f"Generation checkpoint artifact changed or is missing: {relative}")
        return record["data"]

    def write(self, name: str, data: dict, artifacts: list[Path] = ()) -> None:
        record = {
            "version": 1,
            "data": data,
            "artifacts": {str(path.relative_to(self.project_dir)): digest(path) for path in artifacts},
        }
        _write_text_atomic(self.directory / f"{name}.json", json.dumps(record, ensure_ascii=False), "utf-8")

    def save_paper(self, paper: ParsedPaper) -> None:
        data = asdict(paper)
        assets = [figure.path for figure in paper.all_figures() if figure.available]
        # Paths in serialized paper records are restored explicitly on load.
        encoded = json.loads(json.dumps(data, default=str))
        self.write("parsing", encoded, [path for path in assets if path.is_relative_to(self.project_dir)])

    def load_paper(self) -> ParsedPaper | None:
        data = self.read("parsing")
        if data is None:
            return None
        data = dict(data)
        sections = []
        for raw in data.pop("sections", []):
            section = dict(raw)
            section["figures"] = [PaperFigure(**{**fig, "path": Path(fig["path"])}) for fig in section.get("figures", [])]
            section["tables"] = [PaperTable(**table) for table in section.get("tables", [])]
            sections.append(PaperSection(**section))
        if data.get("figures_dir"):
            data["figures_dir"] = Path(data["figures_dir"])
        return ParsedPaper(**data, sections=sections)

    def read_text(self, name: str, filename: str, *, legacy: bool = False) -> str | None:
        record = self.read(name)
        path = self.project_dir / filename
        if record is None and not (legacy and path.is_file()):
            return None
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            raise ValueError(f"Empty generation artifact: {filename}")
        return content

    def saved_pages(self) -> dict[int, str]:
        pages = {}
        for path in sorted(self.directory.glob("page_*.json")):
            data = self.read(path.stem)
            pages[int(data["page"])] = (self.project_dir / data["path"]).read_text(encoding="utf-8")
        return pages

    def save_page(self, page: int, svg_path: Path) -> None:
        self.write(f"page_{page:03d}", {"page": page, "path": str(svg_path.relative_to(self.project_dir))}, [svg_path])
