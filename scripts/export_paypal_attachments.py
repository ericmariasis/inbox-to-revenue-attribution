from __future__ import annotations

import argparse
import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape
import zipfile


DATE_STAMP = "2026-04-17"


@dataclass
class Heading:
    level: int
    text: str


@dataclass
class Paragraph:
    text: str


@dataclass
class Bullet:
    text: str


@dataclass
class Numbered:
    text: str


@dataclass
class CodeBlock:
    lines: list[str]


@dataclass
class Table:
    headers: list[str]
    rows: list[list[str]]


Block = Heading | Paragraph | Bullet | Numbered | CodeBlock | Table


def _split_table_row(line: str) -> list[str]:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return cells


def parse_markdown(text: str) -> list[Block]:
    lines = text.splitlines()
    blocks: list[Block] = []
    i = 0
    in_code = False
    code_lines: list[str] = []

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        if stripped.startswith("```"):
            if in_code:
                blocks.append(CodeBlock(lines=code_lines[:]))
                code_lines.clear()
                in_code = False
            else:
                in_code = True
            i += 1
            continue

        if in_code:
            code_lines.append(raw)
            i += 1
            continue

        if not stripped:
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines):
            maybe_header = _split_table_row(lines[i])
            maybe_sep = lines[i + 1].strip()
            if maybe_sep.startswith("|") and re.fullmatch(r"[|\-\s:]+", maybe_sep):
                rows: list[list[str]] = []
                i += 2
                while i < len(lines) and lines[i].strip().startswith("|"):
                    rows.append(_split_table_row(lines[i]))
                    i += 1
                blocks.append(Table(headers=maybe_header, rows=rows))
                continue

        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            blocks.append(Heading(level=len(heading_match.group(1)), text=heading_match.group(2).strip()))
            i += 1
            continue

        bullet_match = re.match(r"^\s*-\s+(.*)$", raw)
        if bullet_match:
            blocks.append(Bullet(text=bullet_match.group(1).strip()))
            i += 1
            continue

        number_match = re.match(r"^\s*\d+\.\s+(.*)$", raw)
        if number_match:
            blocks.append(Numbered(text=number_match.group(1).strip()))
            i += 1
            continue

        para_lines = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if not nxt:
                break
            if nxt.startswith("|") or nxt.startswith("#") or re.match(r"^\s*[-]\s+", lines[i]) or re.match(
                r"^\s*\d+\.\s+", lines[i]
            ):
                break
            if nxt.startswith("```"):
                break
            para_lines.append(nxt)
            i += 1
        blocks.append(Paragraph(text=" ".join(para_lines)))

    return blocks


def _html_escape(text: str) -> str:
    return html.escape(text, quote=True)


def render_html(title: str, blocks: Iterable[Block]) -> str:
    body: list[str] = []
    open_list: str | None = None

    def close_list() -> None:
        nonlocal open_list
        if open_list:
            body.append(f"</{open_list}>")
            open_list = None

    for block in blocks:
        if isinstance(block, Heading):
            close_list()
            level = min(block.level, 6)
            body.append(f"<h{level}>{_html_escape(block.text)}</h{level}>")
        elif isinstance(block, Paragraph):
            close_list()
            body.append(f"<p>{_html_escape(block.text)}</p>")
        elif isinstance(block, Bullet):
            if open_list != "ul":
                close_list()
                body.append("<ul>")
                open_list = "ul"
            body.append(f"<li>{_html_escape(block.text)}</li>")
        elif isinstance(block, Numbered):
            if open_list != "ol":
                close_list()
                body.append("<ol>")
                open_list = "ol"
            body.append(f"<li>{_html_escape(block.text)}</li>")
        elif isinstance(block, CodeBlock):
            close_list()
            code = "\n".join(_html_escape(line) for line in block.lines)
            body.append(f"<pre><code>{code}</code></pre>")
        elif isinstance(block, Table):
            close_list()
            body.append("<table>")
            body.append("<thead><tr>")
            for header in block.headers:
                body.append(f"<th>{_html_escape(header)}</th>")
            body.append("</tr></thead><tbody>")
            for row in block.rows:
                body.append("<tr>")
                for cell in row:
                    body.append(f"<td>{_html_escape(cell)}</td>")
                body.append("</tr>")
            body.append("</tbody></table>")

    close_list()

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{_html_escape(title)}</title>
  <style>
    body {{
      font-family: Calibri, Arial, sans-serif;
      margin: 40px;
      line-height: 1.45;
      color: #222;
    }}
    h1, h2, h3, h4 {{
      margin-top: 1.4em;
      margin-bottom: 0.5em;
    }}
    p, li {{
      font-size: 11pt;
    }}
    code, pre {{
      font-family: Consolas, "Courier New", monospace;
    }}
    pre {{
      background: #f5f5f5;
      padding: 12px;
      border: 1px solid #ddd;
      white-space: pre-wrap;
    }}
    table {{
      border-collapse: collapse;
      width: 100%;
      margin: 16px 0;
      table-layout: fixed;
    }}
    th, td {{
      border: 1px solid #ccc;
      padding: 8px;
      vertical-align: top;
      text-align: left;
      font-size: 10.5pt;
    }}
    th {{
      background: #f1f1f1;
    }}
  </style>
