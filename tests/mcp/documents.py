"""Small real documents for tests, built in memory (no binary files in the repository)."""

from __future__ import annotations

import io
import zipfile

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
)


def make_pdf(*pages: str) -> bytes:
    """A valid PDF with one text line per page (Helvetica, PDF string escaping applied)."""
    objects: list[bytes] = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    font_id = 3 + 2 * len(pages)
    objects.append(b"<</Type/Catalog/Pages 2 0 R>>")
    objects.append(f"<</Type/Pages/Kids[{kids}]/Count {len(pages)}>>".encode())
    for i, text in enumerate(pages):
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 14 Tf 20 100 Td ({escaped}) Tj ET".encode("latin-1")
        objects.append(
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 400 144]/Contents {4 + 2 * i} 0 R/Resources<</Font<</F1 {font_id} 0 R>>>>>>".encode()
        )
        objects.append(b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream")
    objects.append(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def encrypt_pdf(data: bytes, password: str) -> bytes:
    """The same PDF, encrypted with a user password."""
    from pypdf import PdfReader, PdfWriter  # noqa: PLC0415

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(data)))
    writer.encrypt(password, algorithm="RC4-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _zip(members: dict[str, bytes | str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return out.getvalue()


def make_docx(*paragraphs: str, document_xml: str | None = None) -> bytes:
    """A DOCX with one paragraph per argument (runs, a tab and a line break in the first)."""
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    if paragraphs:
        body = (
            f"<w:p><w:r><w:t>{paragraphs[0]}</w:t><w:tab/><w:t>Spalte</w:t><w:br/><w:t>Zeile 2</w:t></w:r></w:p>"
            + body[body.index("</w:p>") + 6 :]
        )
    xml = document_xml or f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>'
    return _zip({"[Content_Types].xml": "<Types/>", "word/document.xml": xml})


def make_xlsx(rows: list[list[str | int]], sheet_name: str = "Tabelle1") -> bytes:
    """An XLSX with shared strings for text cells and numbers as values."""
    shared: list[str] = []
    cells_xml = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for value in row:
            if isinstance(value, str):
                if value not in shared:
                    shared.append(value)
                cells.append(f'<c t="s"><v>{shared.index(value)}</v></c>')
            else:
                cells.append(f"<c><v>{value}</v></c>")
        cells_xml.append(f'<row r="{r}">{"".join(cells)}</row>')
    sheet = f'<worksheet xmlns="{S_NS}"><sheetData>{"".join(cells_xml)}</sheetData></worksheet>'
    strings = f'<sst xmlns="{S_NS}">{"".join(f"<si><t>{s}</t></si>" for s in shared)}</sst>'
    workbook = f'<workbook xmlns="{S_NS}" xmlns:r="{R_NS}"><sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    rels = f'<Relationships xmlns="{PKG_NS}"><Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    return _zip(
        {
            "[Content_Types].xml": "<Types/>",
            "xl/workbook.xml": workbook,
            "xl/_rels/workbook.xml.rels": rels,
            "xl/sharedStrings.xml": strings,
            "xl/worksheets/sheet1.xml": sheet,
        }
    )


def zip_bomb_docx(size: int) -> bytes:
    """A DOCX whose document.xml inflates to ``size`` bytes of whitespace."""
    xml = f'<w:document xmlns:w="{W_NS}"><w:body>' + " " * size + "</w:body></w:document>"
    return _zip({"word/document.xml": xml})
