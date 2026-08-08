"""Lay a batch of QR cards out on A4 for the print shop.

A batch is printed before anyone knows where the cards will go, so every card
on the sheet looks alike apart from its token. That token is printed in text
under the code on purpose: assigning a card means reading it off the card in
your hand and typing it in, and a QR you cannot read by eye would make a
drawer full of cards unsortable.

`reportlab` is imported inside the functions for the same reason `qrcode` is in
qr.py: it arrived after the first deploys, and at module level a server that
pulled the code without running `pip install` fails to start Django at all —
the guest-facing site goes down over an admin-only convenience.
"""

from __future__ import annotations

from io import BytesIO

# Millimetres, converted at use. A4 is 210×297mm.
PAGE_W_MM, PAGE_H_MM = 210.0, 297.0
MARGIN_MM = 12.0
COLS, ROWS = 3, 4  # 12 cards per sheet

# Of each cell, how much is the code itself. The rest carries the token and the
# caption, plus enough white that a slightly-off guillotine cut does not eat
# into the code's quiet zone.
QR_SIZE_MM = 40.0

# Matches qr.py: ERROR_CORRECT_Q (~25% recovery) and the 4-module quiet zone
# the spec requires. A card gets handled, taped and rained on, so the recovery
# level is worth the density.
BAR_LEVEL = "Q"
BAR_BORDER = 4


class PdfUnavailable(Exception):
    """The reportlab library isn't installed on this server."""


def _load():
    try:
        from reportlab.graphics import renderPDF
        from reportlab.graphics.barcode.qr import QrCodeWidget
        from reportlab.graphics.shapes import Drawing
        from reportlab.lib.units import mm
        from reportlab.pdfgen import canvas
    except ImportError as exc:  # pragma: no cover - exercised via the view test
        raise PdfUnavailable(
            "The 'reportlab' package is not installed on this server. "
            "Run: pip install -r requirements.txt"
        ) from exc
    return renderPDF, QrCodeWidget, Drawing, mm, canvas


def _draw_qr(renderPDF, QrCodeWidget, Drawing, pdf, value: str, x: float, y: float, size: float) -> None:
    """Draw `value` as a vector QR with its bottom-left corner at (x, y)."""
    widget = QrCodeWidget(value, barLevel=BAR_LEVEL, barBorder=BAR_BORDER)
    x0, y0, x1, y1 = widget.getBounds()
    # The widget draws at its own natural size; scale the containing drawing
    # rather than the modules, so the code stays vector and stays square.
    drawing = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    drawing.add(widget)
    renderPDF.draw(drawing, pdf, x, y)


def _crop_marks(pdf, mm, left: float, bottom: float, cell_w: float, cell_h: float) -> None:
    """Short rules just outside each cell corner, for a guillotine to line up on.

    Marks rather than a full grid: a printed rule that is not cut off exactly
    leaves a visible line down the finished card.
    """
    tick = 3 * mm
    pdf.setLineWidth(0.25)
    pdf.setStrokeGray(0.6)
    for cx in (left, left + cell_w):
        for cy in (bottom, bottom + cell_h):
            pdf.line(cx - tick, cy, cx - tick / 3, cy)
            pdf.line(cx + tick / 3, cy, cx + tick, cy)
            pdf.line(cx, cy - tick, cx, cy - tick / 3)
            pdf.line(cx, cy + tick / 3, cx, cy + tick)


def batch_pdf_bytes(tokens_and_urls, *, title: str, caption: str = "") -> bytes:
    """Render `(token, url)` pairs to a multi-page A4 PDF.

    Takes the URLs already built rather than the tags themselves: only the view
    knows the site's absolute address, and a card printed with the wrong host
    is landfill.
    """
    renderPDF, QrCodeWidget, Drawing, mm, canvas = _load()

    buf = BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(PAGE_W_MM * mm, PAGE_H_MM * mm))
    pdf.setTitle(title)

    cell_w = (PAGE_W_MM - 2 * MARGIN_MM) / COLS * mm
    cell_h = (PAGE_H_MM - 2 * MARGIN_MM) / ROWS * mm
    per_page = COLS * ROWS

    for index, (token, url) in enumerate(tokens_and_urls):
        if index and index % per_page == 0:
            pdf.showPage()

        slot = index % per_page
        col, row = slot % COLS, slot // COLS
        left = MARGIN_MM * mm + col * cell_w
        # Cells fill from the top of the page; PDF coordinates start at the bottom.
        bottom = (PAGE_H_MM - MARGIN_MM) * mm - (row + 1) * cell_h

        _crop_marks(pdf, mm, left, bottom, cell_w, cell_h)

        size = QR_SIZE_MM * mm
        _draw_qr(
            renderPDF, QrCodeWidget, Drawing, pdf, url,
            left + (cell_w - size) / 2,
            bottom + cell_h - size - 8 * mm,
            size,
        )

        # Courier because the token is meant to be transcribed, and a
        # proportional font makes a run of capitals harder to read back.
        pdf.setFillGray(0.0)
        pdf.setFont("Courier-Bold", 15)
        pdf.drawCentredString(left + cell_w / 2, bottom + cell_h - size - 15 * mm, token)

        if caption:
            pdf.setFont("Helvetica", 7)
            pdf.setFillGray(0.45)
            pdf.drawCentredString(left + cell_w / 2, bottom + cell_h - size - 20 * mm, caption)

    if not len(tokens_and_urls):
        # An empty batch still has to produce a valid PDF rather than a
        # zero-byte file the browser reports as corrupt.
        pdf.setFont("Helvetica", 11)
        pdf.drawCentredString(PAGE_W_MM * mm / 2, PAGE_H_MM * mm / 2, "This batch has no cards yet.")

    pdf.showPage()
    pdf.save()
    return buf.getvalue()
