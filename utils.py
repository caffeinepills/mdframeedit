import base64
import math
from io import BytesIO
from pathlib import Path
from typing import List, Tuple, Dict

import pyglet
from PIL import Image, ImageChops, ImageDraw

from data import AnimFrame, Offset


_TEMPLATE_PNG = (
    'iVBORw0KGgoAAAANSUhEUgAAABYAAAAICAYAAAD9aA/QAAAABGdBTUEAALGPC/xhBQ'
    'AAAAlwSFlzAAAOwQAADsEBuJFr7QAAABh0RVh0U29mdHdhcmUAcGFpbnQubmV0'
    'IDQuMS42/U4J6AAAAGhJREFUKFOFjoESgDAIQvf/P23idh6QK73nAlm1IuLKyvkF'
    'Zx0VOZ2XcRgsFGaxx1l0gMStJaMSle90k8RfS1ZlYgYL7/oVasmqxDZPM1ncWj'
    'Iq6/s4Gg/s7WAmg4XCLPphwi86nFViPbRIjILFScHyAAAAAElFTkSuQmCC'
)
SHADOW_LABELS = ('Small (0)', 'Medium (1)', 'Large (2)')
SHADOW_COLORS = ((0, 255, 0, 255), (255, 0, 0, 255), (0, 0, 255, 255))
WHITE = (255, 255, 255, 255)


def template_image():
    with Image.open(BytesIO(base64.b64decode(_TEMPLATE_PNG))) as source:
        return source.convert('RGBA')


def shadow_image(size: int, black: bool=False):
    """Return a filled shadow image.

    Args:
        size: Size 0-2 based on the shadow size.
        black: Returns black without the dot for preview.
    """
    assert size in (0, 1, 2)
    image = template_image()
    included = (*SHADOW_COLORS[:size + 1], WHITE)
    fill = (0, 0, 0, 255) if black else SHADOW_COLORS[size]
    image.putdata([
        ((WHITE if pixel == WHITE and not black else fill)
         if pixel in included else (0, 0, 0, 0))
        for pixel in image.getdata()
    ])
    return image


class FormatError(ValueError):
    """Error for when there is a failure to format the XML data."""


def integer(element, tag, default=None):
    text = element.findtext(tag)
    if text is None or not text.strip():
        if default is not None:
            return default
        raise FormatError(f"Missing {tag} in {element.tag}.")
    try:
        return int(text)
    except ValueError as error:
        raise FormatError(f"Invalid {tag}: {text!r}.") from error


def known_children(element, allowed, repeated=()):
    """Rejects attributes, unknown children and repetitions not allowed."""
    if element.attrib:
        raise FormatError(f"Unsupported attributes on '{element.tag}'.")
    seen = set()
    for child in element:
        if child.tag not in allowed:
            raise FormatError(f"Unsupported {element.tag}/{child.tag}. It will not be preserved.")
        if child.tag in seen and child.tag not in repeated:
            raise FormatError(f"Repeated {element.tag}/{child.tag}.")
        seen.add(child.tag)


def get_image_size(path):
    try:
        with Image.open(path) as image:
            image.load()
            return image.size
    except (OSError, ValueError) as error:
        raise FormatError(f"Cannot load {Path(path).name}: {error}") from error


class TopLeftTextureGrid(pyglet.image.TextureGrid):

    def __init__(self, grid):
        image = grid.get_texture()
        if isinstance(image, pyglet.image.TextureRegion):
            owner = image.owner
        else:
            owner = image

        super(pyglet.image.TextureGrid, self).__init__(
            image.x, image.y, image.z, image.width, image.height, owner)

        items = []
        y = image.height - grid.item_height
        for row in range(grid.rows):
            x = 0
            for col in range(grid.columns):
                items.append(
                    self.get_region(x, y, grid.item_width, grid.item_height))
                x += grid.item_width + grid.column_padding
            y -= grid.item_height + grid.row_padding

        self.items = items
        self.rows = grid.rows
        self.columns = grid.columns
        self.item_width = grid.item_width
        self.item_height = grid.item_height


class TopLeftGrid(pyglet.image.ImageGrid):

    def _update_items(self):
        if not self._items:
            self._items = []
            y = self.image.height - self.item_height
            for row in range(self.rows):
                x = 0
                for col in range(self.columns):
                    self._items.append(self.image.get_region(
                        x, y, self.item_width, self.item_height))
                    x += self.item_width + self.column_padding
                y -= self.item_height + self.row_padding

    def get_texture_sequence(self):
        if not self._texture_grid:
            self._texture_grid = TopLeftTextureGrid(self)
        return self._texture_grid


