"""Incremental AcroForm fill for already-signed PDFs.

pypdf rewrites the whole file, which cryptographically invalidates existing
signatures. This module updates field values as PDF *incremental updates*
(appended revisions) via pyHanko's writer, so earlier PAdES signatures remain
intact — the same mechanism Adobe uses for multi-signer workflows. The first
signature certifies the document with FILL_FORMS permission, making these
updates legitimate under DocMDP.
"""
import io

from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.generic import pdf_name
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter


def _iter_fields(field_refs, prefix=""):
    for ref in field_refs:
        obj = ref.get_object()
        t = obj.get("/T")
        full = f"{prefix}.{t}" if (prefix and t is not None) else (str(t) if t is not None else prefix)
        kids = obj.get("/Kids")
        if kids is not None and obj.get("/FT") is None:
            yield from _iter_fields(kids, full)
        else:
            yield full, ref, obj


def _escape(s: str) -> bytes:
    out = s.encode("cp1252", "replace")
    return out.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _layout(value: str, width: float, height: float):
    """Fit text into the box: shrink single lines, word-wrap tall boxes."""
    import textwrap
    avg = 0.5  # approx Helvetica advance per pt of font size
    size = min(9.0, max(5.0, height - 3))
    if height < 20:  # single-line field: shrink to fit width
        if value:
            size = max(4.5, min(size, (width - 4) / (avg * len(value))))
        return size, [value]
    while size > 5:
        per_line = max(8, int((width - 4) / (avg * size)))
        lines = textwrap.wrap(value, per_line) or [""]
        if len(lines) * (size + 1.5) <= height - 2:
            return size, lines
        size -= 0.5
    return size, lines


def _text_ap(w, obj, value: str):
    """Build a minimal /N appearance stream so every viewer renders the value."""
    rect = [float(x) for x in obj["/Rect"]]
    width, height = abs(rect[2] - rect[0]), abs(rect[3] - rect[1])
    size, lines = _layout(value, width, height)
    font = generic.DictionaryObject({
        pdf_name("/Type"): pdf_name("/Font"),
        pdf_name("/Subtype"): pdf_name("/Type1"),
        pdf_name("/BaseFont"): pdf_name("/Helvetica"),
        pdf_name("/Encoding"): pdf_name("/WinAnsiEncoding"),
    })
    font_ref = w.add_object(font)
    leading = size + 1.5
    y0 = (height - size) / 2 + 1.2 if len(lines) == 1 else height - size - 1.5
    body = b"\nT* ".join(b"(%s) Tj" % _escape(ln) for ln in lines)
    content = (
        b"/Tx BMC\nq\nBT\n/HelvE2096 %.1f Tf\n%.1f TL\n0 g\n2 %.1f Td\n%s\nET\nQ\nEMC"
        % (size, leading, y0, body)
    )
    stream = generic.StreamObject(
        dict_data={
            pdf_name("/Type"): pdf_name("/XObject"),
            pdf_name("/Subtype"): pdf_name("/Form"),
            pdf_name("/BBox"): generic.ArrayObject(
                [generic.FloatObject(0), generic.FloatObject(0),
                 generic.FloatObject(width), generic.FloatObject(height)]),
            pdf_name("/Resources"): generic.DictionaryObject({
                pdf_name("/Font"): generic.DictionaryObject(
                    {pdf_name("/HelvE2096"): font_ref}),
            }),
        },
        stream_data=content,
    )
    return w.add_object(stream)


def fill_incremental(pdf_bytes: bytes, text_values: dict[str, str],
                     checkbox_values: dict[str, str] | None = None) -> bytes:
    """Set field values as an appended revision. Returns new PDF bytes."""
    checkbox_values = checkbox_values or {}
    targets = {**text_values, **checkbox_values}
    w = IncrementalPdfFileWriter(io.BytesIO(pdf_bytes), strict=False)
    acro = w.root["/AcroForm"]
    found = set()
    for full, ref, obj in _iter_fields(acro["/Fields"]):
        if full not in targets:
            continue
        found.add(full)
        if full in checkbox_values:
            state = pdf_name(checkbox_values[full])
            obj[pdf_name("/V")] = state
            obj[pdf_name("/AS")] = state
        else:
            value = text_values[full]
            obj[pdf_name("/V")] = generic.TextStringObject(value)
            ap_ref = _text_ap(w, obj, value)
            obj[pdf_name("/AP")] = generic.DictionaryObject(
                {pdf_name("/N"): ap_ref})
        w.mark_update(ref)
    missing = set(targets) - found
    if missing:
        raise KeyError(f"fields not found: {missing}")
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()
