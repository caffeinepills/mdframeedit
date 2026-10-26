from __future__ import annotations

import base64
import math
from contextlib import contextmanager
from contextvars import ContextVar
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageChops, ImageDraw

from data import AnimFrame, ArtworkInfo, FormatError, Offset, PaletteColor

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping
    from os import PathLike
    from xml.etree.ElementTree import Element, ElementTree

    from pyglet.image import AbstractImage

    from data import ActionPoints, Rectangle, TLRectangle

_TEMPLATE_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAABYAAAAICAYAAAD9aA/QAAAABGdBTUEAALGPC/xhBQ"
    "AAAAlwSFlzAAAOwQAADsEBuJFr7QAAABh0RVh0U29mdHdhcmUAcGFpbnQubmV0"
    "IDQuMS42/U4J6AAAAGhJREFUKFOFjoESgDAIQvf/P23idh6QK73nAlm1IuLKyvkF"
    "Zx0VOZ2XcRgsFGaxx1l0gMStJaMSle90k8RfS1ZlYgYL7/oVasmqxDZPM1ncWj"
    "Iq6/s4Gg/s7WAmg4XCLPphwi86nFViPbRIjILFScHyAAAAAElFTkSuQmCC"
)
SHADOW_LABELS = ("Small (0)", "Medium (1)", "Large (2)")
SHADOW_COLORS = ((0, 255, 0, 255), (255, 0, 0, 255), (0, 0, 255, 255))
WHITE = (255, 255, 255, 255)


def write_xml(
    tree: ElementTree, file_name: str | PathLike[str], encoding: bool = False,
) -> None:
    with open(file_name, "wb") as output:
        if encoding:
            tree.write(file_name, encoding="utf-8", xml_declaration=True)
        else:
            output.write(b'<?xml version="1.0"?>\n')
            tree.write(output, encoding="utf-8", xml_declaration=False)


def template_image() -> Image.Image:
    with Image.open(BytesIO(base64.b64decode(_TEMPLATE_PNG))) as source:
        return source.convert("RGBA")


_PNG_SAVE_OPTIONS = ContextVar("png_save_options", default=(True, True))


@contextmanager
def png_save_options(*, verify: bool = True, optimize: bool = True):
    """Scope PNG settings to one conversion, without affecting other callers."""
    token = _PNG_SAVE_OPTIONS.set((verify, optimize))
    try:
        yield
    finally:
        _PNG_SAVE_OPTIONS.reset(token)


def save_png(
    image: Image.Image, path: str | PathLike[str], *,
    verify: bool | None = None, optimize: bool | None = None,
    allow_palette: bool = True,
) -> None:
    """Save exact RGBA pixels, using an indexed PNG only when it is smaller.

    Palette entries are exact RGBA values, never quantized or dithered. Verify
    encoded pixels before writing so callers' staged bundles remain lossless.
    """
    default_verify, default_optimize = _PNG_SAVE_OPTIONS.get()
    verify = default_verify if verify is None else verify
    optimize = default_optimize if optimize is None else optimize
    rgba = image.convert("RGBA")
    pixels = rgba.tobytes() if verify else None

    def encode(source, **options):
        output = BytesIO()
        source.save(output, format="PNG", optimize=optimize, **options)
        return output.getvalue()

    def matches(encoded):
        try:
            with Image.open(BytesIO(encoded)) as decoded:
                return decoded.size == rgba.size and decoded.convert("RGBA").tobytes() == pixels
        except (OSError, ValueError):
            return False

    encoded = encode(rgba)
    if verify and not matches(encoded):
        raise FormatError("PNG verification failed: encoded pixels differ from the source.")
    colors = rgba.getcolors(256) if optimize and allow_palette else None
    if colors:
        # Most frequent first, with deterministic ties; alpha variants have their
        # own entries, including RGB values underneath fully transparent pixels.
        palette = [color for count, color in sorted(colors, key=lambda item: (-item[0], item[1]))]
        indices = {color: index for index, color in enumerate(palette)}
        indexed = Image.frombytes("P", rgba.size, bytes(indices[pixel] for pixel in rgba.get_flattened_data()))
        indexed.putpalette(bytes(channel for color in palette for channel in color[:3]))
        indexed.info = rgba.info.copy()
        indexed.info.pop("transparency", None)
        bits = next(depth for depth in (1, 2, 4, 8) if len(palette) <= 2 ** depth)
        options = {"bits": bits}
        if any(color[3] != 255 for color in palette):
            options["transparency"] = bytes(color[3] for color in palette)
        candidate = encode(indexed, **options)
        if len(candidate) < len(encoded) and (not verify or matches(candidate)):
            encoded = candidate
    Path(path).write_bytes(encoded)


