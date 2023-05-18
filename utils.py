import pyglet


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