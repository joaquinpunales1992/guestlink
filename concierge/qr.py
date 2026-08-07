"""Render a location's landing URL as a QR code.

SVG is the format to print from — it stays sharp at any card size, where a PNG
has to be generated large enough up front. PNG is there for anything that
cannot place an SVG (a chat message, a supplier's web form).

`qrcode` is imported inside the functions on purpose. It arrived after the
first deploys, and at module level a server that pulled the code without
running `pip install` fails to start Django at all — the guest-facing site goes
down over an admin-only convenience. Importing late keeps the blast radius to
this one page.
"""

from __future__ import annotations

from io import BytesIO

# qrcode's ERROR_CORRECT_Q — around 25% recovery. Hardcoded so this module stays
# importable without the dependency installed; the tests assert it still matches
# the library's own constant.
ERROR_CORRECTION = 3

# Four modules of quiet zone is the spec minimum; scanners lose the code
# without it, and print shops routinely trim to the ink.
BORDER = 4

PNG_BOX_SIZE = 12  # ~12px per module → a comfortably scannable PNG


class QrUnavailable(Exception):
    """The qrcode library isn't installed on this server."""


def payload(request, location) -> str:
    """The absolute URL the code should encode.

    No trailing slash: it is what the already-printed cards use, and it keeps
    the encoded string shorter, which makes for a less dense code.
    """
    return request.build_absolute_uri(f"/{location.slug}")


def _load():
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
    except ImportError as exc:  # pragma: no cover - exercised via the view test
        raise QrUnavailable(
            "The 'qrcode' package is not installed on this server. "
            "Run: pip install -r requirements.txt"
        ) from exc
    return qrcode, SvgPathImage


def _code(qrcode_mod, data: str, **kwargs):
    code = qrcode_mod.QRCode(error_correction=ERROR_CORRECTION, border=BORDER, **kwargs)
    code.add_data(data)
    code.make(fit=True)
    return code


def svg_bytes(data: str) -> bytes:
    qrcode_mod, svg_factory = _load()
    buf = BytesIO()
    _code(qrcode_mod, data).make_image(image_factory=svg_factory).save(buf)
    return buf.getvalue()


def png_bytes(data: str) -> bytes:
    qrcode_mod, _ = _load()
    buf = BytesIO()
    code = _code(qrcode_mod, data, box_size=PNG_BOX_SIZE)
    code.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    return buf.getvalue()
