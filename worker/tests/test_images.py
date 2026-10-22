"""Photo validation and metadata scrubbing (privacy): lossless where possible, rotation applied when the EXIF orientation is the only thing telling the viewer which way up it is."""
import io
import struct

import numpy as np
import pytest
from PIL import Image, PngImagePlugin

from helpers import jpeg_with_exif
from lesson_worker.images import ImageError, clean_image, has_metadata


def pixels(b):
    return np.asarray(Image.open(io.BytesIO(b)).convert("RGB"))


def test_jpeg_exif_gps_removed_losslessly():
    raw = jpeg_with_exif()
    out = clean_image(raw, "jpg")
    assert out.changed and not out.rotated and (out.width, out.height) == (64, 48)
    assert b"SecretCameraMaker" not in out.data and not has_metadata(out.data, "jpg")
    assert np.array_equal(pixels(raw), pixels(out.data))              # byte surgery, not re-compression
    assert Image.open(io.BytesIO(out.data)).getexif().get(0x8825) is None


def test_clean_jpeg_is_returned_unchanged():
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (1, 2, 3)).save(buf, "JPEG")
    out = clean_image(buf.getvalue(), "jpg")
    assert not out.changed and out.data == buf.getvalue()


def test_jpeg_comment_xmp_and_trailing_data_are_cut():
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (1, 2, 3)).save(buf, "JPEG")
    raw = bytearray(buf.getvalue())
    com = b"\xff\xfe" + struct.pack(">H", 2 + 11) + b"secret note"
    xmp = b"\xff\xe1" + struct.pack(">H", 2 + 30) + b"http://ns.adobe.com/xap/1.0/\x00 xx"[:30]
    raw[2:2] = com + xmp
    raw += b"\xff\xd8 hidden second picture / trailing bytes"
    out = clean_image(bytes(raw), "jpg")
    assert b"secret note" not in out.data and b"adobe" not in out.data and b"hidden second" not in out.data
    assert out.data.endswith(b"\xff\xd9") and np.array_equal(pixels(bytes(raw)), pixels(out.data))


def test_progressive_jpeg_survives():
    buf = io.BytesIO()
    im = Image.effect_noise((120, 90), 60).convert("RGB")
    im.save(buf, "JPEG", progressive=True, exif=jpeg_exif())
    out = clean_image(buf.getvalue(), "jpg")
    assert out.changed and np.array_equal(pixels(buf.getvalue()), pixels(out.data))


def jpeg_exif():
    ex = Image.Exif()
    ex[0x010F] = "SecretCameraMaker"
    return ex


def test_exif_orientation_rotates_pixels_then_drops_the_tag():
    raw = jpeg_with_exif(w=60, h=40, orientation=6)
    out = clean_image(raw, "jpg")
    assert out.rotated and (out.width, out.height) == (40, 60) and not has_metadata(out.data, "jpg")
    px = pixels(out.data).astype(int)
    white = lambda a: (a.min(axis=-1) > 200).mean()
    assert white(px[0]) < 0.2 and max(white(px[:, 0]), white(px[:, -1])) > 0.8      # the white top row became a white side column


def test_png_text_and_exif_chunks_removed():
    im = Image.new("RGB", (12, 9), (5, 6, 7))
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "Jan Novak GPS 50.1 14.4")
    ex = Image.Exif()
    ex[0x010F] = "SecretCameraMaker"
    buf = io.BytesIO()
    im.save(buf, "PNG", pnginfo=info, exif=ex)
    assert b"Jan Novak" in buf.getvalue()
    out = clean_image(buf.getvalue(), "png")
    assert out.changed and b"Jan Novak" not in out.data and b"SecretCameraMaker" not in out.data and not has_metadata(out.data, "png")
    assert np.array_equal(pixels(buf.getvalue()), pixels(out.data))


def test_webp_exif_and_xmp_removed():
    im = Image.new("RGB", (16, 16), (9, 9, 9))
    ex = Image.Exif()
    ex[0x010F] = "SecretCameraMaker"
    buf = io.BytesIO()
    im.save(buf, "WEBP", lossless=True, exif=ex.tobytes(), xmp=b"<x:xmpmeta>secret-xmp</x:xmpmeta>")
    assert b"SecretCameraMaker" in buf.getvalue()
    out = clean_image(buf.getvalue(), "webp")
    assert out.changed and b"SecretCameraMaker" not in out.data and b"secret-xmp" not in out.data
    assert np.array_equal(pixels(buf.getvalue()), pixels(out.data))


@pytest.mark.parametrize("data,ext", [(b"", "jpg"), (b"not an image", "png"), (b"\xff\xd8\xff\xe0 truncated", "jpg"), (b"RIFF....WEBP", "webp")])
def test_garbage_is_rejected(data, ext):
    with pytest.raises(ImageError):
        clean_image(data, ext)


def test_declared_type_must_match_content():
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, "PNG")
    with pytest.raises(ImageError, match="not JPEG"):
        clean_image(buf.getvalue(), "jpg")
    with pytest.raises(ImageError):
        clean_image(buf.getvalue(), "gif")


def test_oversized_dimensions_are_rejected():
    buf = io.BytesIO()
    Image.new("1", (4200, 10)).save(buf, "PNG")
    with pytest.raises(ImageError, match="4096"):
        clean_image(buf.getvalue(), "png")


def test_truncated_pixel_data_is_rejected():
    buf = io.BytesIO()
    Image.effect_noise((200, 200), 80).convert("RGB").save(buf, "PNG")
    with pytest.raises(ImageError):
        clean_image(buf.getvalue()[:-400], "png")
