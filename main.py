from __future__ import annotations

import sys

if __name__ == "__main__" and len(sys.argv) > 1:
    from cli import main as cli_main

    sys.exit(cli_main())

import copy
import os
import pathlib
import traceback
import xml.etree.ElementTree as ElementTree
from functools import partial, wraps
from hashlib import sha256
from typing import TYPE_CHECKING, TypeVar

import pyglet
from PIL import Image

pyglet.options.com_mta = False
import warnings

warnings.simplefilter("ignore", UserWarning)
sys.coinit_flags = 2

from pyglet.gl import (
    GL_COLOR_BUFFER_BIT,
    GL_DEPTH_BUFFER_BIT,
    GL_NEAREST,
    glClear,
    glClearColor,
)
from pyglet.math import clamp
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QFileInfo, QSettings
from PySide6.QtGui import QImage, QKeyEvent, QSurfaceFormat, QWheelEvent
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QFileDialog, QInputDialog, QListWidgetItem

from conversion import AnimationSerialization, SpriteDocument
from data import (
    FD_STR,
    VERSION,
    ActionPoints,
    AnimationSequence,
    AnimFrame,
    AnimGroup,
    BatchFrameEdit,
    Offset,
    SheetFormat, EditState,
)
from formats import detect_format, validate_export
from gui.batchadd import ActionAliasDialog
from gui.batchedit import BatchEditDialog
from gui.editor import Ui_MainWindow
from gui.info import SpriteInfoDialog
from gui.textures import TopLeftGrid
from utils import (
    Camera,
    FormatError,
    create_plus_image,
    shadow_image,
    template_image,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from os import PathLike
    from types import TracebackType

    from gui.editor import SheetFramePreview

pyglet.image.Texture.default_min_filter = GL_NEAREST
pyglet.image.Texture.default_mag_filter = GL_NEAREST


class SpriteEditCommand(QtGui.QUndoCommand):
    def __init__(
        self, editor: AnimationEditor, text: str, before: EditState, after: EditState
    ) -> None:
        super().__init__(text)
        self.editor = editor
        self.before = before
        self.after = after
        self.firstRedo = True

    def undo(self) -> None:
        self.editor.restoreEditState(self.before)

    def redo(self) -> None:
        if self.firstRedo:
            self.firstRedo = False
        else:
            self.editor.restoreEditState(self.after)


EditResult = TypeVar("EditResult")


def undoable(text: str) -> Callable[
    [Callable[..., EditResult]], Callable[..., EditResult]
]:
    """Record one complete editor operation, including nested edit handlers."""
    def decorate(method: Callable[..., EditResult]) -> Callable[..., EditResult]:
        @wraps(method)
        def record(editor: AnimationEditor, *args: object, **kwargs: object) -> EditResult:
            if editor.loaded_tree is None or editor.editDepth or editor.restoringEdit:
                return method(editor, *args, **kwargs)
            before = editor.captureEditState()
            editor.editDepth += 1
            try:
                result = method(editor, *args, **kwargs)
            finally:
                editor.editDepth -= 1
            after = editor.captureEditState()
            if before.signature != after.signature:
                editor.undoStack.push(SpriteEditCommand(editor, text, before, after))
            return result
        return record
    return decorate


class LoadedSheetFrame(QListWidgetItem):
    def __init__(
        self,
        text: str,
        idx: int,
        image: pyglet.image.AbstractImage,
        label: SheetFramePreview,
        editor: AnimationEditor,
    ) -> None:
        super().__init__(text)
        self.image: pyglet.image.AbstractImage = image
        self.label = label
        self.editor = editor
        self.idx = idx

        data = self.image.get_image_data().get_data("RGBA", -self.image.width * 4)
        self.qim = QImage(
            data, image.width, image.height, QImage.Format.Format_RGBA8888
        ).copy()
        editor.sheet_preview_images[idx] = self.qim

    # Define the double-click event handler
    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent | None) -> None:
        self.editor.add_new_animation_frame(self.idx)

    def mouseClickEvent(self, event: QtGui.QMouseEvent | None) -> None:
        self.editor.selectSheetFrame(self.idx)


class AnimGroupItem(QListWidgetItem):
    def __init__(self, anim_group: AnimGroup, editor: AnimationEditor) -> None:
        self.anim_group = anim_group
        self.editor = editor
        text = self._get_anim_text()
        super().__init__(text)
        self.editor = editor

    def update_text(self) -> None:
        self.setText(self._get_anim_text())

    def _get_anim_text(self) -> str:
        text = f"{self.anim_group.idx if self.anim_group.idx >= 0 else '?'}. {self.anim_group.name}"
        if self.anim_group.copy_name:
            text += f" [{self.anim_group.copy_name}]"

        if self.anim_group.modified:
            text += " *"

        return text

    def mouseClickEvent(self, event: QtGui.QMouseEvent | None) -> None:
        self.editor.current_anim_group = self.anim_group

        self.editor.current_sequence = self.anim_group.directions[
            self.editor.current_direction
        ]

        self.editor.clear_anim_frame()

        if self.editor.current_sequence.frames:
            self.editor.current_anim_frame = self.editor.current_sequence.frames[0]

            self.editor.set_sequence_list()

            self.editor.ui.animationFrameList.setCurrentRow(0)

            self.editor.set_anim_frame_values(self.editor.current_anim_frame)

            self.editor.ui.statusBar.showMessage(
                f"Active action: {self.anim_group.name}."
            )
        self.editor.updateBatchEditDialog()


class AnimFrameItem(QListWidgetItem):
    def __init__(self, anim_frame: AnimFrame, editor: AnimationEditor) -> None:
        self.anim_frame = anim_frame
        self.editor = editor
        text = self._get_text()
        super().__init__(text)
        self.update_text()

    def update_text(self) -> None:
        self.setText(self._get_text())

    def _get_text(self) -> str:
        text = f"#{self.anim_frame.frame_index}"
        rush, hit, returned = self.editor.current_anim_group.event_frames(
            self.editor.current_sequence
        )

        if self.anim_frame.idx == hit:
            text += " (HF)"
        if self.anim_frame.idx == rush:
            text += " (RF)"
        if self.anim_frame.idx == returned:
            text += " (RTF)"

        return text

    def mouseClickEvent(self, event: QtGui.QMouseEvent | None) -> None:
        self.editor.set_anim_frame_values(self.anim_frame)

        if not self.editor.animating:
            if self.anim_frame == self.editor.current_anim_frame:
                return

            self.editor.current_anim_frame = self.anim_frame
            with QtCore.QSignalBlocker(self.editor.ui.frameSlider):
                self.editor.ui.frameSlider.setValue(self.anim_frame.idx)
            self.editor.set_animation()


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

    def keyPressEvent(self, event: QKeyEvent) -> None:
        super().keyPressEvent(event)
        self.makeCurrent()

        if event.key() == QtCore.Qt.Key.Key_O:
            self.editor.ui.showActionPointsCheckBox.toggle()
        elif event.key() == QtCore.Qt.Key.Key_S:
            if self.editor.shadow:
                self.editor.shadow.visible = not self.editor.shadow.visible

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
                group=pyglet.graphics.Group(3),
            )
        self.spriteBounds.position = (
            sprite.x - sprite.image.anchor_x * sprite.scale * sprite.scale_x,
            sprite.y - sprite.image.anchor_y * sprite.scale * sprite.scale_y,
        )
        self.spriteBounds.width = sprite.width
        self.spriteBounds.height = sprite.height
        self.spriteBounds.thickness = 1 / self.camera.zoom
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
            self.editor.sprite.position = self.editor.get_sprite_position()
            self.editor.shadow.position = self.editor.get_shadow_position()
            self.editor.updateActionPointPreview(self.editor.current_anim_frame)
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
        for sprite in (editor.sprite, editor.shadow, *editor.ap_sprites):
            if sprite is not None:
                remember_texture(sprite.image)
                sprite.delete()
        editor.sprite = editor.shadow = None
        editor.left_hand = editor.center = editor.right_hand = editor.head = None
        editor.shadow_point = None
        editor.ap_sprites.clear()
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


REDUCE_RUSH_FRAMES = False


