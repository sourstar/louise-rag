"""Minimal PDFs written by hand, so the tests depend on no PDF writer: one
Helvetica text line per page, an optional /Info dictionary, and a page
with no text at all standing in for a scan."""


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[str | None], info: dict[str, str] | None = None) -> bytes:
    """`pages`: the text on each page; None for a page with no text."""
    objs: list[bytes] = []
    n_pages = len(pages)
    # 1 catalog, 2 pages, 3 font, then (page, content) pairs, then info
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        content = (b"" if text is None else
                   f"BT /F1 12 Tf 72 720 Td ({_esc(text)}) Tj ET".encode("latin-1"))
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {5 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content
                    + b"\nendstream")
    info_ref = ""
    if info:
        body = " ".join(f"/{k} ({_esc(v)})" for k, v in info.items())
        objs.append(f"<< {body} >>".encode("latin-1"))
        info_ref = f" /Info {len(objs)} 0 R"
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R{info_ref} >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)
