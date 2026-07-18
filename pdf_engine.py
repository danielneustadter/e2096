"""DAF Form 2096 PDF pipeline: fill AcroForm fields, stamp signature overlays,
and flatten to an immutable raster PDF on finalization.

The blank template is the official DAF Form 2096 (20230331). The three /Sig
fields (Signature8 = supervisor, Signature9 = commander, Signature12 =
personnel official) cannot hold text, so approvals are stamped as text overlays
positioned at each signature field's rectangle — in the real Salesforce build
these are native digital signatures.
"""
import io
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas

TEMPLATE = Path(__file__).parent / "templates" / "daf2096_blank.pdf"

F = "topmostSubform[0].Page1[0].{}[0]"

# rects from the template's AcroForm (left, bottom, right, top; PDF points)
SIG_RECTS = {
    "supervisor": (372.5, 368.7, 554.4, 386.8),   # Signature8
    "commander": (375.4, 198.8, 555.4, 216.7),    # Signature9
    "fss": (375.9, 141.7, 554.6, 150.1),          # Signature12
}
PAGE_W, PAGE_H = 612, 792


def _overlay(signatures: dict[str, str], watermark: bool) -> PdfReader:
    """One-page overlay PDF with signature stamps and the demo watermark."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    c.setFillColor(HexColor("#1e7d3c"))
    from reportlab.pdfbase.pdfmetrics import stringWidth
    for role, stamp in signatures.items():
        left, bottom, right, top = SIG_RECTS[role]
        size = 7 if (top - bottom) < 12 else 8
        while size > 4 and stringWidth(stamp, "Helvetica-Oblique", size) > (right - left - 4):
            size -= 0.5
        c.setFont("Helvetica-Oblique", size)
        c.drawString(left + 2, bottom + (top - bottom - size) / 2 + 1, stamp)
    if watermark:
        c.saveState()
        c.setFillColor(HexColor("#c0392b"))
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(PAGE_W / 2, PAGE_H - 34,
                            "DEMONSTRATION PROTOTYPE - FICTIONAL DATA - NOT AN OFFICIAL PERSONNEL ACTION")
        c.translate(PAGE_W / 2, PAGE_H / 2)
        c.rotate(45)
        c.setFont("Helvetica-Bold", 42)
        c.setFillColorRGB(0.75, 0.2, 0.2, alpha=0.13)
        c.drawCentredString(0, 0, "DEMO - FICTIONAL DATA")
        c.restoreState()
    c.save()
    buf.seek(0)
    return PdfReader(buf)


def render_2096(field_values: dict[str, str], signatures: dict[str, str]) -> bytes:
    """Fill the blank 2096 with field_values and stamp signature overlays."""
    reader = PdfReader(str(TEMPLATE))
    writer = PdfWriter()
    writer.append(reader)
    acro = writer._root_object.get("/AcroForm")
    if acro is not None:
        acro = acro.get_object()
    if acro is not None and "/XFA" in acro:
        # drop the LiveCycle XFA layer so the plain AcroForm is authoritative
        # (lets pdfium init_forms and render field appearances)
        del acro["/XFA"]
    writer.update_page_form_field_values(
        writer.pages[0], field_values, auto_regenerate=True
    )
    writer.pages[0].merge_page(_overlay(signatures, watermark=True).pages[0])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def flatten(pdf_bytes: bytes, dpi: int = 200) -> bytes:
    """Bake the filled form to a raster PDF: nothing is editable afterward."""
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        doc.init_forms()
    except Exception:
        pass
    images = []
    for page in doc:
        images.append(page.render(scale=dpi / 72, may_draw_forms=True).to_pil().convert("RGB"))
    out = io.BytesIO()
    images[0].save(out, format="PDF", save_all=True, append_images=images[1:],
                   resolution=dpi)
    return out.getvalue()


def render_preview_png(pdf_bytes: bytes, scale: float = 1.6) -> bytes:
    """PNG of page 1 for the web UI preview."""
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        doc.init_forms()
    except Exception:
        pass
    img = doc[0].render(scale=scale, may_draw_forms=True).to_pil()
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
