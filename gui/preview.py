"""GL preview surface, camera, texture grids and point marker rendering."""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pyglet
from PIL import Image, ImageDraw
from pyglet.gl import GL_COLOR_BUFFER_BIT, GL_DEPTH_BUFFER_BIT, glClear, glClearColor
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtGui import QKeyEvent, QWheelEvent
from PySide6.QtOpenGLWidgets import QOpenGLWidget

if TYPE_CHECKING:
    from types import TracebackType
    from pyglet.image import ImageData
    from main import AnimationEditor

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
    def __init__(
        self, glWidget: PygletWidget, position: tuple[float, float],
    ) -> None:
        self.glWidget = glWidget
        self.x, self.y = position
        self._zoom = 1.0

    @property
    def zoom(self) -> float:
        return self._zoom

    @zoom.setter
    def zoom(self, value: float) -> None:
        self._zoom = max(min(value, 4.0), 0.25)

    def __enter__(self) -> None:
        self.begin()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.end()

    def begin(self) -> None:
        x = -(self.glWidget.width() // 2) / self._zoom + self.x
        y = -(self.glWidget.height() // 3) / self._zoom + self.y

        view_matrix = self.glWidget.view.translate(
            (-x * self._zoom, -y * self._zoom, 0)
        )
        view_matrix = view_matrix.scale((self._zoom, self._zoom, 1))
        self.glWidget.view = view_matrix

    def end(self) -> None:
        x = -(self.glWidget.width() // 2) / self._zoom + self.x
        y = -(self.glWidget.height() // 3) / self._zoom + self.y

        view_matrix = self.glWidget.view.scale((1 / self._zoom, 1 / self._zoom, 1))
        view_matrix = view_matrix.translate((x * self._zoom, y * self._zoom, 0))
        self.glWidget.view = view_matrix



def create_point_marker(size: int, color: tuple[int, int, int, int]) -> ImageData:
    """A magenta plus with a one-pixel colored center and transparent corners."""
    import pyglet

    dimensions = (size, size)
    image = Image.new("RGBA", dimensions, (0, 0, 0, 0))

    draw = ImageDraw.Draw(image)

    center = (size // 2, size // 2)
    magenta = (255, 0, 255, color[3])
    draw.line((0, center[1], size - 1, center[1]), fill=magenta, width=1)
    draw.line((center[0], 0, center[0], size - 1), fill=magenta, width=1)
    draw.point(center, fill=color)

    return pyglet.image.ImageData(image.width, image.height, "RGBA", image.tobytes())



class QtPygletContext(pyglet.gl.base.Context):
    """Track pyglet objects in the native context owned by Qt."""

    def __init__(self, qt_context: QtGui.QOpenGLContext) -> None:
        super().__init__(None)
        self.canvas = qt_context

    def _safe_to_operate_on(self) -> bool:
        return (
            self.canvas is not None
            and QtGui.QOpenGLContext.currentContext() is self.canvas
            and super()._safe_to_operate_on()
        )

    def _safe_to_operate_on_object_space(self) -> bool:
        return self._safe_to_operate_on()



class PygletWidget(QOpenGLWidget):
    _default_vertex_source = """#version 150 core
        in vec4 position;

        uniform WindowBlock
        {
            mat4 projection;
            mat4 view;
        } window;

        void main()
        {
            gl_Position = window.projection * window.view * position;
        }
    """
    _default_fragment_source = """#version 330 core
        out vec4 color;

        void main()
        {
            color = vec4(1.0, 0.0, 0.0, 1.0);
        }
    """

    def __init__(
        self,
        width: int,
        height: int,
        parent: QtWidgets.QWidget | None,
        mainWindow: QtWidgets.QMainWindow,
        editor: AnimationEditor,
    ) -> None:
        super().__init__(parent)
        self.mainWindow = mainWindow
        self.setMinimumSize(width, height)
        self.editor = editor

        self._pyglet_context: QtPygletContext | None = None
        self._restore_preview = False
        self.lines: list[pyglet.shapes.Line] = []
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._pyglet_update)
        self.timer.setInterval(0)
        self.timer.start()

        self.focus_point = self.width() // 2, self.height() // 3

        self.camera = Camera(self, self.focus_point)
        self.cameraLocked = True
        self.panOffset = QtCore.QPointF()
        self.dragPosition: QtCore.QPointF | None = None
        self.showSpriteBounds = editor.settings.value(
            "preferences/showSpriteBounds", False, bool
        )
        self.spriteBounds: pyglet.shapes.Box | None = None
        self.setCameraLocked(
            editor.settings.value("preferences/cameraLocked", True, bool)
        )

        self.elapsed = 0

        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self._direction_keys = set()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        movement = (QtCore.Qt.Key.Key_W, QtCore.Qt.Key.Key_A,
                    QtCore.Qt.Key.Key_S, QtCore.Qt.Key.Key_D)
        shortcuts = (QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.AltModifier
                     | QtCore.Qt.KeyboardModifier.MetaModifier)
        if event.key() in movement and not event.modifiers() & shortcuts:
            if not event.isAutoRepeat():
                self._direction_keys.add(event.key())
                self._face_pressed_direction()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_O:
            self.makeCurrent()
            self.editor.ui.showActionPointsCheckBox.toggle()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() in self._direction_keys:
            if not event.isAutoRepeat():
                self._direction_keys.discard(event.key())
                self._face_pressed_direction()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def _face_pressed_direction(self) -> None:
        keys = self._direction_keys
        x = int(QtCore.Qt.Key.Key_D in keys) - int(QtCore.Qt.Key.Key_A in keys)
        y = int(QtCore.Qt.Key.Key_S in keys) - int(QtCore.Qt.Key.Key_W in keys)
        direction = {(0, 1): 0, (-1, 1): 1, (-1, 0): 2, (-1, -1): 3,
                     (0, -1): 4, (1, -1): 5, (1, 0): 6, (1, 1): 7}.get((x, y))
        if direction is not None and direction != self.editor.current_direction:
            self.makeCurrent()
            self.editor.choose_direction(direction)

    def focusOutEvent(self, event) -> None:
        self._direction_keys.clear()
        super().focusOutEvent(event)

    def setCameraLocked(self, locked: bool) -> None:
        locked = bool(locked)
        if locked != self.cameraLocked:
            self._update_camera_focus()
            self.panOffset = (
                QtCore.QPointF()
                if locked
                else QtCore.QPointF(
                    self.camera.x - self.width() // 2,
                    self.camera.y - self.height() // 3,
                )
            )
            self.cameraLocked = locked
            self.dragPosition = None
            self._update_camera_focus()
        self.setCursor(
            QtCore.Qt.CursorShape.ArrowCursor
            if locked
            else QtCore.Qt.CursorShape.OpenHandCursor
        )
        self.update()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if not self.cameraLocked and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.dragPosition = event.position()
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if (
            not self.cameraLocked
            and self.dragPosition is not None
            and event.buttons() & QtCore.Qt.MouseButton.LeftButton
        ):
            delta = event.position() - self.dragPosition
            self.panOffset += QtCore.QPointF(
                -delta.x() / self.camera.zoom, delta.y() / self.camera.zoom
            )
            self.dragPosition = event.position()
            self._update_camera_focus()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if (
            event.button() == QtCore.Qt.MouseButton.LeftButton
            and self.dragPosition is not None
        ):
            self.dragPosition = None
            self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        super().wheelEvent(event)
        self.makeCurrent()

        if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            if self.editor.sprite:
                if event.angleDelta().y() > 0:
                    self.editor.ui.spriteOpacitySlider.setValue(
                        max(self.editor.ui.spriteOpacitySlider.value() - 50, 0)
                    )
                else:
                    self.editor.ui.spriteOpacitySlider.setValue(
                        min(self.editor.ui.spriteOpacitySlider.value() + 50, 255)
                    )
        else:
            if event.angleDelta().y() > 0:
                self.camera.zoom *= 2.0
            else:
                self.camera.zoom /= 2.0

        self.view = pyglet.math.Mat4()
        event.accept()

    def _pyglet_update(self) -> None:
        # Tick the pyglet clock, so scheduled events can work.
        pyglet.clock.tick()

        # Force widget to update, otherwise paintGL will not be called.
        self.update()

    def paintGL(self) -> None:
        if self._pyglet_context is None:
            return
        self._activate_context()
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)

        self._update_camera_focus()
        self._update_guide_lines()
        self._update_sprite_bounds()
        self.editor.updateLayerPreview()
        with self.camera:
            self.batch.draw()

    def _update_camera_focus(self) -> None:
        if not self.cameraLocked:
            self.camera.x = self.width() // 2 + self.panOffset.x()
            self.camera.y = self.height() // 3 + self.panOffset.y()
        elif self.camera.zoom > 1 and self.editor.sprite is not None:
            self.camera.x, self.camera.y = self.editor.sprite.position[:2]
        else:
            self.camera.x = self.width() // 2
            self.camera.y = self.height() // 3

    def _update_guide_lines(self) -> None:
        if not self.lines:
            return
        zoom = self.camera.zoom
        # Convert viewport edges back to world coordinates
        # This way origin spans the view at every zoom level.
        left = self.camera.x - (self.width() // 2) / zoom
        bottom = self.camera.y - (self.height() // 3) / zoom
        horizontal, vertical = self.lines
        horizontal.position = (left - 1 / zoom, self.height() // 3)
        horizontal.x2 = left + (self.width() + 1) / zoom
        horizontal.y2 = self.height() // 3
        vertical.position = (self.width() // 2, bottom - 1 / zoom)
        vertical.x2 = self.width() // 2
        vertical.y2 = bottom + (self.height() + 1) / zoom
        horizontal.width = vertical.width = 1 / zoom

    def _update_sprite_bounds(self) -> None:
        sprite = self.editor.sprite
        if sprite is None or not self.showSpriteBounds:
            if self.spriteBounds is not None:
                self.spriteBounds.visible = False
            return
        if self.spriteBounds is None:
            self.spriteBounds = pyglet.shapes.Box(
                0,
                0,
                sprite.width,
                sprite.height,
                color=(255, 220, 0),
                batch=self.batch,
                group=pyglet.graphics.Group(4),
            )
        zoom = self.camera.zoom
        viewport = (pyglet.gl.GLint * 4)()
        pyglet.gl.glGetIntegerv(pyglet.gl.GL_VIEWPORT, viewport)
        scale_x = viewport[2] / max(1, self.width())
        scale_y = viewport[3] / max(1, self.height())
        zoom_x, zoom_y = zoom * scale_x, zoom * scale_y
        left = sprite.x - sprite.image.anchor_x * sprite.scale * sprite.scale_x
        bottom = sprite.y - sprite.image.anchor_y * sprite.scale * sprite.scale_y
        origin_x = viewport[0] + (self.width() // 2) * scale_x
        origin_y = viewport[1] + (self.height() // 3) * scale_y
        # Snap all four outer edges in screen space. Inverse zoom alone keeps
        # the nominal thickness constant but leaves edges between pixel boundaries.
        screen_left = round((left - self.camera.x) * zoom_x + origin_x)
        screen_bottom = round((bottom - self.camera.y) * zoom_y + origin_y)
        screen_right = max(screen_left + 1, round((left + sprite.width - self.camera.x) * zoom_x + origin_x))
        screen_top = max(screen_bottom + 1, round((bottom + sprite.height - self.camera.y) * zoom_y + origin_y))
        self.spriteBounds.position = (
            (screen_left - origin_x) / zoom_x + self.camera.x,
            (screen_bottom - origin_y) / zoom_y + self.camera.y,
        )
        self.spriteBounds.width = (screen_right - screen_left) / zoom_x
        self.spriteBounds.height = (screen_top - screen_bottom) / zoom_y
        self.spriteBounds.thickness = 1 / max(zoom_x, zoom_y)
        self.spriteBounds.visible = True

    def resizeGL(self, width: int, height: int) -> None:
        self._activate_context()
        self.projection = pyglet.math.Mat4.orthogonal_projection(
            0, width, 0, height, -255, 255
        )

        self.viewport = 0, 0, width, height

        self.focus_point = width // 2, height // 3

        for line in self.lines:
            line.delete()
        self.lines = [
            pyglet.shapes.Line(
                0,
                self.height() // 3,
                self.width(),
                self.height() // 3,
                batch=self.batch,
            ),
            pyglet.shapes.Line(
                self.width() // 2, 0, self.width() // 2, self.height(), batch=self.batch
            ),
        ]

        if self.editor.sprite:
            frame = self.editor.displayed_anim_frame or self.editor.current_anim_frame
            self.editor.sprite.position = self.editor.get_sprite_position(frame)
            self.editor.shadow.position = self.editor.get_shadow_position(frame)
            self.editor.updateActionPointPreview(frame)
            self.editor.updateOnionPreview(frame)
        elif self._restore_preview:
            self._restore_preview = False
            self.editor.set_animation()
        self._update_camera_focus()

    def initializeGL(self) -> None:
        """Call anything that needs a context to be created."""
        self._pyglet_context = QtPygletContext(self.context())
        self._activate_context()
        self.context().aboutToBeDestroyed.connect(self.cleanup_gl_resources)
        self._projection_matrix = pyglet.math.Mat4()
        self._view_matrix = pyglet.math.Mat4()

        self.batch = pyglet.graphics.Batch()

        try:
            self._default_program = pyglet.graphics.shader.ShaderProgram(
                pyglet.graphics.shader.Shader(self._default_vertex_source, "vertex"),
                pyglet.graphics.shader.Shader(
                    self._default_fragment_source, "fragment"
                ),
            )
        except pyglet.graphics.shader.ShaderException:
            self.error_dialog = QtWidgets.QMessageBox()
            self.error_dialog.setWindowTitle("Error")
            self.error_dialog.setIcon(QtWidgets.QMessageBox.Icon.Critical)
            self.error_dialog.setText(
                f"Could not compile shader. Requires OpenGL 3.3 capability."
            )
            self.error_dialog.exec()
            sys.exit(app.exec())

        self.ubo = self._default_program.uniform_blocks["WindowBlock"].create_ubo()

        glClearColor(0.5, 0.5, 0.5, 1.0)

        self.view = pyglet.math.Mat4()
        self.projection = pyglet.math.Mat4.orthogonal_projection(
            0, self.width(), 0, self.height(), -255, 255
        )
        self.viewport = 0, 0, self.width(), self.height()

    def _activate_context(self) -> None:
        if self._pyglet_context is not None:
            self._pyglet_context.set_current()

    def makeCurrent(self) -> None:
        super().makeCurrent()
        self._activate_context()

    def doneCurrent(self) -> None:
        if pyglet.gl.current_context is self._pyglet_context:
            pyglet.gl.current_context = None
            pyglet.gl.gl_info.remove_active_context()
        super().doneCurrent()

    def cleanup_gl_resources(self) -> None:
        """Release GPU objects before Qt destroys their native context.

        VertexList.delete only frees slots in a batch. Its domains still own
        buffers and VAOs, so those must also be released explicitly.
        """
        context = self._pyglet_context
        if context is None:
            return
        self.makeCurrent()
        editor = self.editor
        self._restore_preview = editor.sprite is not None
        textures: dict[int, pyglet.image.Texture] = {}

        def remember_texture(texture: pyglet.image.AbstractImage | None) -> None:
            while isinstance(texture, pyglet.image.TextureRegion):
                texture = texture.owner
            if isinstance(texture, pyglet.image.Texture):
                textures[id(texture)] = texture

        images = [
            editor.sheet_image,
            editor.action_pt_image,
            editor.shadow_image,
            editor.action_point_marker,
            *editor._action_point_markers.values(),
            *editor._shadow_display_images.values(),
        ]
        for grid in (editor.image_grid, editor.action_grid):
            if grid is not None:
                images.extend(grid._items or [])
                remember_texture(grid._texture_grid)
                grid._texture_grid = None
        for image in images:
            if image is not None:
                remember_texture(image)
                for name in ("_current_texture", "_current_mipmap_texture"):
                    remember_texture(getattr(image, name, None))
                    if hasattr(image, name):
                        setattr(image, name, None)
        for sprite in (editor.sprite, editor.shadow, *editor.ap_sprites, *editor.onionSprites):
            if sprite is not None:
                remember_texture(sprite.image)
                sprite.delete()
        editor.sprite = editor.shadow = None
        editor.left_hand = editor.center = editor.right_hand = editor.head = None
        editor.shadow_point = None
        editor.ap_sprites.clear()
        editor.onionSprites.clear()
        for line in self.lines:
            line.delete()
        self.lines.clear()
        if self.spriteBounds is not None:
            self.spriteBounds.delete()
            self.spriteBounds = None
        for domains in self.batch.group_map.values():
            for domain in domains.values():
                domain.vao.delete()
                buffers = {id(buffer): buffer for buffer, _ in domain.buffer_attributes}
                if hasattr(domain, "index_buffer"):
                    buffers[id(domain.index_buffer)] = domain.index_buffer
                for buffer in buffers.values():
                    buffer.delete()
        self.batch.group_map.clear()
        self.batch.group_children.clear()
        self.batch.top_groups.clear()
        self.batch._draw_list.clear()
        self.batch = None
        self.ubo.buffer.delete()
        self.ubo = None
        programs = {
            id(program): program for program in context._cached_programs.values()
        }
        for value in vars(context.object_space).values():
            if isinstance(value, pyglet.graphics.shader.ShaderProgram):
                programs[id(value)] = value
        programs[id(self._default_program)] = self._default_program
        for program in programs.values():
            if program.id is not None:
                program.delete()
        self._default_program = None
        context._cached_programs.clear()
        for texture in textures.values():
            if texture.id is not None:
                texture.delete()
        # Flush objects that pyglet queued while another tab was current.
        context.set_current()
        self.context().aboutToBeDestroyed.disconnect(self.cleanup_gl_resources)
        self.doneCurrent()
        context.canvas = None
        self._pyglet_context = None

    @property
    def viewport(self) -> tuple[int, int, int, int]:
        return self._viewport

    @viewport.setter
    def viewport(self, values: tuple[int, int, int, int]) -> None:
        self._viewport = values
        pr = 1.0
        x, y, w, h = values
        pyglet.gl.glViewport(int(x * pr), int(y * pr), int(w * pr), int(h * pr))

    @property
    def projection(self) -> pyglet.math.Mat4:
        return self._projection_matrix

    @projection.setter
    def projection(self, matrix: pyglet.math.Mat4) -> None:
        with self.ubo as window_block:
            window_block.projection[:] = matrix

        self._projection_matrix = matrix

    @property
    def view(self) -> pyglet.math.Mat4:
        return self._view_matrix

    @view.setter
    def view(self, matrix: pyglet.math.Mat4) -> None:

        with self.ubo as window_block:
            window_block.view[:] = matrix

        self._view_matrix = matrix
