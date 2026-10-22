"""Lesson photos: validation and privacy scrubbing before they are stored on `main` or shown to the model.

* Pillow decides whether the bytes really are a JPEG/PNG/WebP of the declared type and size (uploads are untrusted).
* EXIF / XMP / IPTC / text chunks (GPS position, camera, time, thumbnails, comments) are removed. When the file has no
  rotation EXIF the removal is LOSSLESS (byte-level segment surgery, no re-encode). When an EXIF orientation != 1 is present
  the pixels are rotated first (otherwise the photo would turn sideways once the tag is gone) and re-encoded with Pillow.
* Data outside the image stream (JPEG bytes after EOI, e.g. an embedded second picture) is cut off.
"""
import io
import logging
import struct
from dataclasses import dataclass

from PIL import Image, ImageOps

log = logging.getLogger("lesson_worker.images")

MAX_DIM = 4096
MAX_PIXELS = MAX_DIM * MAX_DIM
FORMATS = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP"}
MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
Image.MAX_IMAGE_PIXELS = MAX_PIXELS * 2          # Pillow raises DecompressionBombError above 2x this


class ImageError(Exception):
    pass


@dataclass
class CleanImage:
    data: bytes
    width: int
    height: int
    mime: str
    changed: bool          # bytes differ from the input (metadata removed and/or rotated)
    rotated: bool = False


# ---- lossless metadata removal -------------------------------------------------------------------------------------
_JPEG_KEEP = {0xE0, 0xE2, 0xEE}                  # JFIF, ICC profile, Adobe colour transform


def _strip_jpeg(b: bytes) -> bytes:
    if b[:2] != b"\xff\xd8":
        raise ImageError("not a JPEG")
    out = bytearray(b"\xff\xd8")
    i, n = 2, len(b)
    while i < n:
        if b[i] != 0xFF:
            raise ImageError("corrupt JPEG marker stream")
        while i < n and b[i] == 0xFF:            # fill bytes
            i += 1
        if i >= n:
            break
        m = b[i]
        i += 1
        if m == 0xD9:                            # EOI (before any scan: nothing to show)
            out += b"\xff\xd9"
            return bytes(out)
        if m == 0x01 or 0xD0 <= m <= 0xD7:       # standalone markers
            out += bytes([0xFF, m])
            continue
        if i + 2 > n:
            raise ImageError("truncated JPEG")
        (length,) = struct.unpack(">H", b[i:i + 2])
        seg = b[i + 2:i + length]
        if length < 2 or i + length > n:
            raise ImageError("truncated JPEG segment")
        drop = (0xE0 <= m <= 0xEF and m not in _JPEG_KEEP) or m == 0xFE
        if not drop:
            out += bytes([0xFF, m]) + struct.pack(">H", length) + seg
        i += length
        if m == 0xDA:                            # SOS: entropy-coded data follows up to (and including) EOI
            j = i
            while True:
                k = b.find(b"\xff", j)
                if k < 0 or k + 1 >= n:
                    out += b[i:]
                    return bytes(out)
                nxt = b[k + 1]
                if nxt == 0xD9:
                    out += b[i:k + 2]
                    return bytes(out)
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7 or nxt == 0xFF:
                    j = k + 1 if nxt == 0xFF else k + 2
                    continue
                # another marker inside the stream (progressive JPEG: next scan, tables): copy up to it and go on
                out += b[i:k]
                i = k
                break
    return bytes(out)


def _strip_png(b: bytes) -> bytes:
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        raise ImageError("not a PNG")
    out = bytearray(b[:8])
    i = 8
    while i + 12 <= len(b):
        (length,) = struct.unpack(">I", b[i:i + 4])
        kind = b[i + 4:i + 8]
        end = i + 12 + length
        if end > len(b):
            raise ImageError("truncated PNG chunk")
        if kind not in (b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"):
            out += b[i:end]
        i = end
        if kind == b"IEND":
            break
    return bytes(out)


def _strip_webp(b: bytes) -> bytes:
    if b[:4] != b"RIFF" or b[8:12] != b"WEBP":
        raise ImageError("not a WebP")
    body = bytearray()
    i = 12
    end_all = min(len(b), 8 + struct.unpack("<I", b[4:8])[0])
    while i + 8 <= end_all:
        kind = b[i:i + 4]
        (length,) = struct.unpack("<I", b[i + 4:i + 8])
        end = i + 8 + length + (length & 1)
        if end > len(b) + 1:
            raise ImageError("truncated WebP chunk")
        chunk = bytearray(b[i:end])
        if kind in (b"EXIF", b"XMP "):
            i = end
            continue
        if kind == b"VP8X" and length >= 10:
            chunk[8] &= ~(0x08 | 0x04) & 0xFF        # EXIF and XMP flags
        body += chunk
        i = end
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + bytes(body)


_STRIP = {"jpg": _strip_jpeg, "png": _strip_png, "webp": _strip_webp}


def clean_image(data: bytes, ext: str, max_dim=MAX_DIM) -> CleanImage:
    """Validate `data` as an image of type `ext` (jpg|png|webp) and return a privacy-scrubbed copy."""
    ext = ext.lower().lstrip(".")
    if ext not in FORMATS:
        raise ImageError(f"unsupported image type .{ext}")
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format != FORMATS[ext]:
                raise ImageError(f"file is {im.format}, not {FORMATS[ext]} as its name says")
            w, h = im.size
            if w < 1 or h < 1 or w > max_dim or h > max_dim:
                raise ImageError(f"image is {w}x{h} px; at most {max_dim} per side")
            im.load()                                     # really decode: catches truncated/corrupt data
            orientation = im.getexif().get(0x0112, 1)
            if orientation not in (None, 1):
                out = _rotate_and_reencode(im, ext)
                with Image.open(io.BytesIO(out)) as chk:
                    chk.load()
                    return CleanImage(out, chk.size[0], chk.size[1], MIME[ext], True, True)
    except ImageError:
        raise
    except Exception as e:                                # Pillow raises many types for broken files
        raise ImageError(f"cannot decode the image: {type(e).__name__}: {e}"[:200])
    try:
        out = _STRIP[ext](data)
        with Image.open(io.BytesIO(out)) as chk:         # the scrubbed copy must still be a good image
            chk.load()
            if chk.size != (w, h):
                raise ImageError("metadata removal changed the image")
    except ImageError:
        raise
    except Exception as e:
        log.warning("lossless metadata removal failed (%s); re-encoding instead", e)
        with Image.open(io.BytesIO(data)) as im:
            out = _rotate_and_reencode(im, ext)
    return CleanImage(out, w, h, MIME[ext], out != data)


def _rotate_and_reencode(im, ext):
    upright = ImageOps.exif_transpose(im)                 # applies the orientation, drops the tag from the copy
    buf = io.BytesIO()
    if ext == "jpg":
        upright.convert("RGB").save(buf, "JPEG", quality=90, optimize=True)       # no exif= argument: nothing is carried over
    elif ext == "png":
        upright.save(buf, "PNG", optimize=True)
    else:
        upright.save(buf, "WEBP", quality=90)
    return buf.getvalue()


def has_metadata(data: bytes, ext: str) -> bool:
    """True if Pillow still sees EXIF / text metadata (used by tests and by the `main`-image verification)."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            return bool(im.getexif()) or bool(getattr(im, "text", None)) or "xmp" in im.info or "XML:com.adobe.xmp" in im.info
    except Exception:
        return False