def analyze_artwork(image: Image.Image) -> ArtworkInfo:
    """Count exact RGBA entries, including transparent colors and alpha variants."""
    rgba = image if image.mode == "RGBA" else image.convert("RGBA")
    colors = [
        PaletteColor(color, count)
        for count, color in rgba.getcolors(rgba.width * rgba.height) or []
    ]
    colors.sort(key=lambda color: (-color.pixels, color.rgba))
    return ArtworkInfo(rgba.width, rgba.height, colors)


def shadow_image(size: int, black: bool = False) -> Image.Image:
    """Return a filled shadow image.

    Args:
        size: Size 0-2 based on the shadow size.
        black: Returns black without the dot for preview.

    """
    assert size in (0, 1, 2)  # noqa: S101
    image = template_image()
    included = (*SHADOW_COLORS[: size + 1], WHITE)
    fill = (0, 0, 0, 255) if black else SHADOW_COLORS[size]
    image.putdata(
        [
            (
                (WHITE if pixel == WHITE and not black else fill)
                if pixel in included
                else (0, 0, 0, 0)
            )
            for pixel in image.get_flattened_data()
        ]
    )
    return image


def integer(element: Element, tag: str, default: int | None = None) -> int:
    text = element.findtext(tag)
    if text is None or not text.strip():
        if default is not None:
            return default
        msg = f"Missing {tag} in {element.tag}."
        raise FormatError(msg)
    try:
        return int(text)
    except ValueError as error:
        msg_0 = f"Invalid {tag}: {text!r}."
        raise FormatError(msg_0) from error


def known_children(
    element: Element, allowed: Collection[str], repeated: Collection[str] = (),
) -> None:
    """Rejects attributes, unknown children and repetitions not allowed."""
    if element.attrib:
        msg = f"Unsupported attributes on '{element.tag}'."
        raise FormatError(msg)
    seen: set[str] = set()
    for child in element:
        if child.tag not in allowed:
            msg_0 = f"Unsupported {element.tag}/{child.tag}. It will not be preserved."
            raise FormatError(msg_0)
        if child.tag in seen and child.tag not in repeated:
            msg_1 = f"Repeated {element.tag}/{child.tag}."
            raise FormatError(msg_1)
        seen.add(child.tag)


def get_image_size(path: str | PathLike[str]) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            image.load()
            return image.size
    except (OSError, ValueError) as error:
        msg = f"Cannot load {Path(path).name}: {error}"
        raise FormatError(msg) from error




def check_duplicate_images(
    images: Iterable[tuple[Image.Image, ActionPoints]], body_check: bool = True,
) -> tuple[list[Image.Image], dict[int, AnimFrame], list[ActionPoints]]:
    unique_images: list[Image.Image] = []
    unique_body_points: list[ActionPoints] = []
    cached_rgba: list[Image.Image] = []
    images_to_frames: dict[int, AnimFrame] = {}
    testing: dict[int, int] = {}

    for img_idx, (image, body_points) in enumerate(images):
        # Alpha is part of the artwork: RGB-only comparisons merge translucent
        # frames that render differently. Check all four difference channels.
        convert = image.convert("RGBA")
        flipped = convert.transpose(Image.FLIP_LEFT_RIGHT)
        odd_width = image.width % 2 == 1

        dupe = False
        # Check if the image is a duplicate of our unique ones, if not, it will be unique.
        for compare_idx, compare_image in enumerate(unique_images):
            # Quick check for sizes.

            if (
                image.width == compare_image.width
                and image.height == compare_image.height
            ):
                diff = ImageChops.difference(convert, cached_rgba[compare_idx])
                if diff.getbbox(alpha_only=False) is None and (
                    body_check is False
                    or unique_body_points[compare_idx].equals(
                        body_points, False, odd_width
                    )
                ):
                    # It's a duplicate.
                    # print("duplicate")
                    ani_frame = AnimFrame()
                    ani_frame.frame_index = compare_idx
                    images_to_frames[img_idx] = ani_frame
                    dupe = True
                    break

                diff = ImageChops.difference(flipped, cached_rgba[compare_idx])
                if diff.getbbox(alpha_only=False) is None and (
                    body_check is False
                    or unique_body_points[compare_idx].equals(
                        body_points, True, odd_width
                    )
                ):
                    # print("I found a flip!")
                    ani_frame = AnimFrame()
                    ani_frame.frame_index = compare_idx
                    ani_frame.flip = True
                    images_to_frames[img_idx] = ani_frame
                    dupe = True
                    break

        if not dupe:
            ani_frame = AnimFrame()
            ani_frame.frame_index = len(unique_images)
            unique_images.append(image)
            cached_rgba.append(convert)
            unique_body_points.append(body_points)
            testing[ani_frame.frame_index] = img_idx
            images_to_frames[img_idx] = ani_frame

    return unique_images, images_to_frames, unique_body_points


