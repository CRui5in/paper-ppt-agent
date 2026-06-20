"""Paper / source parsing module supporting PDF, LaTeX, text, markdown, URL."""

from .paper_model import ParsedPaper, PaperFigure, PaperSection, PaperTable
from .source_model import Source, SourceGroup, SourceKind

def __getattr__(name: str):
    if name == "PDFParser":
        from .pdf_parser import PDFParser as _PDFParser

        return _PDFParser
    if name == "LaTeXParser":
        from .latex_parser import LaTeXParser as _LaTeXParser

        return _LaTeXParser
    if name == "TextParser":
        from .text_parser import TextParser as _TextParser

        return _TextParser
    if name == "MarkdownParser":
        from .markdown_parser import MarkdownParser as _MarkdownParser

        return _MarkdownParser
    if name == "UrlParser":
        from .url_parser import UrlParser as _UrlParser

        return _UrlParser
    raise AttributeError(name)

__all__ = [
    "PDFParser",
    "LaTeXParser",
    "TextParser",
    "MarkdownParser",
    "UrlParser",
    "ParsedPaper",
    "PaperFigure",
    "PaperSection",
    "PaperTable",
    "Source",
    "SourceGroup",
    "SourceKind",
]
