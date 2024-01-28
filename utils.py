import math
from typing import List, Tuple

import pyglet
from PIL import Image, ImageChops

from data import AnimFrame, Rectangle, Offset


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
        x = -self.glWidget.width() // 2 / self._zoom + self.x
        y = -self.glWidget.height() // 3 / self._zoom + self.y

        view_matrix = self.glWidget.view.translate((-x * self._zoom, -y * self._zoom, 0))
        view_matrix = view_matrix.scale((self._zoom, self._zoom, 1))
        self.glWidget.view = view_matrix

    def end(self):
        x = -self.glWidget.width() // 2 / self._zoom + self.x
        y = -self.glWidget.height() // 3 / self._zoom + self.y

        view_matrix = self.glWidget.view.scale((1 / self._zoom, 1 / self._zoom, 1))
        view_matrix = view_matrix.translate((x * self._zoom, y * self._zoom, 0))
        self.glWidget.view = view_matrix


def checkDuplicateImages(images: List, bodyCheck=True):
    uniqueImages = []
    uniqueBodyPoints = []
    frames = []
    imagesToFrames = {}
    oldIdxToNewIdx = {}
    testing = {}

    for imgIdx, (image, bodyPoints) in enumerate(images):
        #imgData = image.get_image_data().get_data()
        flipped = image.transpose(Image.FLIP_LEFT_RIGHT)
        oddWidth = image.width % 2 == 1
        #print("imgIdx", imgIdx, oddWidth)

        dupe = False
        # Check if the image is a duplicate of our unique ones, if not, it will be unique.
        for compareIdx, compareImage in enumerate(uniqueImages):
            # Quick check for sizes.

            if image.width == compareImage.width and image.height == compareImage.height:
                #print("found another", imgIdx, compareIdx, testing[compareIdx])
                diff = ImageChops.difference(image, compareImage)
                if diff.getbbox() is None and (bodyCheck is False or uniqueBodyPoints[compareIdx].equals(bodyPoints, False, oddWidth)):
                    # It's a duplicate.
                    #print("duplicate")
                    aniFrame = AnimFrame()
                    aniFrame.frameIndex = compareIdx
                    imagesToFrames[imgIdx] = aniFrame
                    dupe = True
                    break

                diff = ImageChops.difference(flipped, compareImage)
                if diff.getbbox() is None and (bodyCheck is False or uniqueBodyPoints[compareIdx].equals(bodyPoints, True, oddWidth)):
                    #print("I found a flip!")
                    aniFrame = AnimFrame()
                    aniFrame.frameIndex = compareIdx
                    aniFrame.flip = True
                    imagesToFrames[imgIdx] = aniFrame
                    dupe = True
                    break

        if not dupe:
            aniFrame = AnimFrame()
            aniFrame.frameIndex = len(uniqueImages)
            #cachedData.append(imgData)
            uniqueImages.append(image)
            uniqueBodyPoints.append(bodyPoints)
            oldIdxToNewIdx[imgIdx] = aniFrame.frameIndex
            testing[aniFrame.frameIndex] = imgIdx
            imagesToFrames[imgIdx] = aniFrame

    #print("original", len(images))
    #print("Unique images:", len(uniqueImages))
    #print("Unique Frames:, frames.)

    return uniqueImages, imagesToFrames, uniqueBodyPoints, oldIdxToNewIdx


def roundUpToMult(in_int, in_mult) -> int:
    sub_int = in_int - 1
    div = sub_int // in_mult  # Use integer division (//) in Python
    return (div + 1) * in_mult


def center_and_apply_offset(frame_width, frame_height, rectangle, flipped, offset):
    # Calculate the center of the frame
    center_x = frame_width // 2
    center_y = frame_height // 2

    # Calculate the center of the rectangle
    rect_center_x = rectangle.width / 2

    # # Values do not seem to be correct when flipped.
    # if flipped:
    #     rect_center_x = math.ceil(rect_center_x)
    # else:
    #     rect_center_x = int(rect_center_x)

    rect_center_y = rectangle.height // 2

    # Calculate the new position based on the center, rectangle center, and offset
    new_x = center_x - rect_center_x + offset.x
    new_y = center_y - rect_center_y + offset.y

    return int(new_x), int(new_y)


def flip_image_x_axis(image):
    data = image.get_image_data().get_data()

    width, height = image.width, image.height

    flipped_image_data = bytearray(len(data))  # Create a new bytearray to store the flipped image data

    for y in range(height):
        for x in range(width):
            # Calculate the source and destination pixel indices
            src_pixel_start = (y * width + x) * 4  # Source pixel start index
            dest_pixel_start = (y * width + (width - 1 - x)) * 4  # Destination pixel start index for x-axis flip

            # Copy the RGBA values from the source to the destination
            flipped_image_data[dest_pixel_start:dest_pixel_start + 4] = data[src_pixel_start:src_pixel_start + 4]

    return pyglet.image.ImageData(width, height, image.format, bytes(flipped_image_data))