class Camera:
    def __init__(self, glWidget: 'PygletWidget', position):
        self.glWidget = glWidget
        self.x, self.y = position
        self._zoom = 1.0

    @property
    def zoom(self):
        return self._zoom

    @zoom.setter
    def zoom(self, value):
        self._zoom = max(min(value, 4.0), 0.25)

    def __enter__(self):
        self.begin()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end()

    def begin(self):
        x = -(self.glWidget.width() // 2) / self._zoom + self.x
        y = -(self.glWidget.height() // 3) / self._zoom + self.y

        view_matrix = self.glWidget.view.translate((-x * self._zoom, -y * self._zoom, 0))
        view_matrix = view_matrix.scale((self._zoom, self._zoom, 1))
        self.glWidget.view = view_matrix

    def end(self):
        x = -(self.glWidget.width() // 2) / self._zoom + self.x
        y = -(self.glWidget.height() // 3) / self._zoom + self.y

        view_matrix = self.glWidget.view.scale((1 / self._zoom, 1 / self._zoom, 1))
        view_matrix = view_matrix.translate((x * self._zoom, y * self._zoom, 0))
        self.glWidget.view = view_matrix


def check_duplicate_images(images: List, body_check=True):
    unique_images = []
    unique_body_points = []
    cached_rgba = []
    images_to_frames = {}
    testing = {}

    for img_idx, (image, body_points) in enumerate(images):
        # Alpha is part of the artwork: RGB-only comparisons merge translucent
        # frames that render differently. Check all four difference channels.
        convert = image.convert('RGBA')
        flipped = convert.transpose(Image.FLIP_LEFT_RIGHT)
        odd_width = image.width % 2 == 1

        dupe = False
        # Check if the image is a duplicate of our unique ones, if not, it will be unique.
        for compare_idx, compare_image in enumerate(unique_images):
            # Quick check for sizes.

            if image.width == compare_image.width and image.height == compare_image.height:
                diff = ImageChops.difference(convert, cached_rgba[compare_idx])
                if diff.getbbox(alpha_only=False) is None and (body_check is False or unique_body_points[compare_idx].equals(body_points, False, odd_width)):
                    # It's a duplicate.
                    #print("duplicate")
                    ani_frame = AnimFrame()
                    ani_frame.frame_index = compare_idx
                    images_to_frames[img_idx] = ani_frame
                    dupe = True
                    break

                diff = ImageChops.difference(flipped, cached_rgba[compare_idx])
                if diff.getbbox(alpha_only=False) is None and (body_check is False or unique_body_points[compare_idx].equals(body_points, True, odd_width)):
                    #print("I found a flip!")
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


def center_and_apply_offset(frame_width, frame_height, rectangle, flipped, offset):
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


def get_action_points_from_image(image: pyglet.image.ImageDataRegion) -> Tuple[None | Offset, None | Offset, None | Offset,
                                                                           None | Offset]:
    """Search an offsets image for the colors specifying attachment points on the animation."""
    # Use the same top-left pixel coordinates and overlapping-color rules as
    # multi-sheet import. A bottom-up scan used to shift Y by one pixel.
    data = image.get_image_data().get_data('RGBA', -image.width * 4)
    pil_image = Image.frombytes('RGBA', (image.width, image.height), data)
    return get_action_points_from_pil_image(pil_image)


def get_shadow_location_from_pil_image(image) -> Offset | None:
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


def get_action_points_from_pil_image(image) -> Tuple[None | Offset, None | Offset, None | Offset,
                                                                           None | Offset]:
    """Search an offsets image for the colors specifying attachment points on the animation."""
    r = None
    g = None
    b = None
    black = None

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

def create_plus_image(size: int, color: Tuple):
    # Create a new image with a white background
    dimensions = (size, size)
    image = Image.new('RGBA', dimensions, (0, 0, 0, 0))

    draw = ImageDraw.Draw(image)

    center = (size // 2, size // 2)
    draw.line([(center[0] - 2, center[1]), (center[0] + 2, center[1])], fill=color, width=1)
    draw.line([(center[0], center[1] - 2), (center[0], center[1] + 2)], fill=color, width=1)

    return pyglet.image.ImageData(image.width, image.height, 'RGBA', image.tobytes())

def overlap_colors(positions: Dict):
    combined_positions = {}

    # Iterate over the positions to check for overlaps and combine colors
    for pos, colors in positions.items():
        combined_color = [0, 0, 0, 0]
        for color in colors:
            # Combine colors, taking the maximum value for each channel
            combined_color = [max(c1, c2) for c1, c2 in zip(combined_color, color)]
        combined_positions[pos] = tuple(combined_color)

    return combined_positions
