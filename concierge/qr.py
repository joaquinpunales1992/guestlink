"""Render a location's landing URL as a QR code.

SVG is the format to print from — it stays sharp at any card size, where a PNG
has to be generated large enough up front. PNG is there for anything that
cannot place an SVG (a chat message, a supplier's web form).
"""

from __future__ import annotations

from io import BytesIO

import qrcode
from qrcode.image.svg import SvgPathImage

# Q corrects around 25% of the symbol. A card taped to a restaurant counter
# picks up scuffs and fingerprints, and the extra redundancy costs only a
# slightly denser code.
ERROR_CORRECTION = qrcode.constants.ERROR_CORRECT_Q

# Four modules of quiet zone is the spec minimum; scanners lose the code
# without it, and print shops routinely trim to the ink.
BORDER = 4

PNG_BOX_SIZE = 12  # ~12px per module → a comfortably scannable PNG


def payload(request, location) -> str:
    """The absolute URL the code should encode.

    No trailing slash: it is what the already-printed cards use, and it keeps
    the encoded string shorter, which makes for a less dense code.
    """
    return request.build_absolute_uri(f"/{location.slug}")


def _code(data: str) -> qrcode.QRCode:
    code = qrcode.QRCode(error_correction=ERROR_CORRECTION, border=BORDER)
    code.add_data(data)
    code.make(fit=True)
    return code


def svg_bytes(data: str) -> bytes:
    buf = BytesIO()
    _code(data).make_image(image_factory=SvgPathImage).save(buf)
    return buf.getvalue()


def png_bytes(data: str) -> bytes:
    buf = BytesIO()
    code = qrcode.QRCode(
        error_correction=ERROR_CORRECTION, border=BORDER, box_size=PNG_BOX_SIZE
    )
    code.add_data(data)
    code.make(fit=True)
    code.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    return buf.getvalue()
