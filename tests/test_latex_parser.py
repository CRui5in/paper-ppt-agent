from pathlib import Path

from backend.parser.latex_parser import LaTeXParser


def test_basic_latex_fallback_preserves_chapter_headings_and_body() -> None:
    parser = LaTeXParser()
    content = r"""
\documentclass{xduugthesis}
\begin{document}
\chapter{绪论}
这里是第一章正文，包含控制系统背景。
\section{研究问题}
这里是研究问题正文。
\subsection{贡献}
这里是贡献正文。
\bibliography{ref}
\end{document}
"""

    markdown = parser._basic_latex_to_markdown(content)
    sections = parser._parse_sections(markdown, [])

    assert [section.title for section in sections] == ["绪论", "研究问题", "贡献"]
    assert sections[0].content == "这里是第一章正文，包含控制系统背景。"
    assert sections[1].content == "这里是研究问题正文。"


def test_resolve_includes_uses_current_file_dir_and_avoids_recursive_bootstrap(
    tmp_path: Path,
) -> None:
    parser = LaTeXParser()
    main = tmp_path / "main.tex"
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    intro = chapters / "intro.tex"

    main.write_text(
        r"""
\documentclass{xduugthesis}
\begin{document}
\include{chapters/intro}
\end{document}
""",
        encoding="utf-8",
    )
    intro.write_text(
        r"""
\input{../main.tex}
\chapter{绪论}
章节正文应该只出现一次。
""",
        encoding="utf-8",
    )

    resolved = parser._resolve_includes(main, tmp_path)

    assert resolved.count("章节正文应该只出现一次。") == 1
    assert r"\input{../main.tex}" not in resolved


def test_extract_title_from_xdusetup_info_block() -> None:
    parser = LaTeXParser()
    content = r"""
\documentclass{xduugthesis}
\xdusetup{
  info = {
    title = {面向复杂系统的动力学控制研究},
    author = {测试作者}
  }
}
\begin{document}
\end{document}
"""

    assert parser._extract_title(content) == "面向复杂系统的动力学控制研究"
