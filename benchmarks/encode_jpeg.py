"""Compare JPEG settings using synthetic images, never desktop captures."""

import argparse
import io
import json
import statistics
import time

import numpy as np
from PIL import Image, ImageDraw


def fixtures(width, height):
    desktop = Image.new("RGB", (width, height), "#151922")
    draw = ImageDraw.Draw(desktop)
    for row in range(10):
        y = round(row * height / 10)
        draw.rectangle(
            (10, y + 5, width - 10, y + round(height / 12)),
            fill="#293247" if row % 2 else "#20293b",
        )
        for column in range(5):
            draw.text(
                (20 + column * width // 5, y + 20),
                f"Synthetic row {row} item {column}: 12345", fill="white",
            )
    rng = np.random.default_rng(42)
    x = np.linspace(0, 255, width, dtype=np.float32)[None, :]
    y = np.linspace(0, 255, height, dtype=np.float32)[:, None]
    pixels = np.empty((height, width, 3), dtype=np.float32)
    pixels[:, :, 0] = x
    pixels[:, :, 1] = y
    pixels[:, :, 2] = (x + y) / 2
    pixels += rng.normal(0, 15, pixels.shape)
    textured = Image.fromarray(np.clip(pixels, 0, 255).astype("uint8"))
    return [("desktop", desktop), ("textured_gradient", textured)]


def measure(image, quality, optimize, iterations, legacy=False):
    samples = []
    for iteration in range(iterations + 2):
        buffer = io.BytesIO()
        wall_start = time.perf_counter()
        cpu_start = time.process_time()
        if legacy:
            scale = min(1.0, 1280 / image.width, 720 / image.height)
            prepared = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.Resampling.BOX,
            ) if scale < 1 else image
        else:
            prepared = image
        prepared.save(buffer, format="JPEG", quality=quality, optimize=optimize)
        cpu_ms = (time.process_time() - cpu_start) * 1000
        wall_ms = (time.perf_counter() - wall_start) * 1000
        if iteration >= 2:
            samples.append((cpu_ms, wall_ms, len(buffer.getvalue())))
    return {
        "width": prepared.width,
        "height": prepared.height,
        "quality": quality,
        "optimize": optimize,
        "mean_cpu_ms": round(statistics.mean(sample[0] for sample in samples), 3),
        "median_wall_ms": round(statistics.median(sample[1] for sample in samples), 3),
        "frame_bytes": int(statistics.median(sample[2] for sample in samples)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--iterations", type=int, default=12)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not 64 <= args.width <= 8192 or not 64 <= args.height <= 8192:
        parser.error("Image dimensions must be from 64 to 8192")
    if args.iterations < 1:
        parser.error("Iterations must be positive")
    rows = []
    for name, image in fixtures(args.width, args.height):
        for profile, quality, optimize, legacy in [
            ("default", 65, True, False),
            ("fast-jpeg", 65, False, False),
            ("legacy", 60, False, True),
        ]:
            row = measure(image, quality, optimize, args.iterations, legacy)
            rows.append(dict(fixture=name, profile=profile, **row))
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print("fixture             profile      resolution    CPU ms   wall ms      bytes")
        for row in rows:
            resolution = f"{row['width']}x{row['height']}"
            print(
                f"{row['fixture']:20} {row['profile']:12} {resolution:12} "
                f"{row['mean_cpu_ms']:8.3f} {row['median_wall_ms']:9.3f} {row['frame_bytes']:10}"
            )


if __name__ == "__main__":
    main()
