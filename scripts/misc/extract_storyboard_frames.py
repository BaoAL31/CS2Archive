"""Extract opening frames from yt-dlp MHTML storyboard downloads."""
from __future__ import annotations

import argparse
from email import policy
from email.parser import BytesParser
from pathlib import Path

from PIL import Image, ImageDraw


def extract_opening(mhtml: Path, output: Path, frame_count: int = 8) -> Path:
    message = BytesParser(policy=policy.default).parsebytes(mhtml.read_bytes())
    images = [
        part.get_payload(decode=True)
        for part in message.walk()
        if part.get_content_maintype() == "image"
    ]
    if not images:
        raise ValueError(f"No storyboard images found in {mhtml}")

    sheet_path = output.with_suffix(".sheet.jpg")
    sheet_path.parent.mkdir(parents=True, exist_ok=True)
    sheet_path.write_bytes(images[0])
    with Image.open(sheet_path) as sheet:
        columns = 5
        rows = 5
        tile_width = sheet.width // columns
        tile_height = sheet.height // rows
        scale = 2
        label_height = 24
        contact = Image.new(
            "RGB",
            (tile_width * scale * 4, (tile_height * scale + label_height) * 2),
            "black",
        )
        draw = ImageDraw.Draw(contact)
        for index in range(min(frame_count, columns * rows)):
            x = (index % columns) * tile_width
            y = (index // columns) * tile_height
            tile = sheet.crop((x, y, x + tile_width, y + tile_height))
            tile = tile.resize((tile_width * scale, tile_height * scale))
            target_x = (index % 4) * tile_width * scale
            target_y = (index // 4) * (tile_height * scale + label_height)
            contact.paste(tile, (target_x, target_y))
            draw.text((target_x + 6, target_y + tile_height * scale + 4), f"frame {index}", fill="white")
    contact.save(output, quality=92)
    sheet_path.unlink()
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--outdir", type=Path, required=True)
    args = parser.parse_args()
    for mhtml in sorted(args.folder.glob("*.mhtml")):
        output = args.outdir / f"{mhtml.stem}_opening.jpg"
        print(extract_opening(mhtml, output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