def flip_image_data(image):
    data = image.get_image_data().get_data()

    width, height = image.width, image.height

    flipped_image_data = bytearray(len(data))  # Create a new bytearray to store the flipped image data

    for y in range(height):
        for x in range(width):
            # Calculate the source and destination pixel indices
            src_pixel_start = (y * width + x) * 4  # Source pixel start index
            dest_pixel_start = (y * width + (width - 1 - x)) * 4  # Destination pixel start index for x-axis flip

            # Copy the RGBA values from the source to the destination
            flipped_image_data[dest_pixel_start:dest_pixel_start + 4] = data[src_pixel_start:src_pixel_start + 4]

    return bytes(flipped_image_data)


def findPixelBounds(image):
    data = image.get_image_data().get_data()

    width, height = image.width, image.height

    leftmost = None
    topmost = None
    rightmost = None
    bottommost = None

    for y in range(height):
        for x in range(width):
            pixel_start = (y * width + x) * 4  # Each pixel is represented by 4 bytes (RGBA)
            alpha = data[pixel_start + 3]

            if alpha != 0:  # Check if the alpha channel is not transparent
                if leftmost is None or x < leftmost:
                    leftmost = x
                if rightmost is None or x > rightmost:
                    rightmost = x
                if topmost is None or y > topmost:
                    topmost = y
                if bottommost is None or y < bottommost:
                    bottommost = y

    if leftmost is None or topmost is None or rightmost is None or bottommost is None:
        return None

    return Rectangle(leftmost, bottommost, rightmost - leftmost + 1, topmost - bottommost + 1)


def getShadowLocationFromImage(image) -> Offset | None:
    data = image.get_image_data().get_data()

    width, height = image.width, image.height

    for y in range(height):
        for x in range(width):
            pixel_start = (y * width + x) * 4  # Each pixel is represented by 4 bytes (RGBA)
            alpha = data[pixel_start + 3]

            if alpha != 0:  # Check if the alpha channel is not transparent
                pixel = data[pixel_start:pixel_start+3]
                if pixel == b'\xff\xff\xff':  # white
                    # White is shadow, abandon once we find one..
                    return Offset(x, y)

    return None


def getActionPointsFromImage(image: pyglet.image.ImageDataRegion) -> Tuple[None | Offset, None | Offset, None | Offset,
                                                                           None | Offset]:
    """Search an offsets image for the colors specifying attachment points on the animation."""
    image_data = image.get_image_data()

    if image_data.format == 'BGRA':
        data = image_data.get_data()
        ridx = 2
        gidx = 1
        bidx = 0
    else:
        # Slow if not RGBA.
        data = image_data.get_data('RGBA')
        ridx = 0
        gidx = 1
        bidx = 2

    width, height = image.width, image.height

    r = None
    g = None
    b = None
    black = None

    for y in range(height):
        for x in range(width):
            pixel_start = (y * width + x) * 4  # Each pixel is represented by 4 bytes (RGBA)
            alpha = data[pixel_start + 3]

            if alpha != 0:  # Check if the alpha channel is not transparent
                pixel = data[pixel_start:pixel_start+3]
                if pixel == b'\x00\x00\x00':  # black
                    black = Offset(x, y)
                if pixel[ridx] == 255:  # red
                    r = Offset(x, y)
                if pixel[gidx] == 255:  # green
                    g = Offset(x, y)
                if pixel[bidx] == 255:  # blue
                    b = Offset(x, y)

    return r, g, b, black


def getShadowLocationFromPILImage(image) -> Offset | None:
    width, height = image.width, image.height

    for y in range(height):
        for x in range(width):
            pixel = image.getpixel((x, y))

            if pixel[3] != 0:  # Check if the alpha channel is not transparent
                if pixel == (255, 255, 255, 255):  # white
                    # White is shadow, abandon once we find one..
                    return Offset(x, y)

    return None


def getActionPointsFromPILImage(image) -> Tuple[None | Offset, None | Offset, None | Offset,
                                                                           None | Offset]:
    """Search an offsets image for the colors specifying attachment points on the animation."""
    width, height = image.width, image.height

    r = None
    g = None
    b = None
    black = None

    for y in range(height):
        for x in range(width):
            pixel = image.getpixel((x, y))

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