class AnimationEditor(AnimationSerialization):
    ui: Ui_MainWindow
    shadow_image: pyglet.image.AbstractImage | None
    sprite: pyglet.sprite.Sprite | None
    ANIMATION_SPEED_SLIDER_VALUES = (0.1, 0.25, 0.5, 1, 2)
    MAX_RECENT = 5

    def __init__(
        self,
        app: QtWidgets.QApplication,
        window: QtWidgets.QMainWindow,
        ui: Ui_MainWindow,
        document_host: SpriteEditorWindow | None = None,
    ) -> None:
        self._configure_open_gl()
        self.app = app
        self.window = window
        self.ui = ui
        self.document_host = document_host
        self.groups: list[AnimGroup] = []
        self.file_name = ""
        self.sheet_format: SheetFormat | None = None
        self.shadow_size_present = True
        self.loaded_shadow_size = 0
        self.loaded_tree: ElementTree.ElementTree | None = None
        self.undoStack = QtGui.QUndoStack(window)
        self.undoStack.setUndoLimit(100)
        self.editDepth = 0
        self.restoringEdit = False
        self.savedEditSignature: tuple[object, ...] | None = None
        self.batchEditDialog: BatchEditDialog | None = None
        self.editMenu = QtWidgets.QMenu("Edit", window)
        self.undoAction = self.undoStack.createUndoAction(window, "Undo")
        self.redoAction = self.undoStack.createRedoAction(window, "Redo")
        self.editMenu.addActions([self.undoAction, self.redoAction])
        self.editMenu.addSeparator()
        self.copyAnimationSettingsAction = self.editMenu.addAction(
            "Copy Animation Settings", self.copyAnimationSettings
        )
        self.pasteAnimationSettingsAction = self.editMenu.addAction(
            "Paste Animation Settings", self.pasteAnimationSettings
        )
        self.copyAnimationSettingsAction.setEnabled(False)
        self.pasteAnimationSettingsAction.setEnabled(False)
        self.animationSettingsClipboard: AnimGroup | None = None
        self.editMenu.aboutToShow.connect(self.updateAnimationSettingsActions)
        self.editMenu.addSeparator()
        self.batchEditAction = self.editMenu.addAction(
            "Batch Edit Frames...", self.openBatchEdit
        )
        self.editMenu.aboutToShow.connect(
            lambda: self.batchEditAction.setEnabled(
                self.current_anim_group is not None
                and any(sequence.frames for sequence in self.current_anim_group.directions)
            )
        )
        if document_host is None:
            self.undoAction.setShortcuts(QtGui.QKeySequence.StandardKey.Undo)
            self.redoAction.setShortcuts(QtGui.QKeySequence.StandardKey.Redo)
            window.menuBar().addMenu(self.editMenu)

        self.settings = QSettings("MDFrameEditor", "Frame Editor")
        self.recent_files: list[str] = self.settings.value("recent", [], list)
        self.artworkPath: pathlib.Path | None = None
        # Hash for file change.
        self.artworkDigest: bytes | None = None
        self.artworkRetryCount = 0
        self.artworkReloadEnabled = self.settings.value(
            "preferences/autoReloadArtwork", False, bool
        )
        self.artworkWatcher = QtCore.QFileSystemWatcher(window)
        self.artworkReloadTimer = QtCore.QTimer(window)
        self.artworkReloadTimer.setSingleShot(True)
        self.artworkReloadTimer.timeout.connect(self.reloadChangedArtwork)
        self.artworkWatcher.fileChanged.connect(self.queueArtworkReload)
        self.artworkWatcher.directoryChanged.connect(self.queueArtworkReload)
        self.ui.sheetFramePicture.includeWhitespace = self.settings.value(
            "preferences/includeWhitespace", False, bool
        )
        self.ui.sheetFramePicture.showBounds = self.settings.value(
            "preferences/showSpriteBounds", False, bool
        )

        self.enable_trim = self.settings.value("trim", True, bool)
        self.enable_collapse = self.settings.value("collapse", True, bool)

        self.ui.actionCollapse_Singles.setChecked(self.enable_collapse)
        self.ui.actionTrim_Copies.setChecked(self.enable_trim)

        self.ui.actionCollapse_Singles.changed.connect(lambda: self.save_collapse())
        self.ui.actionTrim_Copies.changed.connect(lambda: self.save_trim())

        self.ui.actionExit.triggered.connect(lambda: self.exit_application())

        self.recentFileActions: list[QtGui.QAction] = []

        for i in range(self.MAX_RECENT):
            action = QtGui.QAction(self.window, visible=False)
            action.triggered.connect(
                lambda checked=False, a=action: self.load_recent_file(a)
            )
            self.recentFileActions.append(action)
            self.ui.menuRecent.addAction(action)

        self._update_recent_actions()

        self.scale = 2.0  # default sprite scaling.

        self.anim_speed = 1 / 60
        self.sheet_image: pyglet.image.ImageData | None = None
        self.image_grid: TopLeftGrid | None = None

        self.action_pt_image: pyglet.image.ImageData | None = None
        self.action_grid: TopLeftGrid | None = None
        self.action_points: dict[int, ActionPoints] = {}
        self.selected_sheet_frame: int | None = None
        self.sheet_preview_images: dict[int, QImage] = {}
        self.loaded_action_points: dict[int, ActionPoints] = {}

        self.shadow_image = self._shadow_image_data(template_image())
        self._shadow_size = 0
        self._shadow_display_images: dict[int, pyglet.image.ImageData] = {}

        self.sprite = None
        self.shadow: pyglet.sprite.Sprite | None = None

        self.marker_size = 5
        self.action_point_marker = create_plus_image(
            self.marker_size, (255, 255, 255, 255)
        )
        self.action_point_marker.anchor_x = self.marker_size // 2
        self.action_point_marker.anchor_y = self.marker_size // 2
        self.left_hand: pyglet.sprite.Sprite | None = None
        self.center: pyglet.sprite.Sprite | None = None
        self.right_hand: pyglet.sprite.Sprite | None = None
        self.head: pyglet.sprite.Sprite | None = None
        self.shadow_point: pyglet.sprite.Sprite | None = None

        self.ap_sprites: list[pyglet.sprite.Sprite] = []

        self.current_direction = 0
        self.animating = False
        self.newWindow: ActionAliasDialog | None = None

        self.single_loaded: bool | None = None

        # Set when opened for non-XML access.
        self.frame_width = 0
        self.frame_height = 0
        self.shadow_size = 0

        self.copied_sequence: AnimationSequence | None = None

        # self.window.setFixedSize(670, 836)

        self.current_sequence: AnimationSequence | None = None
        self.current_anim_frame: AnimFrame | None = None
        self.displayed_anim_frame: AnimFrame | None = None
        self.current_anim_group: AnimGroup | None = None

        self.ui.actionLoad.triggered.connect(lambda: self._load_animation_dialog())

        self.ui.playButton.clicked.connect(lambda: self.play_animation())

        self.ui.frameDownReorderButton.clicked.connect(lambda: self.move_frame_down())
        self.ui.frameUpReorderButton.clicked.connect(lambda: self.move_frame_up())

        self.ui.animationFrameList.itemClicked.connect(
            lambda item: item.mouseClickEvent(None)
        )

        self.directionButtons = [
            self.ui.buttonDown,
            self.ui.buttonDownLeft,
            self.ui.buttonLeft,
            self.ui.buttonUpLeft,
            self.ui.buttonUp,
            self.ui.buttonUpRight,
            self.ui.buttonRight,
            self.ui.buttonDownRight,
        ]

        for dir_idx, button in enumerate(self.directionButtons):
            button.clicked.connect(
                partial(self.set_direction, self.directionButtons.index(button))
            )
            button.setCheckable(True)

        self.ui.buttonDown.toggle()

        self.openGLWidget = self.ui.openGLWidget = PygletWidget(
            240, 220, self.ui.verticalFrame_3, self.window, self
        )
        self.openGLWidget.setMinimumSize(QtCore.QSize(240, 220))
        self.openGLWidget.setMaximumSize(QtCore.QSize(16777215, 16777215))
        self.openGLWidget.setObjectName("openGLWidget")
        self.ui.gridLayout.addWidget(self.openGLWidget, 0, 0, 1, 1)

        self.ui.frameSlider.setMaximum(0)

        self.ui.frameIndexSpinBox.valueChanged.connect(
            lambda: self.frame_index_changed()
        )

        self.ui.copySequenceButton.clicked.connect(lambda: self.copy_sequence())
        self.ui.pasteSequenceButton.clicked.connect(lambda: self.paste_sequence())

        self.ui.xSpinBox.valueChanged.connect(lambda: self.sprite_offset_changed())
        self.ui.ySpinBox.valueChanged.connect(lambda: self.sprite_offset_changed())

        self.ui.xShadowSpinbox.valueChanged.connect(
            lambda: self.shadow_offset_changed()
        )
        self.ui.yShadowSpinBox.valueChanged.connect(
            lambda: self.shadow_offset_changed()
        )

        self.ui.durationSpinBox.valueChanged.connect(lambda: self.duration_changed())
        self.ui.mirroredCheckbox.clicked.connect(lambda: self.flip_changed())

        self.ui.frameSlider.valueChanged.connect(lambda: self.slider_change())
        self.ui.frameDuplicateButton.clicked.connect(lambda: self.duplicate_frame())
        self.ui.frameDeleteButton.clicked.connect(lambda: self.delete_selected_frames())

        self.ui.loadedSheetFrameList.itemDoubleClicked.connect(
            lambda item: item.mouseDoubleClickEvent(None)
        )
        self.ui.loadedSheetFrameList.currentRowChanged.connect(
            lambda row: self.selectSheetFrame(
                self.ui.loadedSheetFrameList.item(row).idx if row >= 0 else None
            )
        )

        self.ui.actionListWidget.itemActivated.connect(
            lambda item: item.mouseClickEvent(None)
        )

        self.ui.animationSpeedSlider.valueChanged.connect(
            lambda: self.change_animation_speed()
        )

        self.ui.actionAddButton.clicked.connect(lambda: self.open_add_action())
        self.ui.actionDuplicateButton.clicked.connect(lambda: self.duplicate_action())
        self.ui.actionDeleteButton.clicked.connect(lambda: self.delete_action())

        self.ui.actionSave.triggered.connect(lambda: self.save_action_trigger())
        self.ui.actionSave_As.triggered.connect(lambda: self.save_as_action_trigger())

        self.ui.defaultFrameButton.clicked.connect(lambda: self.default_frame_click())

        self.ui.actionAdd_Action_Copy.triggered.connect(lambda: self.open_batch_add())

        self.ui.returnPointButton.clicked.connect(lambda: self.set_return_point())
        self.ui.hitPointButton.clicked.connect(lambda: self.set_hit_point())
        self.ui.rushPointButton.clicked.connect(lambda: self.set_rush_point())

        self.ui.actionExportAll_Animations.triggered.connect(
            lambda: self.export_multiple_sheets()
        )
        self.ui.actionExportSingle_Animation.triggered.connect(
            lambda: self.export_single_sheet()
        )

        self.ui.shadowSizeComboBox.currentIndexChanged.connect(self.apply_shadow_size)
        self.ui.previewShadowCheckBox.toggled.connect(self._refresh_shadow_display)
        self.ui.showActionPointsCheckBox.toggled.connect(self.setActionPointsVisible)
        self.ui.spriteOpacitySlider.valueChanged.connect(
            partial(self.setPreviewOpacity, "sprite")
        )
        self.ui.shadowOpacitySlider.valueChanged.connect(
            partial(self.setPreviewOpacity, "shadow")
        )
        self.ui.actionPointsOpacitySlider.valueChanged.connect(
            partial(self.setPreviewOpacity, "actionPoints")
        )
        for pointName in ("leftHand", "rightHand", "head", "center"):
            for spinBox in self.ui.actionPointSpinBoxes[pointName]:
                spinBox.valueChanged.connect(
                    partial(self.actionPointChanged, pointName)
                )

    def captureEditState(self) -> EditState:
        """Snapshot editable metadata without copying artwork or GL resources."""
        groups, points = copy.deepcopy((self.groups, self.action_points))
        return EditState(
            groups, points, self.shadow_size, self.shadow_size_present,
            self.ui.actionListWidget.currentRow(),
            self.ui.animationFrameList.currentRow(), self.current_direction,
            self.selected_sheet_frame, SpriteEditorWindow._document_state(self),
            tuple(sorted(
                self.ui.animationFrameList.row(item)
                for item in self.ui.animationFrameList.selectedItems()
            )),
        )

    def restoreEditState(self, state: EditState) -> None:
        """Restore metadata and selection while retaining preview preferences."""
        self.restoringEdit = True
        playing = self.animating
        spriteVisible = self.sprite.visible if self.sprite is not None else True
        shadowVisible = self.shadow.visible if self.shadow is not None else True
        pyglet.clock.unschedule(self._playing_animation)
        self.animating = False
        try:
            if self.sprite is not None:
                self.openGLWidget.makeCurrent()
                self.sprite.delete()
                self.shadow.delete()
                self.sprite = self.shadow = None
            for marker in self.ap_sprites:
                marker.delete()
            self.ap_sprites.clear()
            self.left_hand = self.center = self.right_hand = self.head = (
                self.shadow_point
            ) = None
            self.displayed_anim_frame = None
            self.current_anim_group = self.current_sequence = None
            self.clear_anim_frame()
            self.groups, self.action_points = copy.deepcopy(
                (state.groups, state.action_points)
            )
            self.shadow_size_present = state.shadow_size_present
            self.shadow_size = state.shadow_size
            if self.savedEditSignature is not None:
                current = SpriteEditorWindow._document_state(self)
                savedGroups = {group[:2]: group for group in self.savedEditSignature[2]}
                savedPoints = dict(self.savedEditSignature[3])
                changedPoints = {
                    index for index, points in current[3]
                    if points != savedPoints.get(index)
                } | (savedPoints.keys() - self.action_points.keys())
                for group, signature in zip(self.groups, current[2]):
                    group.modified = (
                        current[:2] != self.savedEditSignature[:2]
                        or signature != savedGroups.get(signature[:2])
                        or any(
                            frame.frame_index in changedPoints
                            for sequence in group.directions for frame in sequence.frames
                        )
                    )
            self.current_direction = state.direction
            for index, button in enumerate(self.directionButtons):
                button.setChecked(index == state.direction)
            with (
                QtCore.QSignalBlocker(self.ui.actionListWidget),
                QtCore.QSignalBlocker(self.ui.animationFrameList),
                QtCore.QSignalBlocker(self.ui.frameSlider),
            ):
                self.ui.actionListWidget.clear()
                self.ui.animationFrameList.clear()
                for group in self.groups:
                    self.ui.actionListWidget.addItem(AnimGroupItem(group, self))
                self.ui.actionListWidget.setCurrentRow(state.action_row)
                if 0 <= state.action_row < len(self.groups):
                    self.current_anim_group = self.groups[state.action_row]
                    self.current_sequence = self.current_anim_group.directions[state.direction]
                    frames = self.current_sequence.frames
                    row = min(max(state.frame_row, 0), len(frames) - 1)
                    if frames:
                        self.current_anim_frame = frames[row]
                    self.set_sequence_list()
                    self.ui.animationFrameList.setCurrentRow(row)
                    self.ui.frameSlider.setValue(max(row, 0))
                    if frames:
                        self.set_anim_frame_values(frames[row])
                self.ui.animationFrameList.clearSelection()
                for row in state.frame_selection:
                    item = self.ui.animationFrameList.item(row)
                    if item is not None:
                        item.setSelected(True)
            self.selectSheetFrame(state.sheet_frame)
            if self.sprite is not None:
                self.sprite.visible = spriteVisible
                self.shadow.visible = shadowVisible
            if playing and self.current_anim_frame is not None:
                self.animating = True
                if self.document_host is None or self.document_host.current_editor is self:
                    pyglet.clock.schedule_once(
                        self._playing_animation, self.anim_speed * self.current_anim_frame.duration
                    )
            self.openGLWidget.update()
        finally:
            self.restoringEdit = False

    def setPreviewOpacity(self, target: str, opacity: int) -> None:
        getattr(self.ui, target + "OpacityLabel").setText(
            f"{round(opacity * 100 / 255)}%"
        )
        if target == "actionPoints":
            if self.ap_sprites:
                self.openGLWidget.makeCurrent()
                for marker in self.ap_sprites:
                    marker.opacity = opacity
                self.openGLWidget.update()
            self.refreshSheetFramePreview()
            return
        sprite = getattr(self, target)
        if sprite is not None:
            self.openGLWidget.makeCurrent()
            sprite.opacity = opacity
            self.openGLWidget.update()

    def syncActionPointControls(self, animFrame: AnimFrame | None) -> None:
        self.ui.shadowOffsetScopeLabel.setText(
            f"Current Frame: {animFrame.idx if animFrame else '-'}"
        )
        self.selectSheetFrame(animFrame.frame_index if animFrame else None)

    def selectSheetFrame(self, index: int | None) -> None:
        if index is not None and (
            self.image_grid is None or not 0 <= index < len(self.image_grid)
        ):
            index = None
        self.selected_sheet_frame = index
        with QtCore.QSignalBlocker(self.ui.loadedSheetFrameList):
            row = next(
                (
                    row
                    for row in range(self.ui.loadedSheetFrameList.count())
                    if self.ui.loadedSheetFrameList.item(row).idx == index
                ),
                -1,
            )
            self.ui.loadedSheetFrameList.setCurrentRow(row)
        self.ui.sheetFrameScopeLabel.setText(
            f"Current Frame: {index if index is not None else '-'}"
        )
        body = self.action_points.get(index, ActionPoints())
        for pointName, spinBoxes in self.ui.actionPointSpinBoxes.items():
            fieldName = {"leftHand": "left_hand", "rightHand": "right_hand"}.get(
                pointName, pointName
            )
            offset = getattr(body, fieldName)
            for spinBox, value in zip(spinBoxes, offset.data()):
                with QtCore.QSignalBlocker(spinBox):
                    spinBox.setValue(value)
                    spinBox.setEnabled(index is not None)
        self.refreshSheetFramePreview()

    def refreshSheetFramePreview(self) -> None:
        index = self.selected_sheet_frame
        if index is None or self.image_grid is None:
            self.ui.sheetFramePicture.clear()
            return
        image = self.image_grid[index]
        qim = self.sheet_preview_images.get(index)
        if qim is None:
            data = image.get_image_data().get_data("RGBA", -image.width * 4)
            qim = QImage(
                data, image.width, image.height, QImage.Format.Format_RGBA8888
            ).copy()
            self.sheet_preview_images[index] = qim
        body = self.action_points.get(index)
        points = (
            []
            if body is None
            else [
                (point.x + image.width // 2, point.y + image.height // 2, color)
                for point, color in zip(
                    body.all_pos(), ((255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 0, 0))
                )
            ]
        )
        self.ui.sheetFramePicture.setFrame(
            qim,
            points,
            self.ui.showActionPointsCheckBox.isChecked(),
            self.ui.actionPointsOpacitySlider.value(),
        )

    def setArtworkReloadEnabled(self, enabled: bool) -> None:
        if self.artworkReloadEnabled == enabled:
            return
        self.artworkReloadEnabled = enabled
        self.updateArtworkWatch()
        if enabled:
            self.queueArtworkReload()
        else:
            self.artworkReloadTimer.stop()

    def updateArtworkWatch(self) -> None:
        paths = self.artworkWatcher.files() + self.artworkWatcher.directories()
        if paths:
            self.artworkWatcher.removePaths(paths)
        if self.artworkReloadEnabled and self.artworkPath is not None:
            # Watching the directory also catches editors replacing the PNG.
            paths = [str(self.artworkPath.parent)]
            if self.artworkPath.exists():
                paths.append(str(self.artworkPath))
            self.artworkWatcher.addPaths(paths)

    def trackArtwork(self) -> None:
        self.artworkReloadTimer.stop()
        self.artworkPath = None
        self.artworkDigest = None
        if self.sheet_format == SheetFormat.SINGLE and self.sheet_image is not None:
            self.artworkPath = pathlib.Path(self.file_name).resolve().with_name("Anim.png")
            self.artworkDigest = sha256(
                self.sheet_image.get_data("RGBA", -self.sheet_image.width * 4)
            ).digest()
        self.updateArtworkWatch()

    def queueArtworkReload(self, path: str = "") -> None:
        if self.artworkReloadEnabled and self.artworkPath is not None:
            self.artworkRetryCount = 0
            self.artworkReloadTimer.start(300)

    def reloadChangedArtwork(self) -> None:
        if not self.artworkReloadEnabled or self.artworkPath is None:
            return
        try:
            with Image.open(self.artworkPath) as source:
                image = source.convert("RGBA")
            pixels = image.tobytes()
        except OSError:
            # Saves may briefly leave an incomplete or missing file.
            self.artworkRetryCount += 1
            if self.artworkRetryCount <= 3:
                self.artworkReloadTimer.start(500)
            else:
                self.ui.statusBar.showMessage(
                    "Could not reload artwork. Keeping the current preview.", 5000
                )
            self.updateArtworkWatch()
            return
        self.updateArtworkWatch()
        if image.size != (self.sheet_image.width, self.sheet_image.height):
            self.ui.statusBar.showMessage(
                "Artwork dimensions changed. Reopen the sheet to load its new layout.", 5000
            )
            return
        digest = sha256(pixels).digest()
        if digest == self.artworkDigest:
            return
        if self.openGLWidget.isValid():
            self.openGLWidget.makeCurrent()
        selected = self.selected_sheet_frame
        scroll = self.ui.loadedSheetFrameList.verticalScrollBar().value()
        self.sheet_image = pyglet.image.ImageData(
            image.width, image.height, "RGBA", pixels, pitch=-image.width * 4
        )
        self.image_grid = TopLeftGrid(
            self.sheet_image, rows=self.image_grid.rows, columns=self.image_grid.columns
        )
        with QtCore.QSignalBlocker(self.ui.loadedSheetFrameList):
            self.ui.loadedSheetFrameList.clear()
            self.sheet_preview_images.clear()
            self._add_frames_from_grid()
        self.selectSheetFrame(selected)
        self.ui.loadedSheetFrameList.verticalScrollBar().setValue(scroll)
        frame = self.displayed_anim_frame or self.current_anim_frame
        if self.sprite is not None and frame is not None:
            spriteImage = self.image_grid[frame.frame_index]
            if frame.flip:
                spriteImage = spriteImage.get_texture().get_transform(flip_x=True)
                spriteImage.anchor_x = spriteImage.width // 2
                spriteImage.anchor_y = spriteImage.height // 2
            self.sprite.image = spriteImage
        self.artworkDigest = digest
        self.openGLWidget.update()
        self.ui.statusBar.showMessage("Artwork reloaded.", 3000)

    @undoable("Change Action Point")
    def actionPointChanged(self, pointName: str, value: int) -> None:
        index = self.selected_sheet_frame
        if index is None:
            return
        xSpinBox, ySpinBox = self.ui.actionPointSpinBoxes[pointName]
        offset = Offset(xSpinBox.value(), ySpinBox.value())
        fieldName = {"leftHand": "left_hand", "rightHand": "right_hand"}.get(
            pointName, pointName
        )
        body = self.action_points.setdefault(index, ActionPoints())
        if getattr(body, fieldName) == offset:
            return
        setattr(body, fieldName, offset)
        for groupItem in self._get_action_list_items():
            if any(
                other.frame_index == index
                for sequence in groupItem.anim_group.directions
                for other in sequence.frames
            ):
                groupItem.anim_group.modified = True
                groupItem.update_text()
        if not self.animating:
            self.set_animation()
        self.refreshSheetFramePreview()
        self.ui.statusBar.showMessage(
            "Action point updated for all frames using this sheet image.", 4000
        )

    def setActionPointsVisible(self, visible: bool) -> None:
        self.refreshSheetFramePreview()
        if self.sprite is not None:
            self.openGLWidget.makeCurrent()
            self.updateActionPointPreview(
                self.displayed_anim_frame or self.current_anim_frame
            )
            self.openGLWidget.update()

    @staticmethod
    def _shadow_image_data(image: Image.Image) -> pyglet.image.ImageData:
        data = pyglet.image.ImageData(
            image.width, image.height, "RGBA", image.tobytes(), pitch=-image.width * 4
        )
        data.anchor_x = image.width // 2
        data.anchor_y = image.height // 2
        return data

    @property
    def shadow_size(self) -> int:
        return self._shadow_size

    @shadow_size.setter
    def shadow_size(self, size: int) -> None:
        self._shadow_size = size
        if size in (0, 1, 2):
            with QtCore.QSignalBlocker(self.ui.shadowSizeComboBox):
                self.ui.shadowSizeComboBox.setCurrentIndex(size)
            self.ui.shadowSizeComboBox.setEnabled(self.loaded_tree is not None)
        if hasattr(self, "openGLWidget") and size in (0, 1, 2):
            self._refresh_shadow_display()

    def _display_shadow_image(self) -> pyglet.image.AbstractImage | None:
        if not self.ui.previewShadowCheckBox.isChecked():
            return self.shadow_image
        size = self.shadow_size
        if size not in self._shadow_display_images:
            self._shadow_display_images[size] = self._shadow_image_data(
                shadow_image(size, black=True)
            )
        return self._shadow_display_images[size]

    def _refresh_shadow_display(self) -> None:
        if self.shadow is not None:
            self.openGLWidget.makeCurrent()
            self.shadow.image = self._display_shadow_image()
            self.openGLWidget.update()

    @undoable("Change Shadow Size")
    def apply_shadow_size(self, size: int) -> None:
        if not self.loaded_tree:
            return
        self.shadow_size = size
        # An explicit selection writes ShadowSize even if it was omitted on input.
        self.shadow_size_present = True
        for item in self._get_action_list_items():
            item.anim_group.modified = True
            item.update_text()
        self.ui.statusBar.showMessage(
            "Shadow size set for all frames. Save to keep this change.", 5000
        )

    def _configure_open_gl(self) -> None:
        fmt = QSurfaceFormat()
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        fmt.setVersion(3, 3)
        QSurfaceFormat.setDefaultFormat(fmt)

    @undoable("Set Return Frame")
    def set_return_point(self) -> None:
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item and self.current_anim_group:
            idx = self.ui.animationFrameList.row(item)
            if self.current_anim_group.return_frame != idx:
                self.current_anim_group.return_frame = idx
            elif self.current_anim_group.return_frame == idx:
                self.current_anim_group.return_frame = -1

            for frame_item in self._get_frame_list_items():
                frame_item.update_text()

    @undoable("Set Hit Frame")
    def set_hit_point(self) -> None:
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item and self.current_anim_group:
            idx = self.ui.animationFrameList.row(item)
            if self.current_anim_group.hit_frame != idx:
                self.current_anim_group.hit_frame = idx
            elif self.current_anim_group.hit_frame == idx:
                self.current_anim_group.hit_frame = -1

            for frame_item in self._get_frame_list_items():
                frame_item.update_text()

    @undoable("Set Rush Frame")
    def set_rush_point(self) -> None:
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item and self.current_anim_group:
            idx = self.ui.animationFrameList.row(item)
            if self.current_anim_group.rush_frame != idx:
                self.current_anim_group.rush_frame = idx
            elif self.current_anim_group.rush_frame == idx:
                self.current_anim_group.rush_frame = -1

            for frame_item in self._get_frame_list_items():
                frame_item.update_text()

    def open_batch_add(self) -> None:
        if self.newWindow is None:
            source = str(pathlib.Path(self.file_name).parent) if self.file_name else ""
            self.newWindow = ActionAliasDialog(source)
        self.newWindow.show()
        self.newWindow.raise_()
        self.newWindow.activateWindow()

    def save_trim(self) -> None:
        self.settings.setValue("trim", self.ui.actionTrim_Copies.isChecked())

    def save_collapse(self) -> None:
        self.settings.setValue("collapse", self.ui.actionCollapse_Singles.isChecked())

    @undoable("Reset Frame")
    def default_frame_click(self) -> None:
        if self.current_sequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                selected_anim_frame: AnimFrame = item.anim_frame
                default_data = selected_anim_frame.default_copy

                selected_anim_frame.frame_index = default_data.frame_index
                selected_anim_frame.flip = default_data.flip
                selected_anim_frame.duration = default_data.duration
                selected_anim_frame.shadow_offset = Offset(
                    default_data.shadow_offset.x, default_data.shadow_offset.y
                )
                selected_anim_frame.sprite_offset = Offset(
                    default_data.sprite_offset.x, default_data.sprite_offset.y
                )

                item.update_text()

                if not self.animating:
                    self.set_anim_frame_values(selected_anim_frame)

                    self.set_animation()

    def save_action_trigger(self) -> None:
        if self.loaded_tree:
            self._save_document(self.file_name)

    def save_as_action_trigger(self) -> None:
        if self.loaded_tree:
            label = self.sheet_format.value
            file_name, _ = QFileDialog.getSaveFileName(
                self.window, f"Save {label}", self.file_name, f"{label} data (*.xml)"
            )
            if file_name:
                self._save_document(file_name)

    def _save_document(self, file_name: str | PathLike[str]) -> bool:
        if not file_name:
            return False
        target = pathlib.Path(file_name)
        if self.document_host and not self.document_host.can_save_path(self, target):
            return False
        other = (
            SheetFormat.MULTI
            if self.sheet_format == SheetFormat.SINGLE
            else SheetFormat.SINGLE
        )
        if target.name.casefold() == other.value.casefold():
            self.create_error_popup(
                f"Use Export to {other.value} to convert this animation."
            )
            return False
        if not self._export_bundle(target.parent, self.sheet_format, target.name):
            return False
        self.file_name = str(target)
        self.loaded_tree = ElementTree.parse(target)
        self.trackArtwork()
        self.shadow_size_present = (
            self.loaded_tree.getroot().find("ShadowSize") is not None
        )
        self.loaded_shadow_size = self.shadow_size
        for item in self._get_action_list_items():
            item.anim_group.modified = False
            item.update_text()
        for group in self.groups:
            for sequence in group.directions:
                for frame in sequence.frames:
                    frame.reset()
        self.add_recent_list(self.file_name)
        self.savedEditSignature = SpriteEditorWindow._document_state(self)
        self.undoStack.setClean()
        if self.document_host:
            self.document_host.document_saved(self)
        return True

    def _save_frame_data(self, file_name: str | PathLike[str] | None = None) -> None:
        validate_export(self, SheetFormat.SINGLE)
        self._sprite_document().write_single_xml(file_name or self.file_name)
        if not file_name:
            for item in self._get_action_list_items():
                item.anim_group.modified = False
                item.update_text()

    def create_error_popup(self, text: str) -> QtWidgets.QMessageBox.StandardButton:
        return QtWidgets.QMessageBox.critical(
            self.window, "Error", text, QtWidgets.QMessageBox.StandardButton.Ok
        )

    @undoable("Change Sheet Frame")
    def frame_index_changed(self) -> None:
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item:
            idx = self.ui.frameIndexSpinBox.value()
            clamped_idx = clamp(idx, 0, len(self.image_grid) - 1)

            if item.anim_frame.frame_index != clamped_idx:
                item.anim_frame.frame_index = clamped_idx
                self.syncActionPointControls(item.anim_frame)

                item.update_text()

                self._notify_changes()

                if not self.animating:
                    self.openGLWidget.makeCurrent()

                    self._set_anim_frame_display(item.anim_frame)

    @undoable("Move Frame")
    def _move_frame(self, row_dir: int) -> None:
        current_pos = self.ui.animationFrameList.currentRow()
        if row_dir < 0 and current_pos == 0:
            return

        if row_dir > 0 and current_pos == self.ui.animationFrameList.count() - 1:
            return

        new_pos = current_pos + row_dir

        item = self.ui.animationFrameList.takeItem(current_pos)
        self.ui.animationFrameList.insertItem(new_pos, item)

        swap_item = self.ui.animationFrameList.item(current_pos)
        new_item = self.ui.animationFrameList.item(new_pos)

        # Swap position and indexes.
        (
            self.current_sequence.frames[current_pos],
            self.current_sequence.frames[new_pos],
        ) = (
            self.current_sequence.frames[new_pos],
            self.current_sequence.frames[current_pos],
        )
        new_item.anim_frame.idx, swap_item.anim_frame.idx = (
            swap_item.anim_frame.idx,
            new_item.anim_frame.idx,
        )

        self.clear_copy_group()

        self.ui.frameSlider.setValue(item.anim_frame.idx)
        self.ui.animationFrameList.setCurrentRow(new_pos)

    def clear_copy_group(self) -> None:
        self.current_anim_group.copy_name = ""

    def copy_sequence(self) -> None:
        if self.current_sequence:
            self.copied_sequence = copy.deepcopy(self.current_sequence)

    def updateAnimationSettingsActions(self) -> None:
        """Update menu availability for the selected action and shared clipboard."""
        owner = self.document_host or self
        self.copyAnimationSettingsAction.setEnabled(
            self.current_anim_group is not None
            and any(sequence.frames for sequence in self.current_anim_group.directions)
        )
        self.pasteAnimationSettingsAction.setEnabled(
            self.current_anim_group is not None
            and owner.animationSettingsClipboard is not None
        )

    def batchEditFrames(self, scope: str) -> list[AnimFrame]:
        """Collect targets once, including frames shared by several directions."""
        if self.current_anim_group is None:
            return []
        if scope == "selected":
            eligible = {
                id(frame) for frame in self.current_anim_group.directions[
                    self.current_direction
                ].frames
            }
            frames = [
                item.anim_frame for item in self.ui.animationFrameList.selectedItems()
                if id(item.anim_frame) in eligible
            ]
        elif scope == "direction" and self.current_sequence is not None:
            frames = self.current_sequence.frames
        elif scope == "action":
            frames = [
                frame for sequence in self.current_anim_group.directions
                for frame in sequence.frames
            ]
        else:
            return []
        return list({id(frame): frame for frame in frames}.values())

    def openBatchEdit(self) -> None:
        """Keep one non-modal batch tool open while selecting animation frames."""
        if not self.batchEditFrames("action"):
            return
        owner = self.document_host or self
        if owner.batchEditDialog is None:
            owner.batchEditDialog = BatchEditDialog(self)
        dialog = owner.batchEditDialog
        dialog.setEditor(self)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def updateBatchEditDialog(self) -> None:
        owner = self.document_host or self
        dialog = owner.batchEditDialog
        if dialog is not None and dialog.editor is self:
            dialog.updateApplyButton()

    @undoable("Batch Edit Frames")
    def applyBatchEdit(self, scope: str, edit: BatchFrameEdit) -> bool:
        """Validate the full edit before applying timing and offset changes."""
        frames = self.batchEditFrames(scope)
        if not frames:
            self.ui.statusBar.showMessage("Select animation frames to edit.", 5000)
            return False
        if (
            edit.duration is None
            and edit.sprite_offset is None
            and edit.shadow_offset is None
        ):
            self.ui.statusBar.showMessage("Choose a property to change.", 5000)
            return False
        if edit.duration is not None and not 1 <= edit.duration <= 32767:
            self.ui.statusBar.showMessage("Duration must be between 1 and 32767 ticks.", 5000)
            return False
        changes: list[tuple[AnimFrame, int, Offset, Offset]] = []
        for frame in frames:
            sprite = frame.sprite_offset
            shadow = frame.shadow_offset
            if edit.sprite_offset is not None:
                sprite = (
                    sprite + edit.sprite_offset if edit.shift_sprite else edit.sprite_offset
                )
            if edit.shadow_offset is not None:
                shadow = (
                    shadow + edit.shadow_offset if edit.shift_shadow else edit.shadow_offset
                )
            for requested, offset in (
                (edit.sprite_offset, sprite), (edit.shadow_offset, shadow)
            ):
                if requested is not None and not all(
                    -32768 <= value <= 32767 for value in offset.data()
                ):
                    self.ui.statusBar.showMessage(
                        "Offsets would exceed -32768 to 32767. No frames were changed.", 8000
                    )
                    return False
            changes.append((
                frame, edit.duration if edit.duration is not None else frame.duration,
                copy.deepcopy(sprite), copy.deepcopy(shadow),
            ))
        changed = any(
            frame.duration != duration
            or frame.sprite_offset != sprite
            or frame.shadow_offset != shadow
            for frame, duration, sprite, shadow in changes
        )
        if not changed:
            self.ui.statusBar.showMessage("Selected frames already match these settings.", 5000)
            return True
        for frame, duration, sprite, shadow in changes:
            frame.duration, frame.sprite_offset, frame.shadow_offset = duration, sprite, shadow
        self.clear_copy_group()
        self.current_anim_group.modified = True
        for item in self._get_action_list_items():
            if item.anim_group is self.current_anim_group:
                item.update_text()
        item = self.ui.animationFrameList.currentItem()
        selectedSheetFrame = self.selected_sheet_frame
        if item is not None:
            self.set_anim_frame_values(item.anim_frame)
        self.selectSheetFrame(selectedSheetFrame)
        if not self.animating:
            self.set_animation()
        noun = "frame" if len(frames) == 1 else "frames"
        self.ui.statusBar.showMessage(f"Updated {len(frames)} animation {noun}.", 5000)
        return True

    def copyAnimationSettings(self) -> None:
        """Keep a detached settings snapshot available across sprite tabs."""
        if self.current_anim_group is None:
            QtWidgets.QMessageBox.warning(
                self.document_host or self.window,
                "Cannot Copy Animation Settings",
                "No source animation is selected. Select an animation, then copy again.",
            )
            return
        if not any(sequence.frames for sequence in self.current_anim_group.directions):
            QtWidgets.QMessageBox.warning(
                self.document_host or self.window,
                "Cannot Copy Animation Settings",
                f"Source '{self.current_anim_group.name}' has no frames. "
                "Select an animation with frames, then copy again.",
            )
            return
        owner = self.document_host or self
        owner.animationSettingsClipboard = copy.deepcopy(self.current_anim_group)
        if self.document_host is not None:
            for editor in self.document_host._editors.values():
                editor.updateAnimationSettingsActions()
        else:
            self.updateAnimationSettingsActions()
        self.ui.statusBar.showMessage(
            f"Copied animation settings from {self.current_anim_group.name}.", 5000
        )

    @undoable("Paste Animation Settings")
    def pasteAnimationSettings(self) -> None:
        """Copy timing and movement while keeping destination artwork and sharing."""
        source = (self.document_host or self).animationSettingsClipboard
        target = self.current_anim_group
        if source is None:
            QtWidgets.QMessageBox.warning(
                self.document_host or self.window,
                "Cannot Paste Animation Settings",
                "No animation settings have been copied. "
                "Copy settings from a source animation, then paste again.",
            )
            return
        if target is None:
            QtWidgets.QMessageBox.warning(
                self.document_host or self.window,
                "Cannot Paste Animation Settings",
                f"Settings were copied from '{source.name}', but no destination "
                "animation is selected. Select a destination, then paste again.",
            )
            return
        # Validate every direction before changing any destination frame.
        assignments: dict[int, tuple[AnimFrame, AnimFrame]] = {}
        frameLocations: dict[int, tuple[int, int]] = {}
        for direction, (sourceSequence, targetSequence) in enumerate(
            zip(source.directions, target.directions)
        ):
            if len(sourceSequence.frames) != len(targetSequence.frames):
                QtWidgets.QMessageBox.warning(
                    self.document_host or self.window,
                    "Cannot Paste Animation Settings",
                    f"Frame counts differ for {FD_STR[direction].title()}:\n"
                    f"Source '{source.name}': {len(sourceSequence.frames)} frames\n"
                    f"Destination '{target.name}': {len(targetSequence.frames)} frames\n\n"
                    "Add or remove frames so this direction has the same frame count "
                    "in both animations, then paste again. "
                    "If you edit the source, copy its settings again first.",
                )
                return
            for frameNumber, (sourceFrame, targetFrame) in enumerate(
                zip(sourceSequence.frames, targetSequence.frames), start=1
            ):
                previous = assignments.get(id(targetFrame))
                if previous is not None:
                    previousFrame = previous[1]
                    if (
                        previousFrame.duration != sourceFrame.duration
                        or previousFrame.sprite_offset != sourceFrame.sprite_offset
                        or previousFrame.shadow_offset != sourceFrame.shadow_offset
                    ):
                        previousDirection, previousNumber = frameLocations[id(targetFrame)]
                        sharedFrames = (
                            f"{FD_STR[previousDirection].title()} frame {previousNumber} and "
                            f"{FD_STR[direction].title()} frame {frameNumber}"
                        )
                        differences = []
                        for label, previousValue, currentValue in (
                            ("Duration", previousFrame.duration, sourceFrame.duration),
                            ("Sprite offset", previousFrame.sprite_offset.data(),
                             sourceFrame.sprite_offset.data()),
                            ("Shadow offset", previousFrame.shadow_offset.data(),
                             sourceFrame.shadow_offset.data()),
                        ):
                            if previousValue != currentValue:
                                differences.append(
                                    f"{label}: {previousValue} vs {currentValue}"
                                )
                        QtWidgets.QMessageBox.warning(
                            self.document_host or self.window,
                            "Cannot Paste Animation Settings",
                            f"Destination '{target.name}' shares {sharedFrames}.\n\n"
                            f"In source '{source.name}', those same frames differ "
                            "(values in the order above):\n"
                            + "\n".join(differences)
                            + "\n\n"
                            "Make their durations and sprite/shadow offsets match, "
                            "then copy again.",
                        )
                        return
                assignments[id(targetFrame)] = (targetFrame, sourceFrame)
                frameLocations[id(targetFrame)] = (direction, frameNumber)
        if (
            not target.copy_name
            and (target.rush_frame, target.hit_frame, target.return_frame)
            == (source.rush_frame, source.hit_frame, source.return_frame)
            and all(
                targetFrame.duration == sourceFrame.duration
                and targetFrame.sprite_offset == sourceFrame.sprite_offset
                and targetFrame.shadow_offset == sourceFrame.shadow_offset
                for targetFrame, sourceFrame in assignments.values()
            )
        ):
            self.ui.statusBar.showMessage("Animation settings already match.", 5000)
            return
        for targetFrame, sourceFrame in assignments.values():
            targetFrame.duration = sourceFrame.duration
            targetFrame.sprite_offset = copy.deepcopy(sourceFrame.sprite_offset)
            targetFrame.shadow_offset = copy.deepcopy(sourceFrame.shadow_offset)
        target.rush_frame = source.rush_frame
        target.hit_frame = source.hit_frame
        target.return_frame = source.return_frame
        target.copy_name = ""
        target.modified = True
        for item in self._get_action_list_items():
            if item.anim_group is target:
                item.update_text()
        for item in self._get_frame_list_items():
            item.update_text()
        selected = self.ui.animationFrameList.currentItem()
        selectedSheetFrame = self.selected_sheet_frame
        if selected is not None:
            self.set_anim_frame_values(selected.anim_frame)
        self.selectSheetFrame(selectedSheetFrame)
        if not self.animating:
            self.set_animation()
        self.ui.statusBar.showMessage(
            f"Pasted animation settings from {source.name} onto {target.name}.", 5000
        )

    @undoable("Paste Sequence")
    def paste_sequence(self) -> None:
        if self.current_sequence:
            if self.copied_sequence:
                self.current_sequence = self.copied_sequence
                self.current_anim_group.replace_sequence(
                    self.current_direction, self.copied_sequence
                )
                self.clear_copy_group()
                self.current_anim_frame = self.copied_sequence.frames[0]
                self.copied_sequence = None
                self.set_sequence_list()
                self.ui.animationFrameList.setCurrentRow(0)
                self.set_anim_frame_values(self.current_anim_frame)

    def move_frame_up(self) -> None:
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item:
            self._move_frame(-1)

    def move_frame_down(self) -> None:
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item:
            self._move_frame(1)

    def add_recent_list(self, path: str | PathLike[str]) -> None:
        path = str(path)
        if self.document_host:
            self.recent_files = self.settings.value("recent", [], list)
        # Push to top.
        try:
            self.recent_files.remove(path)
        except ValueError:
            pass

        self.recent_files.insert(0, path)

        self.recent_files = self.recent_files[: self.MAX_RECENT]

        self.settings.setValue("recent", self.recent_files)

        self._update_recent_actions()
        if self.document_host:
            self.document_host.refresh_recent_files()

    def _update_recent_actions(self) -> None:
        recent_count = min(len(self.recent_files), self.MAX_RECENT)

        for i in range(recent_count):
            action = self.recentFileActions[i]
            path = self.recent_files[i]
            action.setText(QFileInfo(path).fileName())
            action.setText(f"{QFileInfo(path).filePath()}")
            action.setVisible(True)
            action.setData(path)

        for m in range(recent_count, self.MAX_RECENT):
            self.recentFileActions[m].setVisible(False)

    @undoable("Delete Action")
    def delete_action(self) -> None:
        current: AnimGroupItem = self.ui.actionListWidget.currentItem()

        if current:
            ret = QtWidgets.QMessageBox.question(
                self.window,
                "",
                f"Are you sure you want to delete {current.anim_group.name}?",
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No,
            )
            if ret == QtWidgets.QMessageBox.StandardButton.Yes:
                row = self.ui.actionListWidget.row(current)
                self.groups = [group for group in self.groups if group is not current.anim_group]
                taken = self.ui.actionListWidget.takeItem(row)
                del taken

                pyglet.clock.unschedule(self._playing_animation)
                self.animating = False
                self.current_anim_frame = None
                self.current_anim_group = None
                self.current_sequence = None

                self.clear_anim_frame()

                if self.sprite:
                    self.sprite.delete()
                    self.shadow.delete()
                    self.sprite = None
                    self.shadow = None

                self.ui.animationFrameList.clear()

    @undoable("Duplicate Action")
    def duplicate_action(self) -> None:
        current: AnimGroupItem = self.ui.actionListWidget.currentItem()

        if not current:
            self.ui.statusBar.showMessage("Select an action to duplicate.", 5000)
            return

        name, ok = QInputDialog.getText(
            self.window, "Duplicate Action", "Enter an action name to replace or add."
        )
        if not ok:
            return

        index_id, ok = QInputDialog.getText(
            self.window,
            "Duplicate Action",
            "Specify an available index ID. Leave blank to generate one.",
        )

        if not ok:
            return

        try:
            index_id = int(index_id)
        except ValueError:
            index_id = None

        existing_group: AnimGroup | None = None
        if self.groups and ok and name:
            selected_group = current.anim_group

            for group in self.groups:
                if group.name.lower() == name.lower():
                    existing_group = group
                    break

            # Replace existing group.
            if existing_group:
                existing_group.rush_frame = selected_group.rush_frame
                existing_group.hit_frame = selected_group.hit_frame
                existing_group.return_frame = selected_group.return_frame
                existing_group.directions = copy.deepcopy(selected_group.directions)

                for item in self._get_action_list_items():
                    if item.anim_group.name.lower() == name.lower():
                        item.update_text()

                self.ui.statusBar.showMessage(
                    f"Action: {name} already existed. It has been replaced.", 5000
                )

            else:
                if not index_id:
                    index_id = self.groups[-1].idx + 1

                group = AnimGroup(
                    index_id,
                    name,
                    selected_group.rush_frame,
                    selected_group.hit_frame,
                    selected_group.return_frame,
                    copy.deepcopy(selected_group.directions),
                )
                group.copy_name = selected_group.name
                self.groups.append(group)
                item = AnimGroupItem(group, self)
                self.ui.actionListWidget.addItem(item)

                self.ui.statusBar.showMessage(f"Action: {name} created.", 5000)

    @undoable("Add Action")
    def open_add_action(self) -> None:
        name, ok = QInputDialog.getText(
            self.window, "Create New Action", "Enter an action name."
        )
        if not ok:
            return

        index_id, ok = QInputDialog.getText(
            self.window,
            "Create New Action",
            "Specify an available index ID. Leave blank to generate one.",
        )

        if not ok:
            return

        try:
            index_id = int(index_id)
        except ValueError:
            index_id = None

        if self.groups and ok and name:
            for group in self.groups:
                if group.name == name:
                    self.ui.statusBar.showMessage(
                        f"Action: {name} already exists.", 5000
                    )
                    return

                if group.idx == index_id:
                    self.ui.statusBar.showMessage(
                        f"Index ID {index_id} already exists.", 5000
                    )
                    return

            if index_id is None:
                index_id = self.groups[-1].idx + 1

            group = AnimGroup(index_id, name)
            self.groups.append(group)
            item = AnimGroupItem(group, self)
            self.ui.actionListWidget.addItem(item)

    def _notify_changes(self) -> None:
        """Set action as changed if an action has been modified."""
        if self.current_anim_group:
            for sequences in self.current_anim_group.directions:
                for frame in sequences.frames:
                    if frame.changed:
                        for item in self._get_action_list_items():
                            if item.anim_group == self.current_anim_group:
                                if self.current_anim_group.modified is False:
                                    self.current_anim_group.modified = True
                                    item.update_text()
                                return

    def change_animation_speed(self) -> None:
        new_speed = self.ANIMATION_SPEED_SLIDER_VALUES[
            self.ui.animationSpeedSlider.value()
        ]

        self.anim_speed = (1 / 60.0) / new_speed

        self.ui.animationSpeedLabel.setText(f"{new_speed}x")

    @undoable("Change Frame Duration")
    def duration_changed(self) -> None:
        if self.current_sequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                anim_frame = item.anim_frame
                value = self.ui.durationSpinBox.value()
                anim_frame.duration = value

                self.clear_copy_group()

                self._notify_changes()

    @undoable("Mirror Frame")
    def flip_changed(self) -> None:
        if self.current_sequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                anim_frame = item.anim_frame
                checked = self.ui.mirroredCheckbox.isChecked()
                if int(checked) != anim_frame.flip:
                    anim_frame.flip = int(checked)
                    self.syncActionPointControls(anim_frame)

                    self.clear_copy_group()

                    if not self.animating:
                        self.set_animation()

                    self._notify_changes()

    @undoable("Change Sprite Offset")
    def sprite_offset_changed(self) -> None:
        if self.current_sequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                anim_frame = item.anim_frame
                offset = anim_frame.sprite_offset
                x_value = self.ui.xSpinBox.value()
                y_value = self.ui.ySpinBox.value()

                if offset.x != x_value or offset.y != y_value:
                    offset.x = x_value
                    offset.y = y_value

                    self.clear_copy_group()

                    if not self.animating:
                        self.set_animation()

                    self._notify_changes()

    @undoable("Change Shadow Offset")
    def shadow_offset_changed(self) -> None:
        if self.current_sequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                anim_frame = item.anim_frame
                offset = anim_frame.shadow_offset
                x_value = self.ui.xShadowSpinbox.value()
                y_value = self.ui.yShadowSpinBox.value()

                if offset.x != x_value or offset.y != y_value:
                    offset.x = x_value
                    offset.y = y_value

                    self.clear_copy_group()

                    if not self.animating:
                        self.set_animation()

                    self._notify_changes()

    @undoable("Duplicate Frame")
    def duplicate_frame(self) -> None:
        """Duplicate the selected frame in the Animation Sequence List"""
        if self.current_sequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                selected_anim_frame: AnimFrame = item.anim_frame
                anim_idx = len(self.current_sequence.frames)
                anim_frame = AnimFrame(
                    anim_idx,
                    selected_anim_frame.frame_index,
                    selected_anim_frame.flip,
                    selected_anim_frame.duration,
                    Offset(
                        selected_anim_frame.shadow_offset.x,
                        selected_anim_frame.shadow_offset.y,
                    ),
                    Offset(
                        selected_anim_frame.sprite_offset.x,
                        selected_anim_frame.sprite_offset.y,
                    ),
                )

                self.clear_copy_group()

                self._notify_changes()

                self._add_anim_frame(anim_frame)

    @undoable("Delete Frame")
    def delete_selected_frames(self) -> None:
        """Delete the selected frame in the Animation Sequence List"""
        if self.current_sequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                for frame in list(self.current_sequence.frames):
                    if frame == item.anim_frame:
                        self.current_sequence.frames.remove(frame)

                self.ui.animationFrameList.takeItem(
                    self.ui.animationFrameList.row(item)
                )
                del item

                self.clear_copy_group()

                # Just use last in list as selection.
                last_idx = self.ui.animationFrameList.count() - 1
                last_item = self.ui.animationFrameList.item(last_idx)
                self.ui.animationFrameList.setCurrentItem(last_item)
                self._update_anim_frame_widgets()

    def slider_change(self) -> None:
        if not self.animating:
            if self.current_sequence:
                if self.current_sequence.frames:
                    self.ui.animationFrameList.setCurrentRow(
                        self.ui.frameSlider.value()
                    )
                    self.current_anim_frame = self.current_sequence.frames[
                        self.ui.frameSlider.value()
                    ]
                    self.set_anim_frame_values(self.current_anim_frame)
                    self.set_animation()

    def set_direction(self, direction: int) -> None:
        if direction == self.current_direction:
            self.directionButtons[direction].toggle()
            return

        for button in self.directionButtons:
            if button != self.directionButtons[direction]:
                button.setChecked(False)

        self.current_direction = direction

        self.ui.animationFrameList.clear()

        if self.current_anim_group:
            if self.current_anim_group.directions:
                self.current_sequence = self.current_anim_group.directions[
                    self.current_direction
                ]

                if self.current_sequence.frames:
                    self.current_anim_frame = self.current_sequence.frames[0]

                if not self.animating:
                    self.set_anim_frame_values(self.current_anim_frame)

                if self.current_sequence:
                    self.set_sequence_list()

                self.ui.animationFrameList.setCurrentRow(0)

        self.updateBatchEditDialog()

    def clear_anim_frame(self) -> None:
        """Clears information boxes of the Frame Data."""
        self.current_anim_frame = None
        self.displayed_anim_frame = None
        self.syncActionPointControls(None)

        self.ui.animationFrameList.clear()

        self.ui.frameIndexSpinBox.blockSignals(True)
        self.ui.frameIndexSpinBox.setValue(0)
        self.ui.frameIndexSpinBox.blockSignals(False)

        self.ui.durationSpinBox.blockSignals(True)
        self.ui.durationSpinBox.setValue(0)
        self.ui.durationSpinBox.blockSignals(False)

        self.ui.mirroredCheckbox.blockSignals(True)
        self.ui.mirroredCheckbox.setChecked(False)
        self.ui.mirroredCheckbox.blockSignals(False)

        self.ui.xSpinBox.blockSignals(True)
        self.ui.ySpinBox.blockSignals(True)
        self.ui.xSpinBox.setValue(0)
        self.ui.ySpinBox.setValue(0)
        self.ui.xSpinBox.setEnabled(False)
        self.ui.ySpinBox.setEnabled(False)
        self.ui.xSpinBox.blockSignals(False)
        self.ui.ySpinBox.blockSignals(False)

        self.ui.xShadowSpinbox.blockSignals(True)
        self.ui.yShadowSpinBox.blockSignals(True)
        self.ui.xShadowSpinbox.setValue(0)
        self.ui.yShadowSpinBox.setValue(0)
        self.ui.xShadowSpinbox.setEnabled(False)
        self.ui.yShadowSpinBox.setEnabled(False)
        self.ui.xShadowSpinbox.blockSignals(False)
        self.ui.yShadowSpinBox.blockSignals(False)

    def set_sequence_list(self) -> None:
        self.ui.animationFrameList.clear()

        duration = 0
        for anim_frame in self.current_sequence.frames:
            duration += anim_frame.duration
            item = AnimFrameItem(anim_frame, self)
            self.ui.animationFrameList.addItem(item)

        self._update_anim_frame_widgets()

    def _update_anim_frame_widgets(self) -> None:
        self.ui.frameSlider.setMaximum(max(0, len(self.current_sequence.frames) - 1))
        self.ui.frameSlider.setValue(0)

        self.set_animation()

    def set_anim_frame_values(self, anim_frame: AnimFrame) -> None:
        # Displaying a selection must not run edit handlers or detach copies.
        with (
            QtCore.QSignalBlocker(self.ui.frameIndexSpinBox),
            QtCore.QSignalBlocker(self.ui.durationSpinBox),
            QtCore.QSignalBlocker(self.ui.mirroredCheckbox),
        ):
            self.ui.frameIndexSpinBox.setValue(anim_frame.frame_index)
            self.ui.durationSpinBox.setValue(anim_frame.duration)
            self.ui.mirroredCheckbox.setChecked(bool(anim_frame.flip))

        self.set_offset_data(anim_frame)
        self.syncActionPointControls(anim_frame)

    def set_offset_data(self, anim_frame: AnimFrame | None) -> None:
        if anim_frame:
            self.ui.xSpinBox.blockSignals(True)
            self.ui.ySpinBox.blockSignals(True)
            self.ui.xShadowSpinbox.blockSignals(True)
            self.ui.yShadowSpinBox.blockSignals(True)

            self.ui.xSpinBox.setValue(anim_frame.sprite_offset.x)
            self.ui.ySpinBox.setValue(anim_frame.sprite_offset.y)
            self.ui.xSpinBox.setEnabled(True)
            self.ui.ySpinBox.setEnabled(True)

            self.ui.xShadowSpinbox.setValue(anim_frame.shadow_offset.x)
            self.ui.yShadowSpinBox.setValue(anim_frame.shadow_offset.y)
            self.ui.xShadowSpinbox.setEnabled(True)
            self.ui.yShadowSpinBox.setEnabled(True)

            self.ui.xSpinBox.blockSignals(False)
            self.ui.ySpinBox.blockSignals(False)
            self.ui.xShadowSpinbox.blockSignals(False)
            self.ui.yShadowSpinBox.blockSignals(False)

    def load_recent_file(self, action: QtGui.QAction | None) -> None:
        if action:
            if self.document_host:
                self.document_host.open_file(action.data())
            else:
                self.load_animation(action.data())

    def _load_animation_dialog(self) -> None:
        if self.document_host:
            self.document_host.open_files_dialog()
            return
        file_name, _ = QFileDialog.getOpenFileName(
            self.window, "Select Animation File", "", "Animation File (*.xml)"
        )
        if file_name:
            self.load_animation(file_name)

    def load_animation(self, file_name: str | PathLike[str]) -> bool:
        try:
            format_name = detect_format(ElementTree.parse(file_name).getroot())
        except (OSError, ElementTree.ParseError) as error:
            self.create_error_popup(str(error))
            return False
        loaded = (
            self.load_sheet(file_name)
            if format_name == SheetFormat.SINGLE
            else self.import_multiple_sheets(file_name)
        )
        if loaded and self.ui.actionListWidget.count():
            self.ui.actionListWidget.setCurrentRow(0)
            self.ui.actionListWidget.item(0).mouseClickEvent(None)
        return loaded

    def load_sheet(self, file_name: str | PathLike[str]) -> bool:
        return self._load_sprite_document(file_name, SheetFormat.SINGLE)

    def _load_sprite_document(
        self, file_name: str | PathLike[str], expected: SheetFormat
    ) -> bool:
        try:
            document = SpriteDocument.load(file_name, expected)
        except (FormatError, OSError, ElementTree.ParseError) as error:
            self.create_error_popup(str(error))
            return False

        # Only replace the current document after the backend has loaded and
        # validated every source file. Graphics objects belong to this adapter.
        if self.openGLWidget.isValid():
            self.openGLWidget.makeCurrent()
        self.clear()

        def preview_image(image: Image.Image | None) -> pyglet.image.ImageData | None:
            if image is None:
                return None
            return pyglet.image.ImageData(
                image.width,
                image.height,
                "RGBA",
                image.tobytes(),
                pitch=-image.width * 4,
            )

        self.sheet_image = preview_image(document.sheet_image)
        self.action_pt_image = preview_image(document.action_pt_image)
        self.frame_width, self.frame_height = (
            document.frame_width,
            document.frame_height,
        )
        self.loaded_tree = document.source_tree
        self.sheet_format = document.sheet_format
        modeName = "Single" if document.sheet_format == SheetFormat.SINGLE else "Multi"
        self.ui.groupBox_5.setTitle(f"Sprite Sheet ({modeName})")
        self.single_loaded = document.sheet_format == SheetFormat.SINGLE
        self.shadow_size = document.shadow_size
        self.shadow_size_present = document.shadow_size_present
        self.loaded_shadow_size = document.loaded_shadow_size
        self.groups = document.groups
        self.action_points = document.action_points
        self.loaded_action_points = document.loaded_action_points
        self.file_name = str(file_name)
        rows, columns = document.image_grid.rows, document.image_grid.columns
        self.image_grid = TopLeftGrid(self.sheet_image, rows=rows, columns=columns)
        self.action_grid = (
            TopLeftGrid(self.action_pt_image, rows=rows, columns=columns)
            if self.action_pt_image is not None
            else None
        )
        self._add_frames_from_grid()
        for group in self.groups:
            self.ui.actionListWidget.addItem(AnimGroupItem(group, self))
        self.add_recent_list(file_name)
        self.trackArtwork()
        self.savedEditSignature = SpriteEditorWindow._document_state(self)
        self.ui.statusBar.showMessage(
            "Frame data and images loaded successfully.", 3000
        )
        return True

    @undoable("Add Frame")
    def add_new_animation_frame(self, frame_idx: int) -> None:
        """Create new animation frame in the sequence. Adds to the end."""
        if self.current_sequence:
            anim_idx = len(self.current_sequence.frames)
            anim_frame = AnimFrame(anim_idx, frame_idx)

            self._add_anim_frame(anim_frame)

            self.clear_copy_group()

            self._notify_changes()

    def _add_anim_frame(self, anim_frame: AnimFrame) -> None:
        self.current_sequence.frames.append(anim_frame)

        item = AnimFrameItem(anim_frame, self)
        self.ui.animationFrameList.addItem(item)

        self._update_anim_frame_widgets()

    @staticmethod
    def adjust_offset(
        rush_frame: int, frame_num: int, rush_offset: Offset, frame_offset: Offset
    ) -> Offset | None:
        """Calculation to truncate rush frames."""
        if frame_num > rush_frame:
            diff = frame_offset - rush_offset

            final = rush_offset + (diff // 3)
            return final

    def _add_frames_from_grid(self) -> None:
        self.ui.frameIndexSpinBox.setMaximum(len(self.image_grid) - 1)

        for idx, image in enumerate(self.image_grid):
            image: pyglet.image.ImageDataRegion
            image.anchor_x = image.width // 2
            image.anchor_y = image.height // 2
            if self.action_points and idx not in self.action_points:
                continue
            item = LoadedSheetFrame(
                f"Frame {idx}", idx, image, self.ui.sheetFramePicture, self
            )
            self.ui.loadedSheetFrameList.addItem(item)

    def _get_action_list_items(self) -> list[AnimGroupItem]:
        return [
            self.ui.actionListWidget.item(x)
            for x in range(self.ui.actionListWidget.count())
        ]

    def _get_frame_list_items(self) -> list[AnimFrameItem]:
        return [
            self.ui.animationFrameList.item(x)
            for x in range(self.ui.animationFrameList.count())
        ]

    def clear(self) -> None:
        """Clear everything so we can load a new sprite."""
        self.undoStack.clear()
        self.savedEditSignature = None
        self.ui.groupBox_5.setTitle("Sprite Sheet")
        self.artworkReloadTimer.stop()
        self.artworkPath = None
        self.artworkDigest = None
        self.updateArtworkWatch()
        self.sheet_preview_images.clear()
        self.single_loaded = None
        self.sheet_image = None
        self.action_pt_image = None
        self.image_grid: TopLeftGrid | None = None
        self.action_grid: TopLeftGrid | None = None
        self.action_points.clear()
        self.groups.clear()

        pyglet.clock.unschedule(self._playing_animation)
        self.animating = False
        self.current_anim_frame = None
        self.current_anim_group = None
        self.current_sequence = None

        self.clear_anim_frame()

        if self.sprite:
            self.sprite.delete()
            self.shadow.delete()
            self.sprite = None
            self.shadow = None

        for sprite in self.ap_sprites:
            sprite.delete()
        self.ap_sprites.clear()
        self.left_hand = self.center = self.right_hand = self.head = (
            self.shadow_point
        ) = None

        self.ui.loadedSheetFrameList.clear()
        self.ui.animationFrameList.clear()
        self.ui.actionListWidget.clear()

        self.ui.sheetFramePicture.clear()

    def get_sprite_position(self) -> tuple[float, float, float]:
        return (
            (self.openGLWidget.width() // 2)
            + (self.current_anim_frame.sprite_offset.x * self.scale),
            (self.openGLWidget.height() // 3)
            + (-self.current_anim_frame.sprite_offset.y * self.scale),
            0,
        )

    def get_shadow_position(self) -> tuple[float, float, float]:
        return (
            (self.openGLWidget.width() // 2)
            + (self.current_anim_frame.shadow_offset.x * self.scale),
            (self.openGLWidget.height() // 3)
            + (-self.current_anim_frame.shadow_offset.y * self.scale),
            0,
        )

    def get_action_point_positions(
        self, anim_frame: AnimFrame | None = None
    ) -> tuple[tuple[float, float, float], ...]:
        anim_frame = anim_frame or self.current_anim_frame
        ap = self.action_points[anim_frame.frame_index]

        if not anim_frame.flip:
            lh_pos, c_pos, rh_pos, head_pos = ap.all_pos()
        else:
            width = self.image_grid[anim_frame.frame_index].width
            pivot = width - 1 - 2 * (width // 2)
            lh_pos, c_pos, rh_pos, head_pos = [
                Offset(pivot - point.x, point.y) for point in ap.all_pos()
            ]

        return tuple(
            (
                self.openGLWidget.width() // 2
                + (anim_frame.sprite_offset.x + point.x) * self.scale,
                self.openGLWidget.height() // 3
                - (anim_frame.sprite_offset.y + point.y) * self.scale,
                0,
            )
            for point in (lh_pos, c_pos, rh_pos, head_pos)
        )

    def updateActionPointPreview(self, anim_frame: AnimFrame | None) -> None:
        if self.sprite is None or anim_frame is None:
            return
        positions = (
            self.get_action_point_positions(anim_frame)
            if anim_frame.frame_index in self.action_points
            else (None,) * 4
        )
        shadow_position = (
            self.openGLWidget.width() // 2 + anim_frame.shadow_offset.x * self.scale,
            self.openGLWidget.height() // 3 - anim_frame.shadow_offset.y * self.scale,
            0,
        )
        for name, color, position in zip(
            ("left_hand", "center", "right_hand", "head", "shadow_point"),
            ((255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 0, 0), (255, 255, 255)),
            (*positions, shadow_position),
        ):
            marker = getattr(self, name)
            if marker is None and position is not None:
                marker = pyglet.sprite.Sprite(
                    self.action_point_marker,
                    batch=self.openGLWidget.batch,
                    group=pyglet.graphics.Group(3 if name == "center" else 2),
                )
                marker.color = color
                marker.scale = self.scale
                marker.opacity = self.ui.actionPointsOpacitySlider.value()
                setattr(self, name, marker)
                self.ap_sprites.append(marker)
            if marker is not None:
                if position is not None:
                    marker.position = position
                marker.visible = (
                    position is not None
                    and self.ui.showActionPointsCheckBox.isChecked()
                )

    def _playing_animation(self, dt: float) -> None:
        self.openGLWidget.makeCurrent()

        self._set_anim_frame_display(self.current_anim_frame)

        self.ui.frameSlider.setValue(self.current_anim_frame.idx)

        existing_frame = self.current_anim_frame

        next_frame = self.current_anim_frame.idx + 1
        if next_frame > len(self.current_sequence.frames) - 1:
            next_frame = 0

        self.current_anim_frame = self.current_sequence.frames[next_frame]

        pyglet.clock.schedule_once(
            self._playing_animation, self.anim_speed * existing_frame.duration
        )

    @property
    def animating(self) -> bool:
        return self._animating

    @animating.setter
    def animating(self, playing: bool) -> None:
        self._animating = bool(playing)
        self.ui.playButton.setPlaying(self._animating)

    def play_animation(self) -> None:
        if self.current_anim_frame:
            self.animating = not self.animating
            pyglet.clock.unschedule(self._playing_animation)

            if self.animating:
                pyglet.clock.schedule_once(
                    self._playing_animation,
                    self.anim_speed * self.current_anim_frame.duration,
                )
            else:
                self.slider_change()

    def set_animation(self) -> None:
        # Loading a new tab can select its first frame before Qt creates its GL context.
        if not self.openGLWidget.isValid() or self.openGLWidget._pyglet_context is None:
            self.openGLWidget._restore_preview = self.current_anim_frame is not None
            return
        self.openGLWidget.makeCurrent()

        if self.current_anim_group:
            if self.current_anim_group.directions[self.current_direction].frames:
                if not self.sprite:
                    sprite_pos = self.get_sprite_position()
                    shadow_pos = self.get_shadow_position()

                    first_idx = (
                        self.current_anim_group.directions[self.current_direction]
                        .frames[0]
                        .frame_index
                    )

                    self.shadow = pyglet.sprite.Sprite(
                        self._display_shadow_image(),
                        x=shadow_pos[0],
                        y=shadow_pos[1],
                        batch=self.openGLWidget.batch,
                        group=pyglet.graphics.Group(0),
                    )
                    self.shadow.scale = self.scale
                    self.shadow.opacity = self.ui.shadowOpacitySlider.value()

                    self.sprite = pyglet.sprite.Sprite(
                        self.image_grid[first_idx],
                        x=sprite_pos[0],
                        y=sprite_pos[1],
                        batch=self.openGLWidget.batch,
                        group=pyglet.graphics.Group(1),
                    )
                    self.sprite.scale = self.scale
                    self.sprite.opacity = self.ui.spriteOpacitySlider.value()

                if self.sprite:
                    anim_frame = (
                        self.current_anim_frame
                        if self.current_anim_frame is not None
                        else self.current_anim_group.directions[
                            self.current_direction
                        ].frames[0]
                    )

                    self._set_anim_frame_display(anim_frame)

    def _set_anim_frame_display(self, anim_frame: AnimFrame) -> None:
        if self.sprite:
            self.displayed_anim_frame = anim_frame
            image = self.image_grid[anim_frame.frame_index]
            if anim_frame.flip:
                image = image.get_texture().get_transform(flip_x=True)
                image.anchor_x = image.width // 2
                image.anchor_y = image.height // 2

            self.sprite.image = image

            self.sprite.position = self.get_sprite_position()
            self.shadow.position = self.get_shadow_position()

            self.updateActionPointPreview(anim_frame)

    def import_multiple_sheets(self, file_name: str | PathLike[str]) -> bool:
        return self._load_sprite_document(file_name, SheetFormat.MULTI)

    def export_single_sheet(self) -> None:
        if self.loaded_tree:
            options = QFileDialog.Option.ShowDirsOnly

            directory = QFileDialog.getExistingDirectory(
                self.window, "Select Directory for Single Sheet", "", options
            )

            if directory:
                self._export_single_sheet(directory)

    def _export_single_sheet(self, directory: str | PathLike[str]) -> bool:
        return self._export_bundle(directory, SheetFormat.SINGLE)

    def _sprite_document(self) -> SpriteDocument:
        def pil_image(image: pyglet.image.AbstractImage | None) -> Image.Image | None:
            if image is None:
                return None
            return Image.frombytes(
                "RGBA",
                (image.width, image.height),
                image.get_image_data().get_data("RGBA", -image.width * 4),
            )

        return SpriteDocument(
            pil_image(self.sheet_image),
            self.frame_width,
            self.frame_height,
            groups=self.groups,
            action_points=self.action_points,
            action_pt_image=pil_image(self.action_pt_image),
            shadow_image=pil_image(self.shadow_image),
            shadow_size=self.shadow_size,
            shadow_size_present=self.shadow_size_present,
            loaded_shadow_size=self.loaded_shadow_size,
            loaded_action_points=self.loaded_action_points,
            trim_copies=self.ui.actionTrim_Copies.isChecked(),
            collapse_singles=self.ui.actionCollapse_Singles.isChecked(),
        )

    def _export_bundle(
        self,
        directory: str | PathLike[str],
        format_name: SheetFormat,
        xml_name: str | None = None,
    ) -> bool:
        try:
            if not self.groups or self.image_grid is None:
                raise FormatError("No sprite data to save.")
            self._sprite_document().export(directory, format_name, xml_name)
        except (FormatError, OSError, ValueError) as error:
            self.create_error_popup(str(error))
            return False
        message = (
            "Single sheet" if format_name == SheetFormat.SINGLE else "Multisheet frames"
        )
        self.ui.statusBar.showMessage(f"{message} were output successfully.", 3000)
        return True

    def export_multiple_sheets(self) -> None:
        if self.loaded_tree:
            options = QFileDialog.Option.ShowDirsOnly

            directory = QFileDialog.getExistingDirectory(
                self.window, "Select Directory for Multi-Sheets", "", options
            )

            if directory:
                self._export_multiple_sheets(directory)

    def _export_multiple_sheets(self, directory: str | PathLike[str]) -> bool:
        return self._export_bundle(directory, SheetFormat.MULTI)

    def get_attachment_points_from_texture(self, path: str | PathLike[str]) -> None:
        if os.path.join(path, "Offsets.png"):
            pass

    def exit_application(self) -> None:
        self.window.close()


class SpriteEditorWindow(QtWidgets.QMainWindow):
    """Independent sprite documents with a shared window and active-tab menus."""

    def __init__(self, app: QtWidgets.QApplication) -> None:
        super().__init__()
        self.app = app
        self.app.setWindowIcon(
            QtGui.QIcon(str(pathlib.Path(__file__).with_name("icon.ico")))
        )
        self.settings = QSettings("MDFrameEditor", "Frame Editor")
        self.panelStates: dict[str, QtCore.QByteArray] = {}
        for name in ("mainSplitter", "previewSplitter"):
            state = self.settings.value(f"window/{name}")
            if isinstance(state, QtCore.QByteArray):
                self.panelStates[name] = state
        self._editors: dict[QtWidgets.QWidget, AnimationEditor] = {}
        self._holders: dict[QtWidgets.QWidget, QtWidgets.QMainWindow] = {}
        self._saved_states: dict[QtWidgets.QWidget, tuple[object, ...]] = {}
        self._active_editor: AnimationEditor | None = None
        self.animationSettingsClipboard: AnimGroup | None = None
        self.batchEditDialog: BatchEditDialog | None = None
        self.documentTabs = QtWidgets.QTabWidget(self)
        self.documentTabs.setObjectName("documentTabs")
        self.documentTabs.setDocumentMode(True)
        self.documentTabs.setTabsClosable(True)
        self.documentTabs.setMovable(True)
        self.documentTabs.setUsesScrollButtons(True)
        self.documentTabs.tabBar().setElideMode(QtCore.Qt.TextElideMode.ElideMiddle)
        self.setCentralWidget(self.documentTabs)
        self.closeTabAction = QtGui.QAction("Close Tab", self)
        self.closeTabAction.setShortcut(QtGui.QKeySequence.StandardKey.Close)
        self.closeTabAction.triggered.connect(
            lambda: self.close_document(self.documentTabs.currentIndex())
        )
        self.addAction(self.closeTabAction)
        self._createPreferenceMenus()
        self.documentTabs.currentChanged.connect(self._activate_document)
        self.documentTabs.tabCloseRequested.connect(self.close_document)
        self._add_empty_document()
        self.resize(1040, 800)
        geometry = self.settings.value("window/geometry")
        if self.preferenceActions["rememberWindowSize"].isChecked() and isinstance(
            geometry, QtCore.QByteArray
        ):
            self.restoreGeometry(geometry)
        # Apply saved dividers after Qt allocates the restored window's layout.
        QtCore.QTimer.singleShot(0, self._restoreVisiblePanelLayout)

    def _createPreferenceMenus(self) -> None:
        self.settingsMenu = QtWidgets.QMenu("Settings", self)
        self.viewMenu = QtWidgets.QMenu("View", self)
        self.preferenceActions: dict[str, QtGui.QAction] = {}
        for name, text, default, menu in (
            ("rememberWindowSize", "Remember Window Size", True, self.settingsMenu),
            ("cameraLocked", "Lock Camera", True, self.settingsMenu),
            (
                "autoReloadArtwork",
                "Auto Reload Artwork",
                False,
                self.settingsMenu,
            ),
            ("showSpriteBounds", "Show Sprite Dimensions", False, self.viewMenu),
            ("includeWhitespace", "Include Sprite Whitespace", False, self.viewMenu),
        ):
            action = QtGui.QAction(text, self)
            action.setCheckable(True)
            action.setChecked(self.settings.value(f"preferences/{name}", default, bool))
            action.toggled.connect(partial(self.setPreference, name))
            menu.addAction(action)
            self.preferenceActions[name] = action
        self.viewMenu.addSeparator()
        self.infoAction = self.viewMenu.addAction("Info...", self.showSpriteInfo)
        self.infoAction.setEnabled(False)
        self.viewMenu.aboutToShow.connect(self.updateInfoAction)

    def updateInfoAction(self) -> None:
        editor = self.current_editor
        self.infoAction.setEnabled(editor is not None and editor.sheet_image is not None)

    def showSpriteInfo(self) -> None:
        editor = self.current_editor
        if editor is None or editor.sheet_image is None:
            return
        dialog = SpriteInfoDialog(editor)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def setPreference(self, name: str, checked: bool) -> None:
        self.settings.setValue(f"preferences/{name}", checked)
        if name == "rememberWindowSize" and not checked:
            self.settings.remove("window/geometry")
        for editor in self._editors.values():
            self._applyPreviewPreferences(editor)
        self.settings.sync()

    def _applyPreviewPreferences(self, editor: AnimationEditor) -> None:
        editor.setArtworkReloadEnabled(
            self.preferenceActions["autoReloadArtwork"].isChecked()
        )
        showBounds = self.preferenceActions["showSpriteBounds"].isChecked()
        preview = editor.ui.sheetFramePicture
        preview.showBounds = showBounds
        preview.includeWhitespace = self.preferenceActions[
            "includeWhitespace"
        ].isChecked()
        preview.update()
        editor.openGLWidget.showSpriteBounds = showBounds
        editor.openGLWidget.setCameraLocked(
            self.preferenceActions["cameraLocked"].isChecked()
        )

    def updateArtworkReloadAction(self) -> None:
        """Enable the reload preference only for an active single-sheet document."""
        editor = self.current_editor
        self.preferenceActions["autoReloadArtwork"].setEnabled(
            editor is not None and editor.sheet_format == SheetFormat.SINGLE
        )

    def _restoreVisiblePanelLayout(self) -> None:
        editor = self.current_editor
        if editor is not None:
            editor.ui.updateEditorLayout()
            self._restorePanelLayout(editor)

    def _capturePanelLayout(self, editor: AnimationEditor) -> None:
        for name in ("mainSplitter", "previewSplitter"):
            self.panelStates[name] = getattr(editor.ui, name).saveState()

    def _restorePanelLayout(self, editor: AnimationEditor) -> None:
        for name, state in self.panelStates.items():
            getattr(editor.ui, name).restoreState(state)

    def _saveWindowLayout(self) -> None:
        if self.current_editor is not None:
            self._capturePanelLayout(self.current_editor)
        if self.preferenceActions["rememberWindowSize"].isChecked():
            self.settings.setValue("window/geometry", self.saveGeometry())
        else:
            self.settings.remove("window/geometry")
        for name, state in self.panelStates.items():
            self.settings.setValue(f"window/{name}", state)
        self.settings.sync()

    @property
    def current_editor(self) -> AnimationEditor | None:
        return self._editors.get(self.documentTabs.currentWidget())

    @staticmethod
    def _path_key(path: str | PathLike[str]) -> str:
        return os.path.normcase(os.path.realpath(path))

    @staticmethod
    def _document_state(editor: AnimationEditor) -> tuple[object, ...]:
        groups = tuple(
            (
                group.idx,
                group.name,
                group.copy_name,
                group.is_single_direction,
                group.rush_frame,
                group.hit_frame,
                group.return_frame,
                tuple(
                    tuple(
                        (
                            frame.frame_index,
                            frame.flip,
                            frame.duration,
                            frame.sprite_offset.data(),
                            frame.shadow_offset.data(),
                        )
                        for frame in sequence.frames
                    )
                    for sequence in group.directions
                ),
            )
            for group in editor.groups
        )
        points = tuple(
            (index, tuple(point.data() for point in body.all_pos()))
            for index, body in sorted(editor.action_points.items())
        )
        return (editor.shadow_size, editor.shadow_size_present, groups, points)

    def _create_document(self) -> tuple[QtWidgets.QWidget, AnimationEditor]:
        # Reuse the editor UI without embedding a second menu/status bar in a tab.
        holder = QtWidgets.QMainWindow(self)
        holder.hide()
        ui = Ui_MainWindow()
        ui.setupUi(holder)
        page = holder.takeCentralWidget()
        editor = AnimationEditor(self.app, self, ui, document_host=self)
        self._applyPreviewPreferences(editor)
        self._restorePanelLayout(editor)
        editor.openGLWidget.timer.stop()
        ui.menuFile.insertAction(ui.actionExit, self.closeTabAction)
        ui.statusBar.messageChanged.connect(
            lambda message, document=editor: self._document_status(document, message)
        )
        self._editors[page] = editor
        self._holders[page] = holder
        return page, editor

    def _add_empty_document(self) -> AnimationEditor:
        page, editor = self._create_document()
        self.documentTabs.addTab(page, "Untitled")
        self.documentTabs.setCurrentWidget(page)
        return editor

    def open_files_dialog(self) -> list[AnimationEditor]:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open Sprites", "", "Animation Files (*.xml)"
        )
        return self.open_files(paths)

    def open_files(self, paths: Iterable[str | PathLike[str]]) -> list[AnimationEditor]:
        opened = []
        for path in paths:
            editor = self.open_file(path)
            if editor is not None:
                opened.append(editor)
        return opened

    def open_file(self, path: str | PathLike[str]) -> AnimationEditor | None:
        path = str(pathlib.Path(path).resolve())
        for page, editor in self._editors.items():
            if editor.file_name and self._path_key(editor.file_name) == self._path_key(
                path
            ):
                self.documentTabs.setCurrentWidget(page)
                return editor
        page = self.documentTabs.currentWidget()
        editor = self.current_editor
        reuse_empty = editor is not None and editor.loaded_tree is None
        if not reuse_empty:
            page, editor = self._create_document()
        try:
            loaded = editor.load_animation(path)
        except Exception:
            if not reuse_empty:
                self._dispose_document(page)
            raise
        if not loaded:
            if not reuse_empty:
                self._dispose_document(page)
            return None
        self._saved_states[page] = self._document_state(editor)
        if self.documentTabs.indexOf(page) < 0:
            self.documentTabs.addTab(page, "")
        self._update_tab(page)
        self.documentTabs.setCurrentWidget(page)
        self._document_status(editor, editor.ui.statusBar.currentMessage())
        self.updateArtworkReloadAction()
        self.updateInfoAction()
        return editor

    def _update_tab(self, page: QtWidgets.QWidget) -> None:
        editor = self._editors[page]
        index = self.documentTabs.indexOf(page)
        if index >= 0:
            folder = pathlib.Path(editor.file_name).parent
            base = folder.name or str(folder)
            previous_path = self.documentTabs.tabToolTip(index)
            previous_folder = (
                pathlib.Path(previous_path).parent if previous_path else None
            )
            previous_base = (
                (previous_folder.name or str(previous_folder))
                if previous_folder
                else None
            )
            # Keep an existing suffix stable when saving, moving, or closing other tabs.
            if base != previous_base:
                used = {
                    self.documentTabs.tabText(i).casefold()
                    for i in range(self.documentTabs.count())
                    if i != index
                }
                name = base
                suffix = 1
                while name.casefold() in used:
                    name = f"{base} ({suffix})"
                    suffix += 1
                self.documentTabs.setTabText(index, name)
            self.documentTabs.setTabToolTip(index, editor.file_name)
        if editor is self.current_editor:
            self._update_window_title()

    def _update_window_title(self) -> None:
        editor = self.current_editor
        name = (
            self.documentTabs.tabText(self.documentTabs.currentIndex())
            if editor and editor.file_name
            else ""
        )
        title = f"MD Frame Editor ({VERSION})"
        self.setWindowTitle(f"{name} - {title}" if name else title)

    def _activate_document(self, index: int) -> None:
        self.updateArtworkReloadAction()
        self.updateInfoAction()
        if self._active_editor is not None:
            self._active_editor.undoAction.setShortcuts([])
            self._active_editor.redoAction.setShortcuts([])
            self._capturePanelLayout(self._active_editor)
            self._active_editor.openGLWidget.timer.stop()
            pyglet.clock.unschedule(self._active_editor._playing_animation)
        self.menuBar().clear()
        editor = self.current_editor
        self._active_editor = editor
        if self.batchEditDialog is not None:
            self.batchEditDialog.setEditor(editor)
        if editor is not None:
            editor.undoAction.setShortcuts(QtGui.QKeySequence.StandardKey.Undo)
            editor.redoAction.setShortcuts(QtGui.QKeySequence.StandardKey.Redo)
            self._restorePanelLayout(editor)
            for menu in (editor.ui.menuFile, editor.editMenu):
                self.menuBar().addAction(menu.menuAction())
            self.menuBar().addMenu(self.viewMenu)
            self.menuBar().addMenu(self.settingsMenu)
            editor.openGLWidget.timer.start()
            if editor.animating and editor.current_anim_frame is not None:
                pyglet.clock.schedule_once(
                    editor._playing_animation,
                    editor.anim_speed * editor.current_anim_frame.duration,
                )
            self.statusBar().showMessage(editor.ui.statusBar.currentMessage())
        else:
            self.statusBar().clearMessage()
        self._update_window_title()

    def _document_status(self, editor: AnimationEditor, message: str) -> None:
        if editor is self.current_editor:
            self.statusBar().showMessage(message)

    def refresh_recent_files(self) -> None:
        for editor in self._editors.values():
            editor.recent_files = editor.settings.value("recent", [], list)
            editor._update_recent_actions()

    def can_save_path(self, editor: AnimationEditor, path: str | PathLike[str]) -> bool:
        for other in self._editors.values():
            if (
                other is not editor
                and other.file_name
                and self._path_key(other.file_name) == self._path_key(path)
            ):
                editor.create_error_popup("This file is already open in another tab.")
                return False
        return True

    def document_saved(self, editor: AnimationEditor) -> None:
        for page, document in self._editors.items():
            if document is editor:
                self._saved_states[page] = self._document_state(editor)
                self._update_tab(page)
                break

    def _confirm_close(self, page: QtWidgets.QWidget) -> bool:
        editor = self._editors[page]
        if editor.loaded_tree is None or self._saved_states.get(
            page
        ) == self._document_state(editor):
            return True
        self.documentTabs.setCurrentWidget(page)
        choice = QtWidgets.QMessageBox.question(
            self,
            "Save changes?",
            f"Save changes to {editor.file_name}?",
            QtWidgets.QMessageBox.StandardButton.Save
            | QtWidgets.QMessageBox.StandardButton.Discard
            | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Save,
        )
        if choice == QtWidgets.QMessageBox.StandardButton.Cancel:
            return False
        if choice == QtWidgets.QMessageBox.StandardButton.Save:
            return editor._save_document(editor.file_name)
        return choice == QtWidgets.QMessageBox.StandardButton.Discard

    def close_document(self, index: int) -> bool:
        page = self.documentTabs.widget(index)
        if page is None or not self._confirm_close(page):
            return False
        self.documentTabs.removeTab(index)
        self._dispose_document(page)
        if not self.documentTabs.count():
            self._add_empty_document()
        return True

    def _dispose_document(self, page: QtWidgets.QWidget) -> None:
        editor = self._editors.pop(page)
        if self.batchEditDialog is not None and self.batchEditDialog.editor is editor:
            self.batchEditDialog.setEditor(self.current_editor)
        editor.undoStack.clear()
        editor.undoStack.deleteLater()
        editor.editMenu.deleteLater()
        editor.undoAction.deleteLater()
        editor.redoAction.deleteLater()
        editor.copyAnimationSettingsAction.deleteLater()
        editor.pasteAnimationSettingsAction.deleteLater()
        editor.batchEditAction.deleteLater()
        editor.setArtworkReloadEnabled(False)
        editor.artworkWatcher.deleteLater()
        editor.artworkReloadTimer.deleteLater()
        editor.openGLWidget.timer.stop()
        pyglet.clock.unschedule(editor._playing_animation)
        if editor.newWindow is not None:
            editor.newWindow.close()
        widget = editor.openGLWidget
        widget.cleanup_gl_resources()
        editor._shadow_display_images.clear()
        self._saved_states.pop(page, None)
        page.deleteLater()
        self._holders.pop(page).deleteLater()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if not all(self._confirm_close(page) for page in self._editors):
            event.ignore()
            return
        self._saveWindowLayout()
        if self.batchEditDialog is not None:
            self.batchEditDialog.setEditor(None)
            self.batchEditDialog.close()
        self.menuBar().clear()
        self._active_editor = None
        for page in list(self._editors):
            self._dispose_document(page)
        super().closeEvent(event)


def excepthook(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_tb: TracebackType | None,
) -> None:
    tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(tb)


if __name__ == "__main__":
    sys.excepthook = excepthook
    app = QtWidgets.QApplication(sys.argv)
    mainWindow = SpriteEditorWindow(app)
    mainWindow.show()
    sys.exit(app.exec())
