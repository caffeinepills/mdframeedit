"""Deterministic, unrotated packing of cropped logical frame cells."""

from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image

from utils import FormatError

# [texture x, texture y, width, height, logical trim x, logical trim y].
NativeRegion = list[int]
Rectangle = tuple[int, int, int, int]
PADDING = 1


@dataclass(frozen=True)
class TextureLayout:
    """Mapping used by the editor; engines use the JSON region table directly."""

    frame_size: tuple[int, int]
    grid_size: tuple[int, int]
    regions: list[NativeRegion | None]
    texture_size: tuple[int, int]


def _canvas_size(size: tuple[int, int], power_of_two: bool) -> tuple[int, int]:
    if not power_of_two:
        return size
    return (1 << (size[0] - 1).bit_length(), 1 << (size[1] - 1).bit_length())


def _place(sizes: list[tuple[int, int, int]], width: int, height: int) -> dict[int, Rectangle] | None:
    """Place largest crops first with the MaxRects short-side fit heuristic."""
    free: list[Rectangle] = [(0, 0, width, height)]
    placed: dict[int, Rectangle] = {}
    for index, w, h in sizes:
        candidates = [
            (min(fw - w, fh - h), max(fw - w, fh - h), y, x)
            for x, y, fw, fh in free if w <= fw and h <= fh
        ]
        if not candidates:
            return None
        _, _, y, x = min(candidates)
        placed[index] = (x, y, w, h)
        split: list[Rectangle] = []
        for fx, fy, fw, fh in free:
            right, bottom = fx + fw, fy + fh
            if x >= right or x + w <= fx or y >= bottom or y + h <= fy:
                split.append((fx, fy, fw, fh))
                continue
            if fx < x:
                split.append((fx, fy, x - fx, fh))
            if x + w < right:
                split.append((x + w, fy, right - x - w, fh))
            if fy < y:
                split.append((fx, fy, fw, y - fy))
            if y + h < bottom:
                split.append((fx, y + h, fw, bottom - y - h))
        # Free rectangles can overlap; eliminate duplicate/contained entries.
        unique = list(dict.fromkeys(split))
        free = [r for r in unique if not any(
            r != other and r[0] >= other[0] and r[1] >= other[1]
            and r[0] + r[2] <= other[0] + other[2]
            and r[1] + r[3] <= other[1] + other[3]
            for other in unique
        )]
    return placed