</head>
<body>
{''.join(body)}
</body>
</html>
"""


def _w_p(text: str, *, bold: bool = False, size_half_points: int | None = None) -> str:
    run_props = ""
    if bold:
        run_props += "<w:b/>"
    if size_half_points is not None:
        run_props += f"<w:sz w:val=\"{size_half_points}\"/><w:szCs w:val=\"{size_half_points}\"/>"
    rpr = f"<w:rPr>{run_props}</w:rPr>" if run_props else ""
    safe = escape(text)
    return (
        "<w:p>"
        "<w:r>"
        f"{rpr}"
        f"<w:t xml:space=\"preserve\">{safe}</w:t>"
        "</w:r>"
        "</w:p>"
    )


def _iter_docx_paragraphs(blocks: Iterable[Block]) -> Iterable[str]:
    for block in blocks:
        if isinstance(block, Heading):
            sizes = {1: 32, 2: 28, 3: 24, 4: 22, 5: 20, 6: 20}
            yield _w_p(block.text, bold=True, size_half_points=sizes.get(block.level, 20))
        elif isinstance(block, Paragraph):
            yield _w_p(block.text)
        elif isinstance(block, Bullet):
            yield _w_p(f"• {block.text}")
        elif isinstance(block, Numbered):
            yield _w_p(block.text)
        elif isinstance(block, CodeBlock):
            for line in block.lines:
                yield _w_p(line if line else " ")
        elif isinstance(block, Table):
            for row in block.rows:
                if row and row[0]:
                    yield _w_p(row[0], bold=True, size_half_points=22)
                for idx, cell in enumerate(row[1:], start=1):
                    if cell:
                        header = block.headers[idx] if idx < len(block.headers) else f"Column {idx + 1}"
                        yield _w_p(f"{header}: {cell}")
                yield _w_p(" ")


def write_docx(title: str, blocks: Iterable[Block], output_path: Path) -> None:
    paragraphs = "".join(_iter_docx_paragraphs(blocks))
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas"
 xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
 xmlns:o="urn:schemas-microsoft-com:office:office"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"
 xmlns:v="urn:schemas-microsoft-com:vml"
 xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
 xmlns:w10="urn:schemas-microsoft-com:office:word"
 xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
 xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"
 xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"
 xmlns:wpi="http://schemas.microsoft.com/office/word/2010/wordprocessingInk"
 xmlns:wne="http://schemas.microsoft.com/office/word/2006/wordml"
 xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
 mc:Ignorable="w14 w15 wp14">
  <w:body>
    {paragraphs}
    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>"""

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

    core_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:dcmitype="http://purl.org/dc/dcmitype/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{escape(title)}</dc:title>
  <dc:creator>Codex</dc:creator>
  <cp:lastModifiedBy>Codex</cp:lastModifiedBy>
</cp:coreProperties>"""

    app_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Codex</Application>
</Properties>"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("docProps/core.xml", core_xml)
        zf.writestr("docProps/app.xml", app_xml)


def export_document(source_path: Path, output_stem: str, output_dir: Path) -> list[Path]:
    markdown = source_path.read_text(encoding="utf-8")
    blocks = parse_markdown(markdown)
    title = source_path.stem.replace("-", " ").title()

    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / f"{output_stem}.html"
    docx_path = output_dir / f"{output_stem}.docx"

    html_path.write_text(render_html(title, blocks), encoding="utf-8")
    write_docx(title, blocks, docx_path)
    return [docx_path, html_path]


def main() -> int:
    parser = argparse.ArgumentParser(description="Export PayPal markdown docs to attachable formats.")
    parser.add_argument(
        "--output-dir",
        default="north-star/paypal/outbound",
        help="Directory for generated attachments.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    exports = [
        (
            Path("north-star/paypal/sandbox-review-packet.md"),
            f"Career Code Pro - Sandbox Review Packet - {DATE_STAMP}",
        ),
        (
            Path("north-star/paypal/sandbox-review-checklist-response.md"),
            f"Career Code Pro - Sandbox Checklist Response - {DATE_STAMP}",
        ),
        (
            Path("north-star/paypal/sandbox-integration-checklist-mapping.md"),
            f"Career Code Pro - Sandbox Checklist Mapping - {DATE_STAMP}",
        ),
    ]

    generated: list[Path] = []
    for source, stem in exports:
        generated.extend(export_document(source, stem, output_dir))

    summary_lines = ["Generated attachments:"]
    summary_lines.extend(f"- {path}" for path in generated)
    (output_dir / "ATTACHMENTS-README.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    for path in generated:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
