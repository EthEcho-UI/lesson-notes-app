"""Lesson photos: turn whatever the user picked (any JPEG/PNG/WebP/GIF/BMP/TIFF file Pillow can read) into the small, metadata-free JPEG
the contract wants: long edge <= 1600 px, <= 600 KiB, no EXIF/GPS/XMP (the file is re-encoded from pixels, orientation applied)."""
import io
import logging
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger("lesson_record.photos")

MAX_IMAGES = 12                       # per note (contract)
MAX_BYTES = 614400                    # 600 KiB (contract)
LONG_EDGE = 1600
MAX_INPUT_BYTES = 40 * 1024 * 1024    # refuse absurd inputs before decoding
MAX_INPUT_PIXELS = 120_000_000        # decompression-bomb guard (a 48 MP phone photo is fine)
Image.MAX_IMAGE_PIXELS = MAX_INPUT_PIXELS


class PhotoError(ValueError):
    pass


def prepare_bytes(raw: bytes, label="photo"):
    """-> (jpeg_bytes, width, height). Raises PhotoError."""
    if not raw:
        raise PhotoError(f"{label}: empty file")
    if len(raw) > MAX_INPUT_BYTES:
        raise PhotoError(f"{label}: file is larger than {MAX_INPUT_BYTES // 2**20} MiB")
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.getchannel("A"))
                im = bg
            else:
                im = im.convert("RGB")
            scale = min(1.0, LONG_EDGE / max(im.size))
            size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
            if size != im.size:
                im = im.resize(size, Image.LANCZOS)
            for q in (85, 78, 70, 62, 55, 48, 40):
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=q, optimize=True)          # no exif=/icc_profile= argument: nothing is carried over
                if buf.tell() <= MAX_BYTES:
                    return buf.getvalue(), im.width, im.height
            while True:                                                  # still too big (noisy photo): shrink further
                im = im.resize((max(1, im.width * 4 // 5), max(1, im.height * 4 // 5)), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=48, optimize=True)
                if buf.tell() <= MAX_BYTES or max(im.size) < 200:
                    break
            if buf.tell() > MAX_BYTES:
                raise PhotoError(f"{label}: cannot get the photo under {MAX_BYTES // 1024} KiB")
            return buf.getvalue(), im.width, im.height
    except PhotoError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError, ValueError) as e:
        raise PhotoError(f"{label}: not a readable image ({type(e).__name__})") from None


def prepare_file(path, label=None):
    p = Path(path).expanduser()
    label = label or p.name
    try:
        if not p.is_file():
            raise PhotoError(f"{label}: no such file")
        if p.stat().st_size > MAX_INPUT_BYTES:
            raise PhotoError(f"{label}: file is larger than {MAX_INPUT_BYTES // 2**20} MiB")
        raw = p.read_bytes()
    except OSError as e:
        raise PhotoError(f"{label}: {e.strerror or e}") from None
    return prepare_bytes(raw, label)
