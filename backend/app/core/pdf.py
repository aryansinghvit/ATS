"""Dependency-free PDF 1.4 writer for simple text documents (offer letters).

Produces A4 pages in Helvetica with word-wrapping. Only the Latin-1 subset is supported; other
characters are replaced so the document always renders.
"""

from __future__ import annotations

import textwrap

PAGE_WIDTH, PAGE_HEIGHT = 595, 842  # A4 in points
MARGIN, LEADING, WRAP = 64, 16, 88


def _escape(text: str) -> str:
    safe = text.encode("latin-1", errors="replace").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _lines(paragraphs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(style, text) paragraphs -> wrapped (style, line) list; '' text = blank line."""
    lines: list[tuple[str, str]] = []
    for style, text in paragraphs:
        if not text:
            lines.append((style, ""))
            continue
        width = WRAP if style == "body" else 60
        lines.extend((style, line) for line in textwrap.wrap(text, width) or [""])
    return lines


def render_text_pdf(paragraphs: list[tuple[str, str]], *, title: str) -> bytes:
    """``paragraphs`` items are (style, text) with style in {"title", "heading", "body"}."""
    per_page = (PAGE_HEIGHT - 2 * MARGIN) // LEADING
    lines = _lines(paragraphs)
    pages = [lines[i : i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    sizes = {"title": 18, "heading": 12, "body": 11}
    fonts = {"title": "/F2", "heading": "/F2", "body": "/F1"}

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # placeholder, filled once page ids are known
    pages_id = add(b"")
    regular = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    bold = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    )
    page_ids = []
    for page in pages:
        commands = ["BT"]
        y = PAGE_HEIGHT - MARGIN
        for style, text in page:
            commands.append(
                f"{fonts[style]} {sizes[style]} Tf 1 0 0 1 {MARGIN} {y} Tm ({_escape(text)}) Tj"
            )
            y -= LEADING + (6 if style == "title" else 0)
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(
            add(
                (
                    f"<< /Type /Page /Parent {pages_id} 0 R "
                    f"/MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                    f"/Resources << /Font << /F1 {regular} 0 R /F2 {bold} 0 R >> >> "
                    f"/Contents {content} 0 R >>"
                ).encode()
            )
        )
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
    objects[pages_id - 1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] "
        f"/Count {len(page_ids)} >>"
    ).encode()
    info = add(f"<< /Title ({_escape(title)}) /Producer (ATS Platform) >>".encode("latin-1"))

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    output += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(output)
