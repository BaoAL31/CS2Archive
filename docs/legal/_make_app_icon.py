"""One-shot 1024x1024 Meta app icon. Not imported by the pipeline."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).with_name("app-icon.png")
SIZE = 1024
img = Image.new("RGB", (SIZE, SIZE), "#0d1117")
draw = ImageDraw.Draw(img)
draw.rounded_rectangle((48, 48, SIZE - 48, SIZE - 48), radius=160, outline="#f97316", width=28)
draw.ellipse((312, 220, 712, 620), outline="#f97316", width=36)
draw.polygon([(512, 340), (640, 420), (512, 500)], fill="#f97316")
try:
    font = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 96)
except OSError:
    font = ImageFont.load_default()
label = "CS2 POV"
bbox = draw.textbbox((0, 0), label, font=font)
tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
draw.text(((SIZE - tw) / 2, 700), label, fill="#f3f4f6", font=font)
img.save(OUT, "PNG")
print(OUT, img.size)
