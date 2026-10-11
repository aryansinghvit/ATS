"""Markdown -> HTML for job descriptions shown on public career pages.

Raw HTML in the source is escaped (``html: False``) and markdown-it's link validation rejects
``javascript:``/``vbscript:``/``data:`` URLs, so the output is safe to embed without further
sanitising.
"""

from markdown_it import MarkdownIt

_renderer = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


def render_markdown(source: str) -> str:
    return _renderer.render(source)