def grid_layout(image: Image.Image, frame_size: tuple[int, int]) -> TextureLayout:
    """Keep every logical cell editable, including transparent and unused cells."""
    fw, fh = frame_size
    if min(fw, fh) <= 0 or min(image.size) <= 0 or image.width % fw or image.height % fh:
        raise FormatError("Texture dimensions must be multiples of frame_size.")
    columns, rows = image.width // fw, image.height // fh
    regions: list[NativeRegion | None] = [
        [index % columns * fw, index // columns * fh, fw, fh, 0, 0]
        for index in range(columns * rows)
    ]
    return TextureLayout(frame_size, (columns, rows), regions, image.size)


def pack_texture(
    image: Image.Image, frame_size: tuple[int, int], *, padding: int = PADDING,
    power_of_two: bool = False,
) -> tuple[Image.Image, TextureLayout]:
    """Crop by nonzero alpha, retaining cell indices and logical placement."""
    fw, fh = frame_size
    if type(padding) is not int or padding < 1:
        raise FormatError("Packing padding must be an integer of at least one pixel.")
    if type(power_of_two) is not bool:
        raise FormatError("power_of_two must be a boolean.")
    if min(fw, fh) <= 0 or min(image.size) <= 0 or image.width % fw or image.height % fh:
        raise FormatError("Texture dimensions must be multiples of frame_size.")
    columns, rows = image.width // fw, image.height // fh
    crops: dict[int, Image.Image] = {}
    regions: list[NativeRegion | None] = [None] * (columns * rows)
    for index in range(len(regions)):
        left, top = index % columns * fw, index // columns * fh
        tile = image.crop((left, top, left + fw, top + fh))
        bounds = tile.getchannel("A").getbbox()
        if bounds is not None:
            crop = tile.crop(bounds)
            crops[index] = crop
            regions[index] = [0, 0, crop.width, crop.height, bounds[0], bounds[1]]
    if not crops:
        return Image.new("RGBA", (1, 1)), TextureLayout(frame_size, (columns, rows), regions, (1, 1))
    sizes = sorted(((i, crop.width + padding * 2, crop.height + padding * 2)
                    for i, crop in crops.items()),
                   key=lambda item: (-max(item[1:]), -item[1] * item[2], -item[2], item[0]))
    area = sum(w * h for _, w, h in sizes)
    minimum = max(w for _, w, _ in sizes)
    maximum_height = max(h for _, _, h in sizes)
    # Try a bounded selection of widths around a square, plus the original
    # width. Output is tight and never rounded up to powers of two.
    side = max(minimum, math.isqrt(area))
    widths = {max(minimum, int(side * ratio)) for ratio in (0.75, 1, 1.25, 1.5, 2)}
    if side // 2 <= image.width <= side * 2:
        widths.add(image.width)
    best: tuple[tuple[int, int, int], dict[int, Rectangle], tuple[int, int]] | None = None
    for width in sorted(widths):
        height = max(maximum_height, math.ceil(area / width))
        placement = _place(sizes, width, height)
        # A small number of retries limits save-time work for large sprites.
        for _ in range(8):
            if placement is not None:
                break
            height = max(height + 1, math.ceil(height * 1.15))
            placement = _place(sizes, width, height)
        if placement is None:
            continue
        used = (max(x + w for x, y, w, h in placement.values()),
                max(y + h for x, y, w, h in placement.values()))
        used = _canvas_size(used, power_of_two)
        score = (used[0] * used[1], max(used), used[0])
        if best is None or score < best[0]:
            best = score, placement, used
    # A padded cell grid guarantees a fit without bypassing the padding when
    # the heuristic encounters a pathological rectangle set.
    padded_fw, padded_fh = fw + padding * 2, fh + padding * 2
    fallback_size = _canvas_size((columns * padded_fw, rows * padded_fh), power_of_two)
    if best is None or best[0][0] > fallback_size[0] * fallback_size[1]:
        placement = {}
        for index, region in enumerate(regions):
            if region is not None:
                _, _, w, h, tx, ty = region
                placement[index] = (index % columns * padded_fw + tx,
                                    index // columns * padded_fh + ty,
                                    w + padding * 2, h + padding * 2)
        best = (fallback_size[0] * fallback_size[1], max(fallback_size), fallback_size[0]), placement, fallback_size
    _, placement, packed_size = best
    packed = Image.new("RGBA", packed_size)
    for index, (x, y, w, h) in placement.items():
        region = regions[index]
        assert region is not None
        x, y = x + padding, y + padding
        region[:2] = [x, y]
        # No mask: preserve partial alpha and RGB rather than compositing twice.
        packed.paste(crops[index], (x, y))
    return packed, TextureLayout(frame_size, (columns, rows), regions, packed_size)


def unpack_texture(image: Image.Image, layout: TextureLayout) -> Image.Image:
    """Reconstruct the editor grid without changing indices, origins or alpha."""
    if image.size != layout.texture_size:
        raise FormatError("Packed artwork dimensions changed. Reopen the sheet to load its new layout.")
    fw, fh = layout.frame_size
    columns, rows = layout.grid_size
    sheet = Image.new("RGBA", (columns * fw, rows * fh))
    for index, region in enumerate(layout.regions):
        if region is None:
            continue
        x, y, width, height, tx, ty = region
        crop = image.crop((x, y, x + width, y + height))
        sheet.paste(crop, (index % columns * fw + tx, index // columns * fh + ty))
    return sheet
