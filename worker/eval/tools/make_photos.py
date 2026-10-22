"""Generate two whiteboard-like photos (PIL) for eval input 05: a quadratic-equation solution and a parabola sketch."""
import random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter
OUT = Path(__file__).resolve().parents[1] / "inputs" / "photos"
OUT.mkdir(parents=True, exist_ok=True)
FONT = "/usr/share/fonts/TTF/DejaVuSans-Oblique.ttf"
random.seed(7)

def board(w=1400, h=1000):
    im = Image.new("RGB", (w, h), (236, 238, 236))
    d = ImageDraw.Draw(im)
    for _ in range(4000):                              # faint ghosting / noise
        x, y = random.randrange(w), random.randrange(h)
        d.point((x, y), fill=(222 + random.randrange(10),) * 3)
    d.rectangle([0, 0, w - 1, h - 1], outline=(160, 160, 160), width=14)
    return im, d

def finish(im, name, angle):
    im = im.rotate(angle, resample=Image.BICUBIC, fillcolor=(120, 120, 120)).filter(ImageFilter.GaussianBlur(0.8))
    im.save(OUT / name, quality=82)

im, d = board()
f = lambda s: ImageFont.truetype(FONT, s)
blue, black, red = (20, 40, 150), (25, 25, 25), (170, 30, 30)
d.text((70, 50), "Kvadratická rovnice", font=f(60), fill=red)
d.text((70, 170), "2x² − 5x + 3 = 0", font=f(76), fill=blue)
d.text((70, 300), "D = b² − 4ac", font=f(64), fill=black)
d.text((70, 400), "D = 25 − 24 = 1", font=f(64), fill=black)
d.text((70, 520), "x₁,₂ = (−b ± √D) / 2a = (5 ± 1) / 4", font=f(64), fill=black)
d.text((70, 660), "x₁ = 3/2", font=f(68), fill=blue)
d.text((460, 660), "x₂ = 1", font=f(68), fill=blue)
d.text((70, 800), "D > 0: 2 kořeny   D = 0: 1 kořen   D < 0: žádný", font=f(46), fill=red)
finish(im, "01.jpg", -1.5)

im, d = board()
ox, oy = 250, 760                                     # origin in px; 1 unit = 330 px in x, 90 px in y
d.line([(80, oy), (1330, oy)], fill=black, width=6); d.line([(ox, 940), (ox, 80)], fill=black, width=6)
d.text((1290, oy + 14), "x", font=f(48), fill=black); d.text((ox + 16, 60), "y", font=f(48), fill=black)
pts = []
for i in range(-60, 241):
    x = i / 100
    y = 2 * x * x - 5 * x + 3
    pts.append((ox + x * 330 * 0.6 + 330 * 0.6 * 0, oy - y * 90))
pts = [(ox + (i / 100) * 330 * 0.9, oy - (2 * (i / 100) ** 2 - 5 * (i / 100) + 3) * 90) for i in range(-30, 215)]
pts = [p for p in pts if 90 < p[1] < 930]
d.line(pts, fill=blue, width=7)
for xv, lab in ((1, "1"), (1.5, "3/2")):
    px = ox + xv * 330 * 0.9
    d.ellipse([px - 11, oy - 11, px + 11, oy + 11], fill=red)
    d.text((px - 20, oy + 22), lab, font=f(46), fill=red)
vx, vy = 1.25, 2 * 1.25 ** 2 - 5 * 1.25 + 3
px, py = ox + vx * 330 * 0.9, oy - vy * 90
d.ellipse([px - 11, py - 11, px + 11, py + 11], fill=(30, 130, 30))
d.text((px - 70, py + 95), "vrchol V", font=f(46), fill=(30, 130, 30))
d.text((780, 140), "y = 2x² − 5x + 3", font=f(60), fill=blue)
d.text((780, 230), "kořeny: x₁ = 3/2, x₂ = 1", font=f(44), fill=black)
finish(im, "02.jpg", 1.0)
print("ok")
