r"""LaTeX paper parser with \input resolution and pandoc conversion.

Supports single .tex files and multi-file LaTeX projects (.zip and .tar.gz archives).
Uses pandoc for LaTeX→Markdown conversion with custom pre-processing
for \input/\include resolution and bibliography extraction.
"""

from __future__ import annotations

import logging
import re
import shutil
import tarfile
import zipfile
from pathlib import Path

from backend.config import settings
from backend.runtime import aoffload, arun
from backend.runtime.subproc import SubprocessError, SubprocessTimeout

from .base import PaperParser
from .paper_model import PaperFigure, PaperSection, ParsedPaper

logger = logging.getLogger(__name__)


def _probe_image_size(path: Path) -> tuple[int, int]:
    """Best-effort PIL probe; returns (0, 0) when unreadable."""
    try:
        from PIL import Image

        with Image.open(path) as img:
            return int(img.width), int(img.height)
    except Exception:
        return 0, 0


class LaTeXParser(PaperParser):
    """Parse LaTeX source files into structured paper data."""

    async def parse(self, file_path: Path, output_dir: Path) -> ParsedPaper:
        output_dir.mkdir(parents=True, exist_ok=True)
        images_dir = output_dir / "images"
        images_dir.mkdir(exist_ok=True)

        lower_name = file_path.name.lower()

        # Handle archives
        if file_path.suffix.lower() == ".zip":
            return await self._parse_zip(file_path, output_dir, images_dir)
        if lower_name.endswith(".tar.gz") or file_path.suffix.lower() == ".tgz":
            return await self._parse_tar(file_path, output_dir, images_dir)

        # Single .tex file
        return await self._parse_tex(file_path, output_dir, images_dir)

    async def _parse_zip(
        self, zip_path: Path, output_dir: Path, images_dir: Path
    ) -> ParsedPaper:
        """Extract ZIP and find the main .tex file."""
        extract_dir = output_dir / "latex_src"
        extract_dir.mkdir(exist_ok=True)

        # Decompression is fully synchronous and can take seconds on a large
        # arXiv tarball — run it on the offload pool so the event loop is
        # free to keep serving other requests.
        await aoffload(self._extract_zip_to_dir, zip_path, extract_dir)

        main_tex = await aoffload(self._find_main_tex, extract_dir)
        if not main_tex:
            raise ValueError(f"No main .tex file found in {zip_path}")

        return await self._parse_tex(main_tex, output_dir, images_dir)

    async def _parse_tar(
        self, tar_path: Path, output_dir: Path, images_dir: Path
    ) -> ParsedPaper:
        """Extract TAR/TAR.GZ and find the main .tex file."""
        extract_dir = output_dir / "latex_src"
        extract_dir.mkdir(exist_ok=True)

        await aoffload(self._extract_tar_to_dir, tar_path, extract_dir)

        main_tex = await aoffload(self._find_main_tex, extract_dir)
        if not main_tex:
            raise ValueError(f"No main .tex file found in {tar_path}")

        return await self._parse_tex(main_tex, output_dir, images_dir)

    def _extract_zip_to_dir(self, zip_path: Path, extract_dir: Path) -> None:
        with zipfile.ZipFile(zip_path, "r") as zf:
            self._extract_zip_safely(zf, extract_dir)

    def _extract_tar_to_dir(self, tar_path: Path, extract_dir: Path) -> None:
        with tarfile.open(tar_path, "r:*") as tf:
            self._extract_tar_safely(tf, extract_dir)

    def _find_main_tex(self, directory: Path) -> Path | None:
        """Find the main .tex file by looking for \\documentclass."""
        for tex_file in directory.rglob("*.tex"):
            try:
                content = tex_file.read_text(encoding="utf-8", errors="ignore")
                if r"\documentclass" in content or r"\begin{document}" in content:
                    return tex_file
            except Exception:
                continue
        # Fallback: return first .tex file
        tex_files = list(directory.rglob("*.tex"))
        return tex_files[0] if tex_files else None

    async def _parse_tex(
        self, tex_path: Path, output_dir: Path, images_dir: Path
    ) -> ParsedPaper:
        r"""Parse a single .tex file with \input resolution."""
        tex_dir = tex_path.parent

        # The metadata / figure / section extraction is all synchronous regex
        # work — push it to the offload pool. Pandoc invocation is split out
        # so it can run as an async subprocess (real OS-level parallelism).
        resolved_content = await aoffload(self._resolve_includes, tex_path, tex_dir)

        title = await aoffload(self._extract_title, resolved_content)
        authors = await aoffload(self._extract_authors, resolved_content)
        abstract = await aoffload(self._extract_abstract, resolved_content)
        figures = await aoffload(
            self._extract_figures, resolved_content, tex_dir, images_dir
        )

        markdown = await self._convert_to_markdown_async(
            resolved_content, tex_dir, output_dir
        )

        sections = await aoffload(self._parse_sections, markdown, figures)
        references = await aoffload(self._extract_references, resolved_content, tex_dir)

        return ParsedPaper(
            title=title,
            authors=authors,
            abstract=abstract,
            sections=sections,
            references=references,
            source_type="latex",
            figures_dir=images_dir,
        )

    def _resolve_includes(
        self,
        tex_path: Path,
        base_dir: Path,
        depth: int = 0,
        visited: set[Path] | None = None,
        root_path: Path | None = None,
    ) -> str:
        r"""Recursively resolve \input{} and \include{} directives."""
        if depth > 20:  # Prevent infinite recursion
            return ""

        visited = visited or set()
        try:
            resolved_tex_path = tex_path.resolve()
            project_root = base_dir.resolve()
        except OSError:
            return ""

        if resolved_tex_path in visited:
            return ""
        visited.add(resolved_tex_path)
        root_path = root_path or resolved_tex_path

        try:
            content = resolved_tex_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""

        content = self._strip_latex_comments(content)
        if resolved_tex_path != root_path:
            body = self._extract_document_body(content)
            if body:
                content = body

        def replace_include(match: re.Match) -> str:
            raw_filename = match.group(1).strip()
            if not raw_filename or raw_filename.startswith("\\"):
                return match.group(0)

            filename = raw_filename if raw_filename.endswith(".tex") else f"{raw_filename}.tex"
            search_dirs = [resolved_tex_path.parent, project_root]
            for search_dir in search_dirs:
                included_path = (search_dir / filename).resolve()
                try:
                    included_path.relative_to(project_root)
                except ValueError:
                    continue
                if included_path.exists():
                    return self._resolve_includes(
                        included_path,
                        project_root,
                        depth + 1,
                        visited,
                        root_path,
                    )
            return match.group(0)  # Keep original if file not found

        # Resolve \input{...} and \include{...}
        content = re.sub(r"\\(?:input|include)\s*\{([^}]+)\}", replace_include, content)

        return content

    def _extract_title(self, content: str) -> str:
        title = self._extract_command_argument(content, "title")
        if title:
            return self._clean_latex(title)

        setup_title = self._extract_setup_title(content)
        if setup_title:
            return self._clean_latex(setup_title)

        return "Untitled"

    def _extract_authors(self, content: str) -> list[str]:
        author_arg = self._extract_command_argument(content, "author")
        if not author_arg:
            return []
        raw = self._clean_latex(author_arg)
        # Split on \and, commas, or newlines
        parts = re.split(r"\\and|,|\n", raw)
        return [a.strip() for a in parts if a.strip()]

    def _extract_abstract(self, content: str) -> str:
        match = re.search(
            r"\\begin\{abstract\}(.+?)\\end\{abstract\}", content, re.DOTALL
        )
        if match:
            return self._clean_latex(match.group(1)).strip()
        return ""

    def _extract_figures(
        self, content: str, tex_dir: Path, images_dir: Path
    ) -> list[PaperFigure]:
        """Extract figure references and copy image files.

        When a referenced image cannot be located on disk the figure is still
        recorded with ``available=False`` so the research agent receives the
        caption context but ``to_markdown()`` suppresses the broken reference.
        """
        figures: list[PaperFigure] = []
        fig_pattern = re.compile(
            r"\\begin\{figure\*?\}.*?"
            r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}"
            r".*?"
            r"(?:\\caption\{([^}]*)\})?"
            r".*?"
            r"(?:\\label\{([^}]*)\})?"
            r".*?"
            r"\\end\{figure\*?\}",
            re.DOTALL,
        )
        for match in fig_pattern.finditer(content):
            img_path_str = match.group(1).strip()
            caption = self._clean_latex(match.group(2) or "")
            label = match.group(3)

            img_src = self._resolve_image_path(img_path_str, tex_dir)
            if img_src and img_src.exists():
                # Verify the format is usable in PPT (skip .eps without conversion)
                if img_src.suffix.lower() in {".eps", ".ps"}:
                    # Record as unavailable — we can't embed EPS directly
                    figures.append(
                        PaperFigure(
                            path=img_src,
                            caption=caption,
                            label=label,
                            available=False,
                        )
                    )
                    continue

                dest = images_dir / img_src.name
                try:
                    shutil.copy2(img_src, dest)
                except OSError:
                    figures.append(
                        PaperFigure(
                            path=img_src,
                            caption=caption,
                            label=label,
                            available=False,
                        )
                    )
                    continue
                w, h = _probe_image_size(dest)
                figures.append(
                    PaperFigure(
                        path=dest,
                        caption=caption,
                        label=label,
                        available=True,
                        natural_width=w,
                        natural_height=h,
                    )
                )
            else:
                # Image referenced in LaTeX but not present on disk (e.g. single
                # .tex upload without the Figure/ directory).  Keep the caption
                # for the research agent but mark as unavailable so no broken
                # <image> reference is emitted in the manuscript.
                stub_path = images_dir / (Path(img_path_str).name or "unknown")
                figures.append(
                    PaperFigure(
                        path=stub_path,
                        caption=caption,
                        label=label,
                        available=False,
                    )
                )

        return figures

    def _resolve_image_path(self, img_path: str, base_dir: Path) -> Path | None:
        """Resolve a LaTeX image path to an actual file."""
        candidate = base_dir / img_path
        if candidate.exists():
            return candidate

        # Try common extensions
        for ext in [".png", ".jpg", ".jpeg", ".pdf", ".eps", ".svg"]:
            with_ext = base_dir / (img_path + ext)
            if with_ext.exists():
                return with_ext

        # Search subdirectories
        for ext in ["*.png", "*.jpg", "*.jpeg", "*.pdf"]:
            matches = list(base_dir.rglob(Path(img_path).name + "*"))
            if matches:
                return matches[0]

        return None

    def _extract_zip_safely(self, archive: zipfile.ZipFile, extract_dir: Path) -> None:
        """Extract only regular files and rebuild directories ourselves."""
        for info in archive.infolist():
            if info.is_dir():
                continue
            target = self._safe_extract_target(extract_dir, info.filename)
            if target is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)

    def _extract_tar_safely(self, archive: tarfile.TarFile, extract_dir: Path) -> None:
        """Extract only regular files and avoid symlink/device metadata."""
        for member in archive.getmembers():
            if not member.isfile():
                continue
            target = self._safe_extract_target(extract_dir, member.name)
            if target is None:
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with extracted, target.open("wb") as dst:
                shutil.copyfileobj(extracted, dst)

    def _safe_extract_target(self, extract_dir: Path, raw_name: str) -> Path | None:
        try:
            target = (extract_dir / raw_name).resolve()
        except OSError:
            return None
        try:
            target.relative_to(extract_dir.resolve())
        except ValueError:
            return None
        return target

    async def _convert_to_markdown_async(
        self, content: str, tex_dir: Path, output_dir: Path
    ) -> str:
        """Convert LaTeX content to Markdown using pandoc (async subprocess).

        Falls back to a regex-based stub when pandoc is not on PATH or when
        it errors out. Timeout / non-zero exit are handled separately so the
        caller can still surface the failure mode in logs.
        """
        if not shutil.which("pandoc"):
            return await aoffload(self._basic_latex_to_markdown, content)

        tmp_tex = output_dir / "_resolved.tex"
        await aoffload(tmp_tex.write_text, content, encoding="utf-8")

        try:
            cp = await arun(
                [
                    "pandoc",
                    "-f", "latex",
                    "-t", "gfm",
                    "--wrap=none",
                    str(tmp_tex),
                ],
                timeout=float(settings.pandoc_timeout),
                cwd=tex_dir,
                check=False,
            )
            if cp.returncode == 0:
                return cp.stdout
            logger.warning(
                "pandoc latex→md failed (rc=%d): %s",
                cp.returncode,
                cp.stderr.strip()[:300],
            )
        except SubprocessTimeout:
            logger.warning("pandoc latex→md timed out after %ss", settings.pandoc_timeout)
        except SubprocessError as exc:
            logger.warning("pandoc latex→md error: %s", exc)
        except Exception:
            logger.exception("pandoc latex→md unexpected error")
        finally:
            await aoffload(tmp_tex.unlink, missing_ok=True)

        return await aoffload(self._basic_latex_to_markdown, content)

    def _basic_latex_to_markdown(self, content: str) -> str:
        """Basic LaTeX to Markdown conversion without pandoc."""
        # Extract document body
        body = self._extract_document_body(content)
        if body:
            content = body

        content = self._strip_latex_comments(content)

        # Convert sections
        heading_levels = {
            "part": "##",
            "chapter": "##",
            "section": "##",
            "subsection": "###",
            "subsubsection": "####",
        }

        def replace_heading(match: re.Match) -> str:
            command = match.group(1)
            title = self._clean_latex(match.group(2))
            return f"\n{heading_levels[command]} {title}\n\n"

        content = re.sub(
            r"\\(part|chapter|section|subsection|subsubsection)\*?\s*(?:\[[^\]]*\])?\{([^{}]+)\}",
            replace_heading,
            content,
        )

        # Convert emphasis
        content = re.sub(r"\\textbf\{([^}]+)\}", r"**\1**", content)
        content = re.sub(r"\\textit\{([^}]+)\}", r"*\1*", content)
        content = re.sub(r"\\emph\{([^}]+)\}", r"*\1*", content)

        # Convert lists
        content = re.sub(r"\\begin\{itemize\}", "", content)
        content = re.sub(r"\\end\{itemize\}", "", content)
        content = re.sub(r"\\begin\{enumerate\}", "", content)
        content = re.sub(r"\\end\{enumerate\}", "", content)
        content = re.sub(r"\\item\s*", "- ", content)

        # Remove common commands
        content = re.sub(r"\\maketitle", "", content)
        content = re.sub(r"\\begin\{abstract\}.*?\\end\{abstract\}", "", content, flags=re.DOTALL)
        content = re.sub(r"\\bibliographystyle\{[^}]+\}", "", content)
        content = re.sub(r"\\bibliography\{[^}]+\}", "", content)

        return self._clean_latex_block(content)

    def _parse_sections(
        self, markdown: str, figures: list[PaperFigure]
    ) -> list[PaperSection]:
        """Parse Markdown into structured sections."""
        sections: list[PaperSection] = []
        current: PaperSection | None = None
        content_lines: list[str] = []

        for line in markdown.split("\n"):
            heading_match = re.match(r"^(#{2,4})\s+(.+)$", line)
            if heading_match:
                if current:
                    current.content = "\n".join(content_lines).strip()
                    sections.append(current)
                    content_lines = []

                level = len(heading_match.group(1)) - 1  # ##=1, ###=2, ####=3
                current = PaperSection(
                    title=heading_match.group(2),
                    level=level,
                    content="",
                )
            else:
                content_lines.append(line)

        if current:
            current.content = "\n".join(content_lines).strip()
            sections.append(current)

        # Distribute figures across sections (best effort)
        if figures and sections:
            per_section = max(1, len(figures) // len(sections))
            fig_idx = 0
            for section in sections:
                end = min(fig_idx + per_section, len(figures))
                section.figures = figures[fig_idx:end]
                fig_idx = end

        return sections

    def _extract_references(self, content: str, tex_dir: Path) -> list[str]:
        """Extract bibliography references."""
        refs: list[str] = []

        # Try to find .bib file
        bib_match = re.search(r"\\bibliography\{([^}]+)\}", content)
        if not bib_match:
            bib_match = re.search(r"\\addbibresource\{([^}]+)\}", content)

        if bib_match:
            bib_name = bib_match.group(1)
            if not bib_name.endswith(".bib"):
                bib_name += ".bib"
            bib_path = tex_dir / bib_name
            if bib_path.exists():
                refs = self._parse_bib_file(bib_path)

        # Also extract inline \bibitem entries
        for match in re.finditer(r"\\bibitem\{[^}]*\}\s*(.+?)(?=\\bibitem|\Z)", content, re.DOTALL):
            ref_text = self._clean_latex(match.group(1)).strip()
            if ref_text:
                refs.append(ref_text)

        return refs

    def _parse_bib_file(self, bib_path: Path) -> list[str]:
        """Simple .bib file parser extracting titles."""
        refs = []
        try:
            bib_content = bib_path.read_text(encoding="utf-8", errors="ignore")
            for match in re.finditer(r"title\s*=\s*\{([^}]+)\}", bib_content):
                refs.append(match.group(1).strip())
        except Exception:
            pass
        return refs

    def _strip_latex_comments(self, text: str) -> str:
        """Remove unescaped LaTeX comments while preserving line structure."""
        lines: list[str] = []
        for line in text.splitlines():
            cut_at: int | None = None
            for idx, char in enumerate(line):
                if char != "%":
                    continue
                slash_count = 0
                cursor = idx - 1
                while cursor >= 0 and line[cursor] == "\\":
                    slash_count += 1
                    cursor -= 1
                if slash_count % 2 == 0:
                    cut_at = idx
                    break
            lines.append(line[:cut_at] if cut_at is not None else line)
        return "\n".join(lines)

    def _extract_document_body(self, content: str) -> str:
        match = re.search(
            r"\\begin\{document\}(.+?)\\end\{document\}", content, re.DOTALL
        )
        return match.group(1) if match else ""

    def _extract_command_argument(self, content: str, command: str) -> str:
        pattern = re.compile(rf"\\{re.escape(command)}\*?\s*(?:\[[^\]]*\]\s*)?\{{")
        match = pattern.search(content)
        if not match:
            return ""
        return self._read_braced_content(content, match.end() - 1)

    def _extract_setup_title(self, content: str) -> str:
        for match in re.finditer(r"\\[a-zA-Z@]*setup\s*\{", content):
            setup_body = self._read_braced_content(content, match.end() - 1)
            title = self._extract_keyed_braced_value(setup_body, "title")
            if title:
                return title
        return ""

    def _extract_keyed_braced_value(self, content: str, key: str) -> str:
        for match in re.finditer(rf"\b{re.escape(key)}\s*=", content):
            cursor = match.end()
            while cursor < len(content) and content[cursor].isspace():
                cursor += 1
            if cursor < len(content) and content[cursor] == "{":
                return self._read_braced_content(content, cursor)
        return ""

    def _read_braced_content(self, text: str, open_brace_index: int) -> str:
        if open_brace_index >= len(text) or text[open_brace_index] != "{":
            return ""
        depth = 0
        start = open_brace_index + 1
        for idx in range(open_brace_index, len(text)):
            char = text[idx]
            escaped = idx > 0 and text[idx - 1] == "\\"
            if escaped:
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start:idx]
        return ""

    def _clean_latex_block(self, text: str) -> str:
        """Clean LaTeX text while preserving line breaks for Markdown headings."""
        cleaned_lines: list[str] = []
        previous_blank = False
        for raw_line in text.splitlines():
            cleaned = self._clean_latex(raw_line)
            if cleaned:
                cleaned_lines.append(cleaned)
                previous_blank = False
            elif not previous_blank and cleaned_lines:
                cleaned_lines.append("")
                previous_blank = True
        return "\n".join(cleaned_lines).strip()

    def _clean_latex(self, text: str) -> str:
        """Remove common LaTeX commands from text."""
        text = re.sub(r"~", " ", text)
        text = re.sub(r"\\(?:cite|citep|citet|ref|eqref|label)\*?(?:\[[^\]]*\])?\{[^}]*\}", "", text)
        text = re.sub(r"\\(?:begin|end)\{[^}]+\}", "", text)
        text = re.sub(r"\\[a-zA-Z]+\{([^}]*)\}", r"\1", text)
        text = re.sub(r"\\[a-zA-Z]+\[[^\]]*\]", "", text)
        text = re.sub(r"\\[a-zA-Z]+", "", text)
        text = re.sub(r"\\([{}%&_#])", r"\1", text)
        text = re.sub(r"[{}]", "", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