def round_up_to_mult(in_int: int, in_mult: int) -> int:
    sub_int = in_int - 1
    div = sub_int // in_mult  # Use integer division (//) in Python
    return (div + 1) * in_mult


def center_and_apply_offset(
    frame_width: int,
    frame_height: int,
    rectangle: Rectangle | TLRectangle,
    flipped: bool,
    offset: Offset,
) -> tuple[int, int]:
    # Calculate the center of the frame
    center_x = frame_width // 2
    center_y = frame_height // 2

    # Calculate the center of the rectangle
    rect_center_x = rectangle.width / 2

    # Values do not seem to be correct when flipped.
    if flipped:
        rect_center_x = math.ceil(rect_center_x)
    else:
        rect_center_x = int(rect_center_x)

    rect_center_y = rectangle.height // 2

    # Calculate the new position based on the center, rectangle center, and offset
    new_x = center_x - rect_center_x + offset.x
    new_y = center_y - rect_center_y + offset.y

    return int(new_x), int(new_y)


def get_action_points_from_image(
    image: AbstractImage,
) -> tuple[Offset | None, Offset | None, Offset | None, Offset | None]:
    """Search an offsets image for the colors specifying attachment points on the animation."""
    # Use the same top-left pixel coordinates and overlapping-color rules as
    # multi-sheet import. A bottom-up scan used to shift Y by one pixel.
    data = image.get_image_data().get_data("RGBA", -image.width * 4)
    pil_image = Image.frombytes("RGBA", (image.width, image.height), data)
    return get_action_points_from_pil_image(pil_image)


def get_shadow_location_from_pil_image(image: Image.Image) -> Offset | None:
    # RGBA getbbox uses alpha, so transparent margins cannot contain markers.
    bounds = image.getbbox()
    if bounds is None:
        return None
    left, top, right, bottom = bounds
    pixels = image.load()

    for y in range(top, bottom):
        for x in range(left, right):
            pixel = pixels[x, y]

            if pixel[3] != 0:  # Check if the alpha channel is not transparent
                if pixel == (255, 255, 255, 255):  # white
                    # White is shadow, abandon once we find one..
                    return Offset(x, y)

    return None


def get_action_points_from_pil_image(
    image: Image.Image,
) -> tuple[Offset | None, Offset | None, Offset | None, Offset | None]:
    """Search an offsets image for the colors specifying attachment points on the animation."""
    r: Offset | None = None
    g: Offset | None = None
    b: Offset | None = None
    black: Offset | None = None

    bounds = image.getbbox()
    if bounds is None:
        return r, g, b, black
    left, top, right, bottom = bounds
    # Load once instead of asking Pillow to load/validate on every getpixel.
    pixels = image.load()

    for y in range(top, bottom):
        for x in range(left, right):
            pixel = pixels[x, y]

            if pixel[3] != 0:  # Check if the alpha channel is not transparent
                if pixel == (0, 0, 0, 255):  # black
                    black = Offset(x, y)
                if pixel[0] == 255:  # red
                    r = Offset(x, y)
                if pixel[1] == 255:  # green
                    g = Offset(x, y)
                if pixel[2] == 255:  # blue
                    b = Offset(x, y)

    return r, g, b, black




def overlap_colors(
    positions: Mapping[tuple[int, int], Iterable[tuple[int, int, int, int]]],
) -> dict[tuple[int, int], tuple[int, ...]]:
    combined_positions: dict[tuple[int, int], tuple[int, ...]] = {}

    # Iterate over the positions to check for overlaps and combine colors
    for pos, colors in positions.items():
        combined_color = [0, 0, 0, 0]
        for color in colors:
            # Combine colors, taking the maximum value for each channel
            combined_color = [max(c1, c2) for c1, c2 in zip(combined_color, color)]
        combined_positions[pos] = tuple(combined_color)

    return combined_positions
