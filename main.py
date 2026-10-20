from __future__ import annotations

import copy
import math
import os
import pathlib
import sys
import shutil
import tempfile
import traceback
import xml.etree.ElementTree as ElementTree
from collections import defaultdict
from functools import partial
from typing import Optional, Tuple, Set, Union, Literal
import pyglet
from PIL import Image, ImageDraw

pyglet.options.com_mta = False
import warnings

warnings.simplefilter("ignore", UserWarning)
sys.coinit_flags = 2

from PySide6 import QtCore, QtWidgets, QtGui
from PySide6.QtCore import QSettings, QFileInfo
from PySide6.QtGui import QWheelEvent, QPixmap, QImage, QKeyEvent, QSurfaceFormat
from PySide6.QtWidgets import QFileDialog, QListWidgetItem, QInputDialog
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from pyglet.gl import *
from pyglet.math import clamp

from data import *
from formats import validate_source, validate_export, detect_format
from gui.batchadd import Ui_BatchCreateAction
from gui.editor import Ui_MainWindow
from utils import (FormatError, integer, TopLeftGrid, Camera, check_duplicate_images, round_up_to_mult, center_and_apply_offset,
                   get_action_points_from_image, get_action_points_from_pil_image, get_shadow_location_from_pil_image,
                   create_plus_image, overlap_colors, shadow_image, template_image, write_xml)

pyglet.image.Texture.default_min_filter = GL_NEAREST
pyglet.image.Texture.default_mag_filter = GL_NEAREST


class LoadedSheetFrame(QListWidgetItem):
    def __init__(self, text, idx, image, label, editor: AnimationEditor):
        super().__init__(text)
        self.image: pyglet.image.ImageData = image
        self.label = label
        self.editor = editor
        self.idx = idx

        data = self.image.get_image_data().get_data('RGBA', -self.image.width * 4)
        self.qim = QImage(data, image.width, image.height, QImage.Format.Format_RGBA8888).copy()
        editor.sheet_preview_images[idx] = self.qim

    # Define the double-click event handler
    def mouseDoubleClickEvent(self, event):
        self.editor.add_new_animation_frame(self.idx)

    def mouseClickEvent(self, event):
        self.editor.selectSheetFrame(self.idx)


class AnimGroupItem(QListWidgetItem):
    def __init__(self, anim_group: 'AnimGroup', editor: AnimationEditor):
        self.anim_group = anim_group
        self.editor = editor
        text = self._get_anim_text()
        super().__init__(text)
        self.editor = editor

    def update_text(self):
        self.setText(self._get_anim_text())

    def _get_anim_text(self):
        text = f"{self.anim_group.idx if self.anim_group.idx >= 0 else '?'}. {self.anim_group.name}"
        if self.anim_group.copy_name:
            text += f" [{self.anim_group.copy_name}]"

        if self.anim_group.modified:
            text += " *"

        return text

    def mouseClickEvent(self, event):
        self.editor.current_anim_group = self.anim_group

        self.editor.current_sequence = self.anim_group.directions[self.editor.current_direction]

        self.editor.clear_anim_frame()

        if self.editor.current_sequence.frames:
            self.editor.current_anim_frame = self.editor.current_sequence.frames[0]

            self.editor.set_sequence_list()

            self.editor.ui.animationFrameList.setCurrentRow(0)

            self.editor.set_anim_frame_values(self.editor.current_anim_frame)

            self.editor.ui.statusBar.showMessage(f"Active action: {self.anim_group.name}.")


class AnimFrameItem(QListWidgetItem):
    def __init__(self, anim_frame: AnimFrame, editor: AnimationEditor):
        self.anim_frame = anim_frame
        self.editor = editor
        text = self._get_text()
        super().__init__(text)
        self.update_text()

    def update_text(self):
        self.setText(self._get_text())

    def _get_text(self):
        text = f"#{self.anim_frame.frame_index}"
        rush, hit, returned = self.editor.current_anim_group.event_frames(self.editor.current_sequence)

        if self.anim_frame.idx == hit:
            text += " (HF)"
        if self.anim_frame.idx == rush:
            text += " (RF)"
        if self.anim_frame.idx == returned:
            text += " (RTF)"

        return text

    def mouseClickEvent(self, event):
        self.editor.set_anim_frame_values(self.anim_frame)

        if not self.editor.animating:
            if self.anim_frame == self.editor.current_anim_frame:
                return

            self.editor.current_anim_frame = self.anim_frame
            self.editor.ui.frameSlider.setValue(self.anim_frame.idx)
            self.editor.set_animation()


class QtPygletContext(pyglet.gl.base.Context):
    """Track pyglet objects in the native context owned by Qt."""

    def __init__(self, qt_context):
        super().__init__(None)
        self.canvas = qt_context

    def _safe_to_operate_on(self):
        return (self.canvas is not None
                and QtGui.QOpenGLContext.currentContext() is self.canvas
                and super()._safe_to_operate_on())

    def _safe_to_operate_on_object_space(self):
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

    def __init__(self, width, height, parent, mainWindow, editor: AnimationEditor):
        super().__init__(parent)
        self.mainWindow = mainWindow
        self.setMinimumSize(width, height)
        self.editor = editor

        self._pyglet_context = None
        self._restore_preview = False
        self.lines = []
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._pyglet_update)
        self.timer.setInterval(0)
        self.timer.start()

        self.focus_point = self.width() // 2, self.height() // 3

        self.camera = Camera(self, self.focus_point)
        self.cameraLocked = True
        self.panOffset = QtCore.QPointF()
        self.dragPosition = None
        self.showSpriteBounds = editor.settings.value('preferences/showSpriteBounds', False, bool)
        self.spriteBounds = None
        self.setCameraLocked(editor.settings.value('preferences/cameraLocked', True, bool))

        self.elapsed = 0

        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

    def keyPressEvent(self, event: QKeyEvent):
        super().keyPressEvent(event)
        self.makeCurrent()

        if event.key() == QtCore.Qt.Key.Key_O:
            self.editor.ui.showActionPointsCheckBox.toggle()
        elif event.key() == QtCore.Qt.Key.Key_S:
            if self.editor.shadow:
                self.editor.shadow.visible = not self.editor.shadow.visible

    def setCameraLocked(self, locked):
        locked = bool(locked)
        if locked != self.cameraLocked:
            self._update_camera_focus()
            self.panOffset = (QtCore.QPointF() if locked else
                              QtCore.QPointF(self.camera.x - self.width() // 2,
                                            self.camera.y - self.height() // 3))
            self.cameraLocked = locked
            self.dragPosition = None
            self._update_camera_focus()
        self.setCursor(QtCore.Qt.CursorShape.ArrowCursor if locked else QtCore.Qt.CursorShape.OpenHandCursor)
        self.update()

    def mousePressEvent(self, event):
        if not self.cameraLocked and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.dragPosition = event.position()
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (not self.cameraLocked and self.dragPosition is not None
                and event.buttons() & QtCore.Qt.MouseButton.LeftButton):
            delta = event.position() - self.dragPosition
            self.panOffset += QtCore.QPointF(-delta.x() / self.camera.zoom, delta.y() / self.camera.zoom)
            self.dragPosition = event.position()
            self._update_camera_focus()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.dragPosition is not None:
            self.dragPosition = None
            self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QWheelEvent):
        super().wheelEvent(event)
        self.makeCurrent()

        if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            if self.editor.sprite:
                if event.angleDelta().y() > 0:
                    self.editor.ui.spriteOpacitySlider.setValue(
                        max(self.editor.ui.spriteOpacitySlider.value() - 50, 0))
                else:
                    self.editor.ui.spriteOpacitySlider.setValue(
                        min(self.editor.ui.spriteOpacitySlider.value() + 50, 255))
        else:
            if event.angleDelta().y() > 0:
                self.camera.zoom *= 2.0
            else:
                self.camera.zoom /= 2.0

        self.view = pyglet.math.Mat4()
        event.accept()

    def _pyglet_update(self):
        # Tick the pyglet clock, so scheduled events can work.
        pyglet.clock.tick()

        # Force widget to update, otherwise paintGL will not be called.
        self.update()

    def paintGL(self):
        if self._pyglet_context is None:
            return
        self._activate_context()
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)

        self._update_camera_focus()
        self._update_sprite_bounds()
        with self.camera:
            self.batch.draw()

    def _update_camera_focus(self):
        if not self.cameraLocked:
            self.camera.x = self.width() // 2 + self.panOffset.x()
            self.camera.y = self.height() // 3 + self.panOffset.y()
        elif self.camera.zoom > 1 and self.editor.sprite is not None:
            self.camera.x, self.camera.y = self.editor.sprite.position[:2]
        else:
            self.camera.x = self.width() // 2
            self.camera.y = self.height() // 3

    def _update_sprite_bounds(self):
        sprite = self.editor.sprite
        if sprite is None or not self.showSpriteBounds:
            if self.spriteBounds is not None:
                self.spriteBounds.visible = False
            return
        if self.spriteBounds is None:
            self.spriteBounds = pyglet.shapes.Box(0, 0, sprite.width, sprite.height,
                                                 color=(255, 220, 0), batch=self.batch,
                                                 group=pyglet.graphics.Group(3))
        self.spriteBounds.position = (sprite.x - sprite.image.anchor_x * sprite.scale * sprite.scale_x,
                                      sprite.y - sprite.image.anchor_y * sprite.scale * sprite.scale_y)
        self.spriteBounds.width = sprite.width
        self.spriteBounds.height = sprite.height
        self.spriteBounds.thickness = 1 / self.camera.zoom
        self.spriteBounds.visible = True

    def resizeGL(self, width, height):
        self._activate_context()
        self.projection = pyglet.math.Mat4.orthogonal_projection(0, width, 0, height, -255, 255)

        self.viewport = 0, 0, width, height

        self.focus_point = width // 2, height // 3

        for line in self.lines:
            line.delete()
        self.lines = [
            pyglet.shapes.Line(0, self.height() // 3, self.width(), self.height() // 3, batch=self.batch),
            pyglet.shapes.Line(self.width() // 2, 0, self.width() // 2, self.height(), batch=self.batch)
        ]

        if self.editor.sprite:
            self.editor.sprite.position = self.editor.get_sprite_position()
            self.editor.shadow.position = self.editor.get_shadow_position()
            self.editor.updateActionPointPreview(self.editor.current_anim_frame)
        elif self._restore_preview:
            self._restore_preview = False
            self.editor.set_animation()
        self._update_camera_focus()

    def initializeGL(self):
        """Call anything that needs a context to be created."""
        self._pyglet_context = QtPygletContext(self.context())
        self._activate_context()
        self.context().aboutToBeDestroyed.connect(self.cleanup_gl_resources)
        self._projection_matrix = pyglet.math.Mat4()
        self._view_matrix = pyglet.math.Mat4()

        self.batch = pyglet.graphics.Batch()

        try:
            self._default_program = pyglet.graphics.shader.ShaderProgram(
                pyglet.graphics.shader.Shader(self._default_vertex_source, 'vertex'),
                pyglet.graphics.shader.Shader(self._default_fragment_source, 'fragment'))
        except pyglet.graphics.shader.ShaderException:
            self.error_dialog = QtWidgets.QMessageBox()
            self.error_dialog.setWindowTitle("Error")
            self.error_dialog.setIcon(QtWidgets.QMessageBox.Icon.Critical)
            self.error_dialog.setText(f"Could not compile shader. Requires OpenGL 3.3 capability.")
            self.error_dialog.exec()
            sys.exit(app.exec())

        self.ubo = self._default_program.uniform_blocks['WindowBlock'].create_ubo()

        glClearColor(0.5, 0.5, 0.5, 1.0)

        self.view = pyglet.math.Mat4()
        self.projection = pyglet.math.Mat4.orthogonal_projection(0, self.width(), 0, self.height(), -255, 255)
        self.viewport = 0, 0, self.width(), self.height()

    def _activate_context(self):
        if self._pyglet_context is not None:
            self._pyglet_context.set_current()

    def makeCurrent(self):
        super().makeCurrent()
        self._activate_context()

    def doneCurrent(self):
        if pyglet.gl.current_context is self._pyglet_context:
            pyglet.gl.current_context = None
            pyglet.gl.gl_info.remove_active_context()
        super().doneCurrent()

    def cleanup_gl_resources(self):
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
        textures = {}

        def remember_texture(texture):
            while isinstance(texture, pyglet.image.TextureRegion):
                texture = texture.owner
            if isinstance(texture, pyglet.image.Texture):
                textures[id(texture)] = texture

        images = [editor.sheet_image, editor.action_pt_image,
                  editor.shadow_image, editor.action_point_marker,
                  *editor._shadow_display_images.values()]
        for grid in (editor.image_grid, editor.action_grid):
            if grid is not None:
                images.extend(grid._items or [])
                remember_texture(grid._texture_grid)
                grid._texture_grid = None
        for image in images:
            if image is not None:
                remember_texture(image)
                for name in ('_current_texture', '_current_mipmap_texture'):
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
                if hasattr(domain, 'index_buffer'):
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
        programs = {id(program): program for program in context._cached_programs.values()}
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
    def viewport(self):
        return self._viewport

    @viewport.setter
    def viewport(self, values):
        self._viewport = values
        pr = 1.0
        x, y, w, h = values
        pyglet.gl.glViewport(int(x * pr), int(y * pr), int(w * pr), int(h * pr))

    @property
    def projection(self):
        return self._projection_matrix

    @projection.setter
    def projection(self, matrix: pyglet.math.Mat4):
        with self.ubo as window_block:
            window_block.projection[:] = matrix

        self._projection_matrix = matrix

    @property
    def view(self):
        return self._view_matrix

    @view.setter
    def view(self, matrix: pyglet.math.Mat4):

        with self.ubo as window_block:
            window_block.view[:] = matrix

        self._view_matrix = matrix


class BatchAddImplementation:
    def __init__(self, window: QtWidgets.QWidget, editor: AnimationEditor, ui: 'Ui_BatchCreateAction'):
        self.window = window
        self.ui = ui
        self.editor = editor

        self.ui.openDirectory.clicked.connect(lambda: self.open_directory())

        self.loaded_frame_data()

        self.ui.finalizeButtonBox.clicked.connect(self.finalize_click)

    def apply(self):

        if self.ui.copyComboBox.currentText() == self.ui.actionComboBox.currentText():

            self.error_dialog = QtWidgets.QMessageBox()
            self.error_dialog.setWindowTitle("Warning")
            self.error_dialog.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            self.error_dialog.setText("Both boxes cannot be the same name.")
            self.error_dialog.exec()

        else:
            if not self.ui.directoryLineEdit.text():
                return

            use_action: AnimGroup = self.ui.actionComboBox.currentData()
            copy_action: AnimGroup = self.ui.copyComboBox.currentData()

            overwrite = self.ui.overwriteCheckbox.isChecked()
            fulldata = self.ui.fullDataCheckbox.isChecked()

            _use_idx = self.ui.indexSpinBox.value()
            group_idx = _use_idx if _use_idx >= 0 else use_action.idx
            collapse = self.editor.ui.actionCollapse_Singles.isChecked()

            ct = 0
            for dirpath, dirnames, filenames in os.walk(self.ui.directoryLineEdit.text()):
                for filename in filenames:
                    if filename == "FrameData.xml":
                        file_name = os.path.join(dirpath, filename)
                        data = ElementTree.parse(file_name)

                        root = data.getroot()

                        anims_el = root.find("Anims")

                        exists = False
                        write = False
                        for action_anim in anims_el:
                            for anim_el in action_anim:
                                if anim_el.tag == "Name":
                                    # Found one...
                                    if anim_el.text.lower() == use_action.name.lower():
                                        exists = True

                                        if overwrite:
                                            # Clear existing anim.
                                            action_anim.clear()
                                            if self.editor.create_base_anim_group_xml(action_anim, use_action.name, group_idx,
                                                                                  copy_action,
                                                                                  trim=not fulldata,
                                                                                  copy_name=copy_action.name):
                                                self.editor.create_single_sheet_frame_data(action_anim, copy_action, collapse)

                                            write = True
                                        else:
                                            break

                        if not exists:
                            anim_el = ElementTree.SubElement(anims_el, "Anim")
                            if self.editor.create_base_anim_group_xml(anim_el, use_action.name, group_idx, copy_action,
                                                                  trim=not fulldata,
                                                                  copy_name=copy_action.name):
                                self.editor.create_single_sheet_frame_data(anim_el, copy_action, collapse)
                            write = True

                        if write:
                            ElementTree.indent(root)

                            tree = ElementTree.ElementTree(root)
                            write_xml(tree, file_name)

                            ct += 1

            error_dialog = QtWidgets.QMessageBox()
            error_dialog.setWindowTitle("Complete")
            error_dialog.setIcon(QtWidgets.QMessageBox.Icon.Information)
            error_dialog.setText(f"Operation completed with {ct} changes.")
            error_dialog.exec()

    def finalize_click(self, button):
        role = self.ui.finalizeButtonBox.buttonRole(button)
        if role == QtWidgets.QDialogButtonBox.ButtonRole.ApplyRole:
            self.apply()
        elif role == QtWidgets.QDialogButtonBox.ButtonRole.RejectRole:
            self.window.hide()

    def loaded_frame_data(self):
        self.ui.copyComboBox.clear()
        self.ui.actionComboBox.clear()

        self.fill_action_list()
        self.fill_copy_list()

    def fill_action_list(self):
        for a in self.editor.groups:
            self.ui.actionComboBox.addItem(a.name, userData=a)

    def fill_copy_list(self):
        for a in self.editor.groups:
            self.ui.copyComboBox.addItem(a.name, userData=a)

    def open_directory(self):
        directory = QFileDialog.getExistingDirectory(self.window, "Select Directory")

        self.ui.directoryLineEdit.setText(directory)


REDUCE_RUSH_FRAMES = False


class AnimationEditor:
    shadow_image: Optional[pyglet.image.AbstractImage]
    sprite: Optional[pyglet.sprite.Sprite]
    ANIMATION_SPEED_SLIDER_VALUES = (0.1, 0.25, 0.5, 1, 2)
    MAX_RECENT = 5

    def __init__(self, app: QtWidgets.QApplication, window: QtWidgets.QMainWindow, ui: Ui_MainWindow,
                 document_host=None):
        self._configure_open_gl()
        self.app = app
        self.window = window
        self.ui = ui
        self.document_host = document_host
        self.groups: List[AnimGroup] = []
        self.file_name = ''
        self.sheet_format = None
        self.shadow_size_present = True
        self.loaded_shadow_size = 0
        self.loaded_tree: Optional[ElementTree] = None
        self.batch_add_implem: Optional[BatchAddImplementation] = None

        self.settings = QSettings('MDFrameEditor', 'Frame Editor')
        self.recent_files = self.settings.value('recent', [], list)
        self.ui.sheetFramePicture.includeWhitespace = self.settings.value('preferences/includeWhitespace', False, bool)
        self.ui.sheetFramePicture.showBounds = self.settings.value('preferences/showSpriteBounds', False, bool)

        self.enable_trim = self.settings.value('trim', True, bool)
        self.enable_collapse = self.settings.value('collapse', True, bool)

        self.ui.actionCollapse_Singles.setChecked(self.enable_collapse)
        self.ui.actionTrim_Copies.setChecked(self.enable_trim)

        self.ui.actionCollapse_Singles.changed.connect(lambda: self.save_collapse())
        self.ui.actionTrim_Copies.changed.connect(lambda: self.save_trim())

        self.ui.actionExit.triggered.connect(lambda: self.exit_application())

        self.recentFileActions = []

        for i in range(self.MAX_RECENT):
            action = QtGui.QAction(self.window, visible=False)
            action.triggered.connect(lambda checked=False, a=action: self.load_recent_file(a))
            self.recentFileActions.append(action)
            self.ui.menuRecent.addAction(action)

        self._update_recent_actions()

        self.scale = 2.0  # default sprite scaling.

        self.anim_speed = 1 / 60
        self.sheet_image: Optional[pyglet.image.ImageData] = None
        self.image_grid: Optional[TopLeftGrid] = None

        self.action_pt_image: Optional[pyglet.image.ImageData] = None
        self.action_grid: Optional[TopLeftGrid] = None
        self.action_points: dict[int, ActionPoints] = {}
        self.selected_sheet_frame = None
        self.sheet_preview_images = {}
        self.loaded_action_points = {}

        self.shadow_image = self._shadow_image_data(template_image())
        self._shadow_size = 0
        self._shadow_display_images = {}

        self.sprite = None
        self.shadow: Optional[pyglet.sprite.Sprite] = None

        self.marker_size = 5
        self.action_point_marker = create_plus_image(self.marker_size, (255, 255, 255, 255))
        self.action_point_marker.anchor_x = self.marker_size // 2
        self.action_point_marker.anchor_y = self.marker_size // 2
        self.left_hand: Optional[pyglet.sprite.Sprite] = None
        self.center: Optional[pyglet.sprite.Sprite] = None
        self.right_hand: Optional[pyglet.sprite.Sprite] = None
        self.head: Optional[pyglet.sprite.Sprite] = None
        self.shadow_point: Optional[pyglet.sprite.Sprite] = None

        self.ap_sprites = []

        self.current_direction = 0
        self.animating = False
        self.newWindow = None

        self.single_loaded = None

        # Set when opened for non-XML access.
        self.frame_width = 0
        self.frame_height = 0
        self.shadow_size = 0

        self.copied_sequence: Optional[AnimationSequence] = None

        # self.window.setFixedSize(670, 836)

        self.current_sequence: Optional[AnimationSequence] = None
        self.current_anim_frame: Optional[AnimFrame] = None
        self.displayed_anim_frame: Optional[AnimFrame] = None
        self.current_anim_group: Optional[AnimGroup] = None

        self.ui.actionLoad.triggered.connect(lambda: self._load_animation_dialog())

        self.ui.playButton.clicked.connect(lambda: self.play_animation())

        self.ui.frameDownReorderButton.clicked.connect(lambda: self.move_frame_down())
        self.ui.frameUpReorderButton.clicked.connect(lambda: self.move_frame_up())

        self.ui.animationFrameList.itemClicked.connect(lambda item: item.mouseClickEvent(None))

        self.directionButtons = [self.ui.buttonDown, self.ui.buttonDownLeft, self.ui.buttonLeft, self.ui.buttonUpLeft,
                                 self.ui.buttonUp, self.ui.buttonUpRight, self.ui.buttonRight, self.ui.buttonDownRight]

        for dir_idx, button in enumerate(self.directionButtons):
            button.clicked.connect(partial(self.set_direction, self.directionButtons.index(button)))
            button.setCheckable(True)

        self.ui.buttonDown.toggle()

        self.openGLWidget = self.ui.openGLWidget = PygletWidget(240, 220, self.ui.verticalFrame_3, self.window, self)
        self.openGLWidget.setMinimumSize(QtCore.QSize(240, 220))
        self.openGLWidget.setMaximumSize(QtCore.QSize(16777215, 16777215))
        self.openGLWidget.setObjectName("openGLWidget")
        self.ui.gridLayout.addWidget(self.openGLWidget, 0, 0, 1, 1)

        self.ui.frameSlider.setMaximum(0)

        self.ui.frameIndexSpinBox.valueChanged.connect(lambda: self.frame_index_changed())

        self.ui.copySequenceButton.clicked.connect(lambda: self.copy_sequence())
        self.ui.pasteSequenceButton.clicked.connect(lambda: self.paste_sequence())

        self.ui.xSpinBox.valueChanged.connect(lambda: self.sprite_offset_changed())
        self.ui.ySpinBox.valueChanged.connect(lambda: self.sprite_offset_changed())

        self.ui.xShadowSpinbox.valueChanged.connect(lambda: self.shadow_offset_changed())
        self.ui.yShadowSpinBox.valueChanged.connect(lambda: self.shadow_offset_changed())

        self.ui.durationSpinBox.valueChanged.connect(lambda: self.duration_changed())
        self.ui.mirroredCheckbox.clicked.connect(lambda: self.flip_changed())

        self.ui.frameSlider.valueChanged.connect(lambda: self.slider_change())
        self.ui.frameDuplicateButton.clicked.connect(lambda: self.duplicate_frame())
        self.ui.frameDeleteButton.clicked.connect(lambda: self.delete_selected_frames())

        self.ui.loadedSheetFrameList.itemDoubleClicked.connect(lambda item: item.mouseDoubleClickEvent(None))
        self.ui.loadedSheetFrameList.currentRowChanged.connect(
            lambda row: self.selectSheetFrame(self.ui.loadedSheetFrameList.item(row).idx if row >= 0 else None))

        self.ui.actionListWidget.itemActivated.connect(lambda item: item.mouseClickEvent(None))

        self.ui.animationSpeedSlider.valueChanged.connect(lambda: self.change_animation_speed())

        self.ui.actionAddButton.clicked.connect(lambda: self.open_add_action())
        self.ui.actionDuplicateButton.clicked.connect(lambda: self.duplicate_action())
        self.ui.actionDeleteButton.clicked.connect(lambda: self.delete_action())

        self.ui.actionSave.triggered.connect(lambda: self.save_action_trigger())
        self.ui.actionSave_As.triggered.connect(lambda: self.save_as_action_trigger())

        self.ui.defaultFrameButton.clicked.connect(lambda: self.default_frame_click())

        self.ui.menuBatch.triggered.connect(lambda: self.open_batch_add())

        self.ui.returnPointButton.clicked.connect(lambda: self.set_return_point())
        self.ui.hitPointButton.clicked.connect(lambda: self.set_hit_point())
        self.ui.rushPointButton.clicked.connect(lambda: self.set_rush_point())

        self.ui.actionExportAll_Animations.triggered.connect(lambda: self.export_multiple_sheets())
        self.ui.actionExportSingle_Animation.triggered.connect(lambda: self.export_single_sheet())

        self.ui.shadowSizeComboBox.currentIndexChanged.connect(self.apply_shadow_size)
        self.ui.previewShadowCheckBox.toggled.connect(self._refresh_shadow_display)
        self.ui.showActionPointsCheckBox.toggled.connect(self.setActionPointsVisible)
        self.ui.spriteOpacitySlider.valueChanged.connect(partial(self.setPreviewOpacity, 'sprite'))
        self.ui.shadowOpacitySlider.valueChanged.connect(partial(self.setPreviewOpacity, 'shadow'))
        self.ui.actionPointsOpacitySlider.valueChanged.connect(partial(self.setPreviewOpacity, 'actionPoints'))
        for pointName in ('leftHand', 'rightHand', 'head', 'center'):
            for spinBox in self.ui.actionPointSpinBoxes[pointName]:
                spinBox.valueChanged.connect(partial(self.actionPointChanged, pointName))

    def setPreviewOpacity(self, target, opacity):
        getattr(self.ui, target + 'OpacityLabel').setText(f'{round(opacity * 100 / 255)}%')
        if target == 'actionPoints':
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

    def syncActionPointControls(self, animFrame):
        self.ui.shadowOffsetScopeLabel.setText(f'Current Frame: {animFrame.idx if animFrame else "-"}')
        self.selectSheetFrame(animFrame.frame_index if animFrame else None)

    def selectSheetFrame(self, index):
        if index is not None and (self.image_grid is None or not 0 <= index < len(self.image_grid)):
            index = None
        self.selected_sheet_frame = index
        with QtCore.QSignalBlocker(self.ui.loadedSheetFrameList):
            row = next((row for row in range(self.ui.loadedSheetFrameList.count())
                        if self.ui.loadedSheetFrameList.item(row).idx == index), -1)
            self.ui.loadedSheetFrameList.setCurrentRow(row)
        self.ui.sheetFrameScopeLabel.setText(f'Current Frame: {index if index is not None else "-"}')
        body = self.action_points.get(index, ActionPoints())
        for pointName, spinBoxes in self.ui.actionPointSpinBoxes.items():
            fieldName = {'leftHand': 'left_hand', 'rightHand': 'right_hand'}.get(pointName, pointName)
            offset = getattr(body, fieldName)
            for spinBox, value in zip(spinBoxes, offset.data()):
                with QtCore.QSignalBlocker(spinBox):
                    spinBox.setValue(value)
                    spinBox.setEnabled(index is not None)
        self.refreshSheetFramePreview()

    def refreshSheetFramePreview(self):
        index = self.selected_sheet_frame
        if index is None or self.image_grid is None:
            self.ui.sheetFramePicture.clear()
            return
        image = self.image_grid[index]
        qim = self.sheet_preview_images.get(index)
        if qim is None:
            data = image.get_image_data().get_data('RGBA', -image.width * 4)
            qim = QImage(data, image.width, image.height, QImage.Format.Format_RGBA8888).copy()
            self.sheet_preview_images[index] = qim
        body = self.action_points.get(index)
        points = [] if body is None else [(point.x + image.width // 2, point.y + image.height // 2, color)
                                          for point, color in zip(body.all_pos(),
                                              ((255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 0, 0)))]
        self.ui.sheetFramePicture.setFrame(qim, points, self.ui.showActionPointsCheckBox.isChecked(),
                                           self.ui.actionPointsOpacitySlider.value())

    def actionPointChanged(self, pointName, value):
        index = self.selected_sheet_frame
        if index is None:
            return
        xSpinBox, ySpinBox = self.ui.actionPointSpinBoxes[pointName]
        offset = Offset(xSpinBox.value(), ySpinBox.value())
        fieldName = {'leftHand': 'left_hand', 'rightHand': 'right_hand'}.get(pointName, pointName)
        body = self.action_points.setdefault(index, ActionPoints())
        if getattr(body, fieldName) == offset:
            return
        setattr(body, fieldName, offset)
        for groupItem in self._get_action_list_items():
            if any(other.frame_index == index for sequence in groupItem.anim_group.directions
                   for other in sequence.frames):
                groupItem.anim_group.modified = True
                groupItem.update_text()
        if not self.animating:
            self.set_animation()
        self.refreshSheetFramePreview()
        self.ui.statusBar.showMessage('Action point updated for all frames using this sheet image.', 4000)

    def setActionPointsVisible(self, visible):
        self.refreshSheetFramePreview()
        if self.sprite is not None:
            self.openGLWidget.makeCurrent()
            self.updateActionPointPreview(self.displayed_anim_frame or self.current_anim_frame)
            self.openGLWidget.update()

    @staticmethod
    def _shadow_image_data(image):
        data = pyglet.image.ImageData(image.width, image.height, 'RGBA', image.tobytes(),
                                      pitch=-image.width * 4)
        data.anchor_x = image.width // 2
        data.anchor_y = image.height // 2
        return data

    @property
    def shadow_size(self):
        return self._shadow_size

    @shadow_size.setter
    def shadow_size(self, size):
        self._shadow_size = size
        if size in (0, 1, 2):
            with QtCore.QSignalBlocker(self.ui.shadowSizeComboBox):
                self.ui.shadowSizeComboBox.setCurrentIndex(size)
            self.ui.shadowSizeComboBox.setEnabled(self.loaded_tree is not None)
        if hasattr(self, 'openGLWidget') and size in (0, 1, 2):
            self._refresh_shadow_display()

    def _display_shadow_image(self):
        if not self.ui.previewShadowCheckBox.isChecked():
            return self.shadow_image
        size = self.shadow_size
        if size not in self._shadow_display_images:
            self._shadow_display_images[size] = self._shadow_image_data(shadow_image(size, black=True))
        return self._shadow_display_images[size]

    def _refresh_shadow_display(self):
        if self.shadow is not None:
            self.openGLWidget.makeCurrent()
            self.shadow.image = self._display_shadow_image()
            self.openGLWidget.update()

    def apply_shadow_size(self, size):
        if not self.loaded_tree:
            return
        self.shadow_size = size
        # An explicit selection writes ShadowSize even if it was omitted on input.
        self.shadow_size_present = True
        for item in self._get_action_list_items():
            item.anim_group.modified = True
            item.update_text()
        self.ui.statusBar.showMessage('Shadow size set for all frames. Save to keep this change.', 5000)

    def _configure_open_gl(self) -> None:
        fmt = QSurfaceFormat()
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        fmt.setVersion(3, 3)
        QSurfaceFormat.setDefaultFormat(fmt)

    def set_return_point(self):
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item and self.current_anim_group:
            idx = self.ui.animationFrameList.row(item)
            if self.current_anim_group.return_frame != idx:
                self.current_anim_group.return_frame = idx
            elif self.current_anim_group.return_frame == idx:
                self.current_anim_group.return_frame = -1

            for frame_item in self._get_frame_list_items():
                frame_item.update_text()

    def set_hit_point(self):
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item and self.current_anim_group:
            idx = self.ui.animationFrameList.row(item)
            if self.current_anim_group.hit_frame != idx:
                self.current_anim_group.hit_frame = idx
            elif self.current_anim_group.hit_frame == idx:
                self.current_anim_group.hit_frame = -1

            for frame_item in self._get_frame_list_items():
                frame_item.update_text()

    def set_rush_point(self):
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item and self.current_anim_group:
            idx = self.ui.animationFrameList.row(item)
            if self.current_anim_group.rush_frame != idx:
                self.current_anim_group.rush_frame = idx
            elif self.current_anim_group.rush_frame == idx:
                self.current_anim_group.rush_frame = -1

            for frame_item in self._get_frame_list_items():
                frame_item.update_text()

    def open_batch_add(self):
        if not self.newWindow:
            self.newWindow = QtWidgets.QWidget()
            ba = Ui_BatchCreateAction()
            ba.setupUi(self.newWindow)
            self.batch_add_implem = BatchAddImplementation(self.newWindow, self, ba)
            self.newWindow.setWindowTitle("Batch Add Action")
        self.newWindow.show()

    def save_trim(self):
        self.settings.setValue('trim', self.ui.actionTrim_Copies.isChecked())

    def save_collapse(self):
        self.settings.setValue('collapse', self.ui.actionCollapse_Singles.isChecked())

    def default_frame_click(self):
        if self.current_sequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                selected_anim_frame: AnimFrame = item.anim_frame
                default_data = selected_anim_frame.default_copy

                selected_anim_frame.frame_index = default_data.frame_index
                selected_anim_frame.flip = default_data.flip
                selected_anim_frame.duration = default_data.duration
                selected_anim_frame.shadow_offset = Offset(default_data.shadow_offset.x, default_data.shadow_offset.y)
                selected_anim_frame.sprite_offset = Offset(default_data.sprite_offset.x, default_data.sprite_offset.y)

                item.update_text()

                if not self.animating:
                    self.set_anim_frame_values(selected_anim_frame)

                    self.set_animation()

    def save_action_trigger(self):
        if self.loaded_tree:
            self._save_document(self.file_name)

    def save_as_action_trigger(self):
        if self.loaded_tree:
            label = self.sheet_format.value
            file_name, _ = QFileDialog.getSaveFileName(self.window, f"Save {label}", self.file_name,
                                                      f"{label} data (*.xml)")
            if file_name:
                self._save_document(file_name)

    def _save_document(self, file_name):
        if not file_name:
            return False
        target = pathlib.Path(file_name)
        if self.document_host and not self.document_host.can_save_path(self, target):
            return False
        other = SheetFormat.MULTI if self.sheet_format == SheetFormat.SINGLE else SheetFormat.SINGLE
        if target.name.casefold() == other.value.casefold():
            self.create_error_popup(f"Use Export to {other.value} to convert this animation.")
            return False
        if not self._export_bundle(target.parent, self.sheet_format, target.name):
            return False
        self.file_name = str(target)
        self.loaded_tree = ElementTree.parse(target)
        self.shadow_size_present = self.loaded_tree.getroot().find('ShadowSize') is not None
        self.loaded_shadow_size = self.shadow_size
        for item in self._get_action_list_items():
            item.anim_group.modified = False
            item.update_text()
        for group in self.groups:
            for sequence in group.directions:
                for frame in sequence.frames:
                    frame.reset()
        self.add_recent_list(self.file_name)
        if self.document_host:
            self.document_host.document_saved(self)
        return True

    def _append_shadow_size(self, root):
        if self.shadow_size_present or self.shadow_size != self.loaded_shadow_size:
            ElementTree.SubElement(root, 'ShadowSize').text = str(self.shadow_size)

    def _save_frame_data(self, file_name=None):
        validate_export(self, SheetFormat.SINGLE)
        collapse = self.ui.actionCollapse_Singles.isChecked()

        root = ElementTree.Element("AnimData")

        ElementTree.SubElement(root, "FrameWidth").text = str(self.frame_width)
        ElementTree.SubElement(root, "FrameHeight").text = str(self.frame_height)
        self._append_shadow_size(root)

        anims_el = ElementTree.SubElement(root, "Anims")

        trim = self.ui.actionTrim_Copies.isChecked()

        for group_anim in self.groups:
            anim_el = ElementTree.SubElement(anims_el, "Anim")
            if self.create_base_anim_group_xml(anim_el, group_anim.name, group_anim.idx, group_anim, trim=trim,
                                           copy_name=group_anim.copy_name, allow_copy_chains=True):
                self.create_single_sheet_frame_data(anim_el, group_anim, collapse)

        ElementTree.indent(root)

        if not file_name:
            # Use loaded file name
            file_name = self.file_name

            # Reset saves.
            for item in self._get_action_list_items():
                item.anim_group.modified = False
                item.update_text()

        tree = ElementTree.ElementTree(root)
        write_xml(tree, file_name)

    def is_sequence_collapsable(self, anim_group: AnimGroup):
        ct = 0
        first = anim_group.directions[0].frames
        for direction in anim_group.directions:
            if direction.frames == first:
                ct += 1

        if ct == 8:
            # All 8 frames are the same.
            return True

        return False

    def _can_trim_copy(self, group: AnimGroup, copy_name=None):
        copy_name = group.copy_name if copy_name is None else copy_name
        source = next((candidate for candidate in self.groups if candidate.name == copy_name), None)
        if source is None:
            return False
        return (group.rush_frame, group.hit_frame, group.return_frame, group.directions) == (
            source.rush_frame, source.hit_frame, source.return_frame, source.directions)

    def _copy_target(self, group, copy_name):
        """Serialize a direct copy of a matching, fully written source."""
        target = next((item for item in self.groups if item.name == copy_name), None)
        seen = {group.name}
        while target is not None:
            if target.name in seen or not self._can_trim_copy(group, target.name):
                return ''
            seen.add(target.name)
            if not target.copy_name or not self._can_trim_copy(target):
                return target.name
            target = next((item for item in self.groups if item.name == target.copy_name), None)
        return ''

    def create_base_anim_group_xml(self, anim_el: ElementTree.Element, name: str, index: int, group: AnimGroup,
                               trim=False, copy_name="", size=None, allow_copy_chains=False) -> bool:
        ElementTree.SubElement(anim_el, "Name").text = name

        if index != -1:
            ElementTree.SubElement(anim_el, "Index").text = str(index)

        if trim:
            target = (copy_name if allow_copy_chains and self._can_trim_copy(group, copy_name)
                      else self._copy_target(group, copy_name))
            if target:
                ElementTree.SubElement(anim_el, "CopyOf").text = target
                return False

        if size:
            ElementTree.SubElement(anim_el, "FrameWidth").text = str(size[0])
            ElementTree.SubElement(anim_el, "FrameHeight").text = str(size[1])

        if group.rush_frame != -1:
            ElementTree.SubElement(anim_el, "RushFrame").text = str(group.rush_frame)

        if group.hit_frame != -1:
            ElementTree.SubElement(anim_el, "HitFrame").text = str(group.hit_frame)

        if group.return_frame != -1:
            ElementTree.SubElement(anim_el, "ReturnFrame").text = str(group.return_frame)

        return True

    def create_single_sheet_frame_data(self, anim_el: ElementTree.Element, group: AnimGroup, collapse):
        sequences_ele = ElementTree.SubElement(anim_el, "Sequences")

        is_collapsable = False
        if collapse:
            is_collapsable = self.is_sequence_collapsable(group)

        for sequence in group.directions:
            seq_ele = ElementTree.SubElement(sequences_ele, "AnimSequence")

            for frame in sequence.frames:
                frame_ele = ElementTree.SubElement(seq_ele, "AnimFrame")

                ElementTree.SubElement(frame_ele, "FrameIndex").text = str(frame.frame_index)
                ElementTree.SubElement(frame_ele, "Duration").text = str(frame.duration)
                ElementTree.SubElement(frame_ele, "HFlip").text = str(int(frame.flip))
                sprite_off = ElementTree.SubElement(frame_ele, "Sprite")
                ElementTree.SubElement(sprite_off, "XOffset").text = str(frame.sprite_offset.x)
                ElementTree.SubElement(sprite_off, "YOffset").text = str(frame.sprite_offset.y)
                shadow_off = ElementTree.SubElement(frame_ele, "Shadow")
                ElementTree.SubElement(shadow_off, "XOffset").text = str(frame.shadow_offset.x)
                ElementTree.SubElement(shadow_off, "YOffset").text = str(frame.shadow_offset.y)

            # Stop after writing a frame if we are collapsing it.
            if is_collapsable:
                break

        return True

    def create_error_popup(self, text: str):
        return QtWidgets.QMessageBox.critical(self.window, 'Error', text, QtWidgets.QMessageBox.StandardButton.Ok)

    def _get_frame_uniformity(self, group: AnimGroup) -> Union[Set[Tuple], Literal[False]]:
        uniform_durations: Set[Tuple] = set()
        for sequence in group.directions:
            durations = tuple([frame.duration for frame in sequence.frames])
            if not durations:  # Ignore empty sequences?
                continue

            uniform_durations.add(durations)

            if len(uniform_durations) > 1:
                # Check if frame count differs between directions.
                first_length = len(next(iter(uniform_durations), ()))
                for t in uniform_durations:
                    if len(t) != first_length:
                        s_frame_count = ""
                        for idx, subseq in enumerate(group.directions):
                            s_frame_count += f"{FD_STR[idx]}: {len(subseq.frames)} Frames\n"
                        self.create_error_popup(
                            f"Could not save AnimData.xml. All directions for animation {group.name} must all be the "
                            f"same number of frames.\n{s_frame_count}")
                        return False

                s_durations = ""
                for idx, sequence2 in enumerate(group.directions):
                    frame_durations = [fr.duration for fr in sequence2.frames]
                    s_durations += f"{FD_STR[idx]}: {tuple(frame_durations)} - Total: ({sum(frame_durations)})\n"

                # If frame counts are fine, then the durations are messed up.
                self.create_error_popup(
                    f"Could not save AnimData.xml. Duration values for {group.name} must match for all directions for "
                    f"each frame.\n{s_durations}")
                return False

        return uniform_durations

    def create_multi_sheet_frame_data(self, anim_el: ElementTree.Element, group: AnimGroup):
        """This essentially just includes the durations of each frame. Limited in that durations are set for the whole
        animation regardless of direction."""
        uniform_durations = self._get_frame_uniformity(group)
        if not uniform_durations:
            return False

        duration_ele = ElementTree.SubElement(anim_el, "Durations")

        for durations in uniform_durations:
            for value in durations:
                ElementTree.SubElement(duration_ele, "Duration").text = str(value)

        return True

    def frame_index_changed(self):
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

    def _move_frame(self, row_dir: int):
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
        self.current_sequence.frames[current_pos], self.current_sequence.frames[new_pos] = self.current_sequence.frames[
            new_pos], self.current_sequence.frames[current_pos]
        new_item.anim_frame.idx, swap_item.anim_frame.idx = swap_item.anim_frame.idx, new_item.anim_frame.idx

        self.clear_copy_group()

        self.ui.frameSlider.setValue(item.anim_frame.idx)
        self.ui.animationFrameList.setCurrentRow(new_pos)

    def clear_copy_group(self):
        self.current_anim_group.copy_name = ""

    def copy_sequence(self):
        if self.current_sequence:
            self.copied_sequence = copy.deepcopy(self.current_sequence)

    def paste_sequence(self):
        if self.current_sequence:
            if self.copied_sequence:
                self.current_sequence = self.copied_sequence
                self.current_anim_group.replace_sequence(self.current_direction, self.copied_sequence)
                self.clear_copy_group()
                self.current_anim_frame = self.copied_sequence.frames[0]
                self.copied_sequence = None
                self.set_sequence_list()
                self.ui.animationFrameList.setCurrentRow(0)
                self.set_anim_frame_values(self.current_anim_frame)

    def move_frame_up(self):
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item:
            self._move_frame(-1)

    def move_frame_down(self):
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item:
            self._move_frame(1)

    def add_recent_list(self, path):
        path = str(path)
        if self.document_host:
            self.recent_files = self.settings.value('recent', [], list)
        # Push to top.
        try:
            self.recent_files.remove(path)
        except ValueError:
            pass

        self.recent_files.insert(0, path)

        self.recent_files = self.recent_files[:self.MAX_RECENT]

        self.settings.setValue('recent', self.recent_files)

        self._update_recent_actions()
        if self.document_host:
            self.document_host.refresh_recent_files()

    def _update_recent_actions(self):
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

    def delete_action(self):
        current: AnimGroupItem = self.ui.actionListWidget.currentItem()

        if current:

            ret = QtWidgets.QMessageBox.question(self.window, '',
                                                 f"Are you sure you want to delete {current.anim_group.name}?",
                                                 QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
            if ret == QtWidgets.QMessageBox.StandardButton.Yes:
                row = self.ui.actionListWidget.row(current)
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

    def duplicate_action(self):
        current: AnimGroupItem = self.ui.actionListWidget.currentItem()

        if not current:
            self.ui.statusBar.showMessage("Select an action to duplicate.", 5000)
            return

        name, ok = QInputDialog.getText(self.window, 'Duplicate Action', 'Enter an action name to replace or add.')
        if not ok:
            return

        index_id, ok = QInputDialog.getText(self.window, 'Duplicate Action',
                                           'Specify an available index ID. Leave blank to generate one.')

        if not ok:
            return

        try:
            index_id = int(index_id)
        except ValueError:
            index_id = None

        existing_group: Optional[AnimGroup] = None
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

                self.ui.statusBar.showMessage(f"Action: {name} already existed. It has been replaced.", 5000)

            else:
                if not index_id:
                    index_id = self.groups[-1].idx + 1

                group = AnimGroup(index_id, name, selected_group.rush_frame, selected_group.hit_frame,
                                  selected_group.return_frame, copy.deepcopy(selected_group.directions))
                group.copy_name = selected_group.name
                self.groups.append(group)
                item = AnimGroupItem(group, self)
                self.ui.actionListWidget.addItem(item)

                self.ui.statusBar.showMessage(f"Action: {name} created.", 5000)

    def open_add_action(self):
        name, ok = QInputDialog.getText(self.window, 'Create New Action', 'Enter an action name.')
        if not ok:
            return

        index_id, ok = QInputDialog.getText(self.window, 'Create New Action',
                                           'Specify an available index ID. Leave blank to generate one.')

        if not ok:
            return

        try:
            index_id = int(index_id)
        except ValueError:
            index_id = None

        if self.groups and ok and name:

            for group in self.groups:
                if group.name == name:
                    self.ui.statusBar.showMessage(f"Action: {name} already exists.", 5000)
                    return

                if group.idx == index_id:
                    self.ui.statusBar.showMessage(f"Index ID {index_id} already exists.", 5000)
                    return

            if index_id is None:
                index_id = self.groups[-1].idx + 1

            group = AnimGroup(index_id, name)
            self.groups.append(group)
            item = AnimGroupItem(group, self)
            self.ui.actionListWidget.addItem(item)

    def _notify_changes(self):
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

    def change_animation_speed(self):
        new_speed = self.ANIMATION_SPEED_SLIDER_VALUES[self.ui.animationSpeedSlider.value()]

        self.anim_speed = (1 / 60.0) / new_speed

        self.ui.animationSpeedLabel.setText(f"{new_speed}x")

    def duration_changed(self):
        if self.current_sequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                anim_frame = item.anim_frame
                value = self.ui.durationSpinBox.value()
                anim_frame.duration = value

                self.clear_copy_group()

                self._notify_changes()

    def flip_changed(self):
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

    def sprite_offset_changed(self):
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

    def shadow_offset_changed(self):
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

    def duplicate_frame(self):
        """Duplicate the selected frame in the Animation Sequence List"""
        if self.current_sequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                selected_anim_frame: AnimFrame = item.anim_frame
                anim_idx = len(self.current_sequence.frames)
                anim_frame = AnimFrame(anim_idx, selected_anim_frame.frame_index, selected_anim_frame.flip,
                                      selected_anim_frame.duration,
                                      Offset(selected_anim_frame.shadow_offset.x, selected_anim_frame.shadow_offset.y),
                                      Offset(selected_anim_frame.sprite_offset.x, selected_anim_frame.sprite_offset.y))

                self.clear_copy_group()

                self._notify_changes()

                self._add_anim_frame(anim_frame)

    def delete_selected_frames(self):
        """Delete the selected frame in the Animation Sequence List"""
        if self.current_sequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                for frame in list(self.current_sequence.frames):
                    if frame == item.anim_frame:
                        self.current_sequence.frames.remove(frame)

                self.ui.animationFrameList.takeItem(self.ui.animationFrameList.row(item))
                del item

                self.clear_copy_group()

                # Just use last in list as selection.
                last_idx = self.ui.animationFrameList.count() - 1
                last_item = self.ui.animationFrameList.item(last_idx)
                self.ui.animationFrameList.setCurrentItem(last_item)
                self._update_anim_frame_widgets()

    def slider_change(self):
        if not self.animating:
            if self.current_sequence:
                if self.current_sequence.frames:
                    self.ui.animationFrameList.setCurrentRow(self.ui.frameSlider.value())
                    self.current_anim_frame = self.current_sequence.frames[self.ui.frameSlider.value()]
                    self.set_anim_frame_values(self.current_anim_frame)
                    self.set_animation()

    def set_direction(self, direction):
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
                self.current_sequence = self.current_anim_group.directions[self.current_direction]

                if self.current_sequence.frames:
                    self.current_anim_frame = self.current_sequence.frames[0]

                if not self.animating:
                    self.set_anim_frame_values(self.current_anim_frame)

                if self.current_sequence:
                    self.set_sequence_list()

                self.ui.animationFrameList.setCurrentRow(0)

    def clear_anim_frame(self):
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

    def set_sequence_list(self):
        self.ui.animationFrameList.clear()

        duration = 0
        for anim_frame in self.current_sequence.frames:
            duration += anim_frame.duration
            item = AnimFrameItem(anim_frame, self)
            self.ui.animationFrameList.addItem(item)

        self._update_anim_frame_widgets()

    def _update_anim_frame_widgets(self):
        self.ui.frameSlider.setMaximum(max(0, len(self.current_sequence.frames) - 1))
        self.ui.frameSlider.setValue(0)

        self.set_animation()

    def set_anim_frame_values(self, anim_frame):
        # Displaying a selection must not run edit handlers or detach copies.
        with (QtCore.QSignalBlocker(self.ui.frameIndexSpinBox),
              QtCore.QSignalBlocker(self.ui.durationSpinBox),
              QtCore.QSignalBlocker(self.ui.mirroredCheckbox)):
            self.ui.frameIndexSpinBox.setValue(anim_frame.frame_index)
            self.ui.durationSpinBox.setValue(anim_frame.duration)
            self.ui.mirroredCheckbox.setChecked(bool(anim_frame.flip))

        self.set_offset_data(anim_frame)
        self.syncActionPointControls(anim_frame)

    def set_offset_data(self, anim_frame):
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

    def load_recent_file(self, action):
        if action:
            if self.document_host:
                self.document_host.open_file(action.data())
            else:
                self.load_animation(action.data())

    def _load_animation_dialog(self):
        if self.document_host:
            self.document_host.open_files_dialog()
            return
        file_name, _ = QFileDialog.getOpenFileName(self.window, "Select Animation File", "",
                                                 "Animation File (*.xml)")
        if file_name:
            self.load_animation(file_name)

    def load_animation(self, file_name):
        try:
            format_name = detect_format(ElementTree.parse(file_name).getroot())
        except (OSError, ElementTree.ParseError) as error:
            self.create_error_popup(str(error))
            return False
        loaded = self.load_sheet(file_name) if format_name == SheetFormat.SINGLE else self.import_multiple_sheets(file_name)
        if loaded and self.ui.actionListWidget.count():
            self.ui.actionListWidget.setCurrentRow(0)
            self.ui.actionListWidget.item(0).mouseClickEvent(None)
        return loaded

    def load_sheet(self, file_name):
        dir_name = os.path.dirname(file_name)
        try:
            tree = validate_source(file_name, SheetFormat.SINGLE)
        except (FormatError, OSError, ElementTree.ParseError) as error:
            self.create_error_popup(str(error))
            return False

        try:
            with open(f"{dir_name}/Anim.png", 'rb') as image_file:
                sheet_image = pyglet.image.load(f"{dir_name}/Anim.png", file=image_file)
        except FileNotFoundError:
            self.ui.statusBar.showMessage("Failed to find Anim.png.", 5000)
            return

        try:
            with open(f"{dir_name}/Offsets.png", 'rb') as image_file:
                action_pt_image = pyglet.image.load(f"{dir_name}/Offsets.png", file=image_file)
        except FileNotFoundError:
            action_pt_image = None
            self.ui.statusBar.showMessage("Failed to find Offsets file... skipping.", 5000)

        self.clear()

        self.single_loaded = True

        self.sheet_image = sheet_image  # Do this after clear. Try block above so we don't clear loaded if fail loading.
        self.action_pt_image = action_pt_image

        self._parse(file_name, tree)

        if self.batch_add_implem:
            self.batch_add_implem.loaded_frame_data()
        return True

    def add_new_animation_frame(self, frame_idx: int):
        """Create new animation frame in the sequence. Adds to the end."""
        if self.current_sequence:
            anim_idx = len(self.current_sequence.frames)
            anim_frame = AnimFrame(anim_idx, frame_idx)

            self._add_anim_frame(anim_frame)

            self.clear_copy_group()

            self._notify_changes()

    def _add_anim_frame(self, anim_frame: AnimFrame):
        self.current_sequence.frames.append(anim_frame)

        item = AnimFrameItem(anim_frame, self)
        self.ui.animationFrameList.addItem(item)

        self._update_anim_frame_widgets()

    @staticmethod
    def adjust_offset(rush_frame: int, frame_num: int, rush_offset: Offset, frame_offset: Offset):
        """Calculation to truncate rush frames."""
        if frame_num > rush_frame:
            diff = frame_offset - rush_offset

            final = rush_offset + (diff // 3)
            return final

    def _add_frames_from_grid(self):
        self.ui.frameIndexSpinBox.setMaximum(len(self.image_grid) - 1)

        for idx, image in enumerate(self.image_grid):
            image: pyglet.image.ImageDataRegion
            image.anchor_x = image.width // 2
            image.anchor_y = image.height // 2
            if self.action_points:
                if idx not in self.action_points:
                    continue
            item = LoadedSheetFrame(f"Frame {idx}", idx, image, self.ui.sheetFramePicture, self)
            self.ui.loadedSheetFrameList.addItem(item)

    def _parse(self, file_name, tree=None):
        try:
            self.loaded_tree = tree if tree is not None else ElementTree.parse(file_name)
        except ElementTree.ParseError:
            self.ui.statusBar.showMessage("Failed to parse animations XML data.", 5000)
            return

        root = self.loaded_tree.getroot()

        try:
            width = int(root.find("FrameWidth").text)
            height = int(root.find("FrameHeight").text)
            shadow_size = integer(root, 'ShadowSize', 1)
        except AttributeError:
            self.ui.statusBar.showMessage("Unable to determine dimensions of XML data.", 5000)
            return

        if self.sheet_image.width % width != 0 or self.sheet_image.height % height != 0:
            warning = (f"Sheet is not evenly divisible by frame dimensions.\nImage Dimensions: "
                       f"{self.sheet_image.width}x{self.sheet_image.height}\nData Dimensions: {width}x{height}\n"
                       f"Images may be cut incorrectly. Continue anyway?")

            result = QtWidgets.QMessageBox.warning(self.window, 'Warning', warning,
                                                   QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
            if result == QtWidgets.QMessageBox.StandardButton.No:
                return

        self.frame_width = width
        self.frame_height = height
        self.shadow_size = shadow_size
        self.shadow_size_present = root.find('ShadowSize') is not None
        self.loaded_shadow_size = shadow_size

        anims = root.find('Anims')
        if anims is None:
            self.ui.statusBar.showMessage("Unable to find any Animation XML data.", 5000)
            return

        self.image_grid = TopLeftGrid(self.sheet_image,
                                     rows=self.sheet_image.height // height,
                                     columns=self.sheet_image.width // width)

        if self.action_pt_image:
            self.action_grid = TopLeftGrid(self.action_pt_image,
                                          rows=self.sheet_image.height // height,
                                          columns=self.sheet_image.width // width)

            action_center = self.action_grid[0].width // 2, self.action_grid[0].height // 2
            for idx, act_img in enumerate(self.action_grid):
                act_img: pyglet.image.ImageDataRegion
                action_point_loc = get_action_points_from_image(act_img)

                if action_point_loc[0] and action_point_loc[1] and action_point_loc[2]:
                    center = action_point_loc[1]
                    head = center
                    if action_point_loc[3]:
                        head = action_point_loc[3]

                    left_hand = action_point_loc[0]
                    right_hand = action_point_loc[2]

                    self.action_points[idx] = ActionPoints(left_hand, center, right_hand, head)

                    # Position relative to 0, 0.
                    self.action_points[idx].add(Offset(-action_center[0], -action_center[1]))


                else:
                    continue

        self._add_frames_from_grid()

        self.groups = []
        copies = []
        copy_groups = []
        for action_anim in anims:
            action_idx = -1
            rush_frame = -1
            hit_frame = -1
            return_frame = -1
            sequences = []
            copy_name = ''
            for action_element in action_anim:
                if action_element.tag == "Name":
                    name = action_element.text
                elif action_element.tag == "Index":
                    action_idx = int(action_element.text) if action_element.text and action_element.text.strip() else -1
                elif action_element.tag == "CopyOf":
                    copy_name = action_element.text
                elif action_element.tag == "RushFrame":
                    rush_frame = int(action_element.text) if action_element.text and action_element.text.strip() else -1
                elif action_element.tag == "HitFrame":
                    hit_frame = int(action_element.text) if action_element.text and action_element.text.strip() else -1
                elif action_element.tag == "ReturnFrame":
                    return_frame = int(action_element.text) if action_element.text and action_element.text.strip() else -1
                elif action_element.tag == "Sequences":
                    sequences = []
                    for sequence_element in action_element:
                        frame_seq_idx = 0
                        frames = []
                        for anim_sequences in sequence_element:
                            for frame in anim_sequences:
                                if frame.tag == "FrameIndex":
                                    frame_index = int(frame.text)

                                elif frame.tag == "Sprite":
                                    sprite_offset = Offset(integer(frame, 'XOffset'), integer(frame, 'YOffset'))

                                elif frame.tag == "Shadow":
                                    shadow_offset = Offset(integer(frame, 'XOffset'), integer(frame, 'YOffset'))

                                elif frame.tag == "HFlip":
                                    try:
                                        hflip = int(frame.text)
                                    except ValueError:
                                        hflip = int(bool(frame.text))

                                elif frame.tag == "Duration":
                                    duration = int(frame.text)

                            frames.append(
                                AnimFrame(frame_seq_idx, frame_index, hflip, duration, shadow_offset, sprite_offset))
                            frame_seq_idx += 1

                            if REDUCE_RUSH_FRAMES:
                                if rush_frame > -1:
                                    frame2 = frame_seq_idx - 1
                                    # print(frame2, rush_frame, name, frames)
                                    if frame2 > rush_frame:
                                        # print("NAME", name)
                                        # print("FRAME!", name, self.adjust_offset(rush_frame, frame2, 
                                        # frames[rush_frame].sprite_offset, sprite_offset))

                                        frames[frame2].sprite_offset = self.adjust_offset(rush_frame, frame2,
                                                                                        frames[rush_frame].sprite_offset,
                                                                                        sprite_offset)
                                        frames[frame2].shadow_offset = self.adjust_offset(rush_frame, frame2,
                                                                                        frames[rush_frame].shadow_offset,
                                                                                        shadow_offset)

                        sequence = AnimationSequence(frames)
                        sequences.append(sequence)

            if copy_name:
                group = AnimGroup(action_idx, name, copy_name=copy_name)
                copy_groups.append(group)
            else:
                if len(sequences) == 0:
                    print(f"Warning: {name} no sequences found. Generating empty sequences.")
                    for i in range(8):
                        sequences.append(AnimationSequence())

                group = AnimGroup(action_idx, name, rush_frame, hit_frame, return_frame, sequences)
            group.width = width
            group.height = height
            self.groups.append(group)
            item = AnimGroupItem(group, self)
            self.ui.actionListWidget.addItem(item)

        self._resolve_copies()

        self.ui.statusBar.showMessage("Frame data and images loaded successfully.", 3000)

        self.file_name = file_name
        self.sheet_format = SheetFormat.SINGLE
        self.loaded_action_points = copy.deepcopy(self.action_points)

        self.add_recent_list(file_name)

    def _resolve_copies(self):
        by_name = {group.name: group for group in self.groups}
        resolved = set()
        def resolve(group):
            if group.name in resolved:
                return
            if group.copy_name:
                source = by_name[group.copy_name]
                resolve(source)
                group.rush_frame, group.hit_frame, group.return_frame = source.rush_frame, source.hit_frame, source.return_frame
                group.directions = copy.deepcopy(source.directions)
            resolved.add(group.name)
        for group in self.groups:
            resolve(group)

    def _get_action_list_items(self) -> List[AnimGroupItem]:
        return [self.ui.actionListWidget.item(x) for x in range(self.ui.actionListWidget.count())]

    def _get_frame_list_items(self) -> List[AnimFrameItem]:
        return [self.ui.animationFrameList.item(x) for x in range(self.ui.animationFrameList.count())]

    def clear(self):
        """Clear everything so we can load a new sprite."""
        self.sheet_preview_images.clear()
        self.single_loaded = None
        self.sheet_image = None
        self.action_pt_image = None
        self.image_grid: Optional[TopLeftGrid] = None
        self.action_grid: Optional[TopLeftGrid] = None
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
        self.left_hand = self.center = self.right_hand = self.head = self.shadow_point = None

        self.ui.loadedSheetFrameList.clear()
        self.ui.animationFrameList.clear()
        self.ui.actionListWidget.clear()

        self.ui.sheetFramePicture.clear()

    def get_sprite_position(self):
        return ((self.openGLWidget.width() // 2) + (self.current_anim_frame.sprite_offset.x * self.scale),
                (self.openGLWidget.height() // 3) + (-self.current_anim_frame.sprite_offset.y * self.scale), 0)

    def get_shadow_position(self):
        return ((self.openGLWidget.width() // 2) + (self.current_anim_frame.shadow_offset.x * self.scale),
                (self.openGLWidget.height() // 3) + (-self.current_anim_frame.shadow_offset.y * self.scale), 0)

    def get_action_point_positions(self, anim_frame=None):
        anim_frame = anim_frame or self.current_anim_frame
        ap = self.action_points[anim_frame.frame_index]

        if not anim_frame.flip:
            lh_pos, c_pos, rh_pos, head_pos = ap.all_pos()
        else:
            width = self.image_grid[anim_frame.frame_index].width
            pivot = width - 1 - 2 * (width // 2)
            lh_pos, c_pos, rh_pos, head_pos = [Offset(pivot - point.x, point.y) for point in ap.all_pos()]

        return tuple((self.openGLWidget.width() // 2 + (anim_frame.sprite_offset.x + point.x) * self.scale,
                      self.openGLWidget.height() // 3 - (anim_frame.sprite_offset.y + point.y) * self.scale, 0)
                     for point in (lh_pos, c_pos, rh_pos, head_pos))

    def updateActionPointPreview(self, anim_frame):
        if self.sprite is None or anim_frame is None:
            return
        positions = (self.get_action_point_positions(anim_frame)
                     if anim_frame.frame_index in self.action_points else (None,) * 4)
        shadow_position = (self.openGLWidget.width() // 2 + anim_frame.shadow_offset.x * self.scale,
                           self.openGLWidget.height() // 3 - anim_frame.shadow_offset.y * self.scale, 0)
        for name, color, position in zip(
                ('left_hand', 'center', 'right_hand', 'head', 'shadow_point'),
                ((255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 0, 0), (255, 255, 255)),
                (*positions, shadow_position)):
            marker = getattr(self, name)
            if marker is None and position is not None:
                marker = pyglet.sprite.Sprite(self.action_point_marker, batch=self.openGLWidget.batch,
                                              group=pyglet.graphics.Group(3 if name == 'center' else 2))
                marker.color = color
                marker.scale = self.scale
                marker.opacity = self.ui.actionPointsOpacitySlider.value()
                setattr(self, name, marker)
                self.ap_sprites.append(marker)
            if marker is not None:
                if position is not None:
                    marker.position = position
                marker.visible = position is not None and self.ui.showActionPointsCheckBox.isChecked()

    def _playing_animation(self, dt):
        self.openGLWidget.makeCurrent()

        self._set_anim_frame_display(self.current_anim_frame)

        self.ui.frameSlider.setValue(self.current_anim_frame.idx)

        existing_frame = self.current_anim_frame

        next_frame = self.current_anim_frame.idx + 1
        if next_frame > len(self.current_sequence.frames) - 1:
            next_frame = 0

        self.current_anim_frame = self.current_sequence.frames[next_frame]

        pyglet.clock.schedule_once(self._playing_animation, self.anim_speed * existing_frame.duration)

    @property
    def animating(self):
        return self._animating

    @animating.setter
    def animating(self, playing):
        self._animating = bool(playing)
        self.ui.playButton.setPlaying(self._animating)

    def play_animation(self):
        if self.current_anim_frame:
            self.animating = not self.animating
            pyglet.clock.unschedule(self._playing_animation)

            if self.animating:
                pyglet.clock.schedule_once(self._playing_animation, self.anim_speed * self.current_anim_frame.duration)
            else:
                self.slider_change()

    def set_animation(self):
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

                    first_idx = self.current_anim_group.directions[self.current_direction].frames[0].frame_index

                    self.shadow = pyglet.sprite.Sprite(self._display_shadow_image(), x=shadow_pos[0], y=shadow_pos[1],
                                                       batch=self.openGLWidget.batch, group=pyglet.graphics.Group(0))
                    self.shadow.scale = self.scale
                    self.shadow.opacity = self.ui.shadowOpacitySlider.value()

                    self.sprite = pyglet.sprite.Sprite(self.image_grid[first_idx], x=sprite_pos[0], y=sprite_pos[1],
                                                       batch=self.openGLWidget.batch, group=pyglet.graphics.Group(1))
                    self.sprite.scale = self.scale
                    self.sprite.opacity = self.ui.spriteOpacitySlider.value()

                if self.sprite:
                    anim_frame = self.current_anim_frame if self.current_anim_frame is not None else \
                        self.current_anim_group.directions[self.current_direction].frames[0]

                    self._set_anim_frame_display(anim_frame)

    def _set_anim_frame_display(self, anim_frame: AnimFrame):
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

    def import_multiple_sheets(self, file_name):
        dir_name = os.path.dirname(file_name)

        try:
            tree = validate_source(file_name, SheetFormat.MULTI)
        except (FormatError, OSError, ElementTree.ParseError) as error:
            self.create_error_popup(str(error))
            return False
        self.loaded_tree = tree

        root = self.loaded_tree.getroot()

        anims = root.find('Anims')
        if anims is None:
            self.ui.statusBar.showMessage("Unable to find any Animation XML data.", 5000)
            return

        try:
            shadow_size = integer(root, 'ShadowSize', 1)
        except AttributeError:
            self.ui.statusBar.showMessage("Unable to determine dimensions of XML data.", 5000)
            return

        self.clear()

        self.single_loaded = False

        self.ui.statusBar.showMessage("Processing... this may take a moment.", 5000)

        self.app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

        self.groups = []
        copy_groups = []
        collapsed_anims = []
        max_width = 0
        max_height = 0
        frames = []
        frames_to_sequence = []
        for action_anim in anims:
            name = "Unknown"
            action_idx = -1
            rush_frame = -1
            hit_frame = -1
            return_frame = -1
            copy_name = ''
            frame_height = 0
            frame_width = 0
            durations = []
            for action_element in action_anim:
                if action_element.tag == "Name":
                    name = action_element.text
                elif action_element.tag == "Index":
                    action_idx = int(action_element.text) if action_element.text and action_element.text.strip() else -1
                elif action_element.tag == "CopyOf":
                    copy_name = action_element.text
                elif action_element.tag == "RushFrame":
                    rush_frame = int(action_element.text) if action_element.text and action_element.text.strip() else -1
                elif action_element.tag == "HitFrame":
                    hit_frame = int(action_element.text) if action_element.text and action_element.text.strip() else -1
                elif action_element.tag == "ReturnFrame":
                    return_frame = int(action_element.text) if action_element.text and action_element.text.strip() else -1
                elif action_element.tag == "FrameWidth":
                    frame_width = int(action_element.text)
                elif action_element.tag == "FrameHeight":
                    frame_height = int(action_element.text)
                elif action_element.tag == "Durations":
                    durations = []
                    for duration_element in action_element:
                        try:
                            duration_value = int(duration_element.text)
                        except ValueError:
                            return self.create_error_popup(
                                f"{name} animation has an invalid duration value. Cannot be {duration_element.text}")

                        if duration_value <= 0:
                            return self.create_error_popup(
                                f"{name} animation has invalid duration value. Cannot be {duration_value}")

                        durations.append(duration_value)

            # After all checks, lets create some data.
            if copy_name:
                group = AnimGroup(action_idx, name, copy_name=copy_name)
                copy_groups.append(group)
                self.groups.append(group)
                item = AnimGroupItem(group, self)
                self.ui.actionListWidget.addItem(item)
                continue

            anim_file = f"{name}-Anim.png"
            anim_image_path = os.path.join(dir_name, anim_file)
            if not os.path.exists(anim_image_path):
                return self.create_error_popup(f"{anim_file} not found.")

            offset_file = f"{name}-Offsets.png"
            offset_image_path = os.path.join(dir_name, offset_file)
            if not os.path.exists(offset_image_path):
                return self.create_error_popup(f"{offset_file} not found.")

            shadow_file = f"{name}-Shadow.png"
            shadow_image_path = os.path.join(dir_name, shadow_file)
            if not os.path.exists(shadow_image_path):
                return self.create_error_popup(f"{shadow_file} not found.")

            with Image.open(anim_image_path) as source_image:
                anim_image = source_image.convert('RGBA')
            with Image.open(offset_image_path) as source_image:
                offset_image = source_image.convert('RGBA')
            with Image.open(shadow_image_path) as source_image:
                shadow_image = source_image.convert('RGBA')

            if (shadow_image.width != anim_image.width or shadow_image.height != anim_image.height or
                    anim_image.width != offset_image.width or anim_image.height != offset_image.height or
                    offset_image.width != anim_image.width or offset_image.height != anim_image.height):
                return self.create_error_popup(f"Dimensions of Anims, Shadows, and Offsets do not match for {name}.")

            if frame_width == 0 or frame_height == 0:
                return self.create_error_popup(f"Could not find frame dimensions for {name}.")

            if anim_image.width % frame_width != 0 or anim_image.height % frame_height != 0:
                return self.create_error_popup(f"Animation must be divisible by frame dimensions for {name}.")

            frame_x_count = anim_image.width // frame_width
            sequence_count = frame_y_count = anim_image.height // frame_height

            if len(durations) != frame_x_count:
                return self.create_error_popup("Amount of frame duration does not match number of frames.")

            if sequence_count != 1 and sequence_count != 8:
                return self.create_error_popup(f"Frame count is not 1 or 8 for {name}.")

            group = AnimGroup(action_idx, name, rush_frame, hit_frame, return_frame)
            self.groups.append(group)
            item = AnimGroupItem(group, self)
            self.ui.actionListWidget.addItem(item)

            for i in range(sequence_count):
                sequence_idx = (sequence_count - i) % sequence_count

                for frame_idx in range(frame_x_count):
                    start_x, start_y = frame_idx % frame_x_count, sequence_idx
                    l, t = start_x * frame_width, start_y * frame_height
                    r, b = l + frame_width, t + frame_height

                    obounds = (l, t, r, b)

                    frame_img = anim_image.crop(obounds)

                    o_frame_box = frame_img.getbbox()
                    if o_frame_box:
                        cropped_frame = TLRectangle.from_bounds(o_frame_box)
                    else:
                        # No bounds found, it's possible the frame is empty. For example, an animation may temporarily
                        # make a character disappear/reappear. Create a frame at the center that's 1x1.
                        cropped_frame = TLRectangle(frame_width // 2, frame_height // 2, 1, 1)
                        o_frame_box = cropped_frame.bounds()

                    max_width = max(max_width, cropped_frame.width)
                    max_height = max(max_height, cropped_frame.height)

                    action_point_frame = offset_image.crop(obounds)

                    action_point_loc = get_action_points_from_pil_image(action_point_frame)

                    bounds_center = cropped_frame.center

                    action_points = ActionPoints(Offset(*bounds_center),
                                                Offset(*bounds_center),
                                                Offset(*bounds_center),
                                                Offset(*bounds_center))

                    if action_point_loc[0] and action_point_loc[1] and action_point_loc[2]:
                        center = action_point_loc[1]
                        head = center
                        if action_point_loc[3]:
                            head = action_point_loc[3]

                        left_hand = action_point_loc[0]
                        right_hand = action_point_loc[2]
                        action_points = ActionPoints(left_hand, center, right_hand, head)
                    elif action_point_loc[0] or action_point_loc[1] or action_point_loc[2] or action_point_loc[3]:
                        return self.create_error_popup(
                            f"Error decoding action points from offsets image. Frame Index: {frame_idx}")

                    # Position relative to 0, 0.
                    action_points.add(Offset(-bounds_center[0], -bounds_center[1]))

                    offset_rect = action_points.get_rect()
                    c_offset_rect = center_bounds(offset_rect)

                    max_width = max(max_width, c_offset_rect.width)
                    max_height = max(max_height, c_offset_rect.height)

                    anim_frame = AnimFrame(len(group.directions[sequence_idx].frames))

                    offset_x = cropped_frame.x - ((frame_width // 2) - (cropped_frame.width // 2))
                    offset_y = cropped_frame.y - ((frame_height // 2) - (cropped_frame.height // 2))

                    anim_frame.sprite_offset = Offset(offset_x, offset_y)
                    anim_frame.duration = durations[frame_idx]

                    if shadow_image:
                        simage = shadow_image.crop(obounds)
                        if shadow_offset := get_shadow_location_from_pil_image(simage):
                            anim_frame.shadow_offset.x = shadow_offset.x - frame_width // 2
                            anim_frame.shadow_offset.y = shadow_offset.y - frame_height // 2
                    else:
                        anim_frame.shadow_offset.x = 0
                        anim_frame.shadow_offset.y = -(cropped_frame.y - frame_height // 2) // 2

                    frames.append((frame_img.crop(o_frame_box), action_points))
                    # XML indices are optional and need not uniquely identify a
                    # group. Keep the actual frame instead of looking it up by ID.
                    frames_to_sequence.append(anim_frame)
                    group.directions[sequence_idx].frames.append(anim_frame)

            if sequence_count == 1:
                collapsed_anims.append(group)

        max_width = round_up_to_mult(max_width, 2)
        max_height = round_up_to_mult(max_height, 2)

        unique_images, old_frame_to_new_frame, unique_body_points = check_duplicate_images(frames, True)

        max_tex_size = int(math.ceil(math.sqrt(len(unique_images))))

        single_sheet_size = (max_width * max_tex_size, max_height * max_tex_size)

        # Create single sheet
        sheet = Image.new("RGBA", single_sheet_size, (0, 0, 0, 0))

        ap_sheet = Image.new("RGBA", single_sheet_size, (0, 0, 0, 0))

        ap_draw = ImageDraw.Draw(ap_sheet)

        # Map the positions of the frames to their sheet positions.
        for frame_idx, uI in enumerate(unique_images):
            diff_x = max_width // 2 - uI.width // 2
            diff_y = max_height // 2 - uI.height // 2
            start_x = max_width * (frame_idx % max_tex_size)
            start_y = (max_height * (frame_idx // max_tex_size))

            sheet.paste(uI, (start_x + diff_x, start_y + diff_y))

            # Create an Offset sheet.
            bp_start_x = start_x + max_width // 2
            bp_start_y = start_y + max_height // 2

            start_offset = Offset(bp_start_x, bp_start_y)

            bp = unique_body_points[frame_idx]
            lh = (start_offset + bp.left_hand).data()
            center = (start_offset + bp.center).data()
            rh = (start_offset + bp.right_hand).data()
            head = (start_offset + bp.head).data()

            positions = defaultdict(list)
            positions[lh].append((255, 0, 0, 255))
            positions[center].append((0, 255, 0, 255))
            positions[head].append((0, 0, 0, 255))
            positions[rh].append((0, 0, 255, 255))

            for pos, color in overlap_colors(positions).items():
                ap_draw.point(pos, fill=color)

            self.action_points[frame_idx] = unique_body_points[frame_idx]

        flipped_frames = set()
        # Now we need to go through and update the data with the correct frame indexes.
        for old_id, old_frame in enumerate(frames):
            new_frame = frames_to_sequence[old_id]
            changed_frame = old_frame_to_new_frame[old_id]
            new_frame.frame_index = changed_frame.frame_index
            new_frame.flip = changed_frame.flip

            if new_frame.flip:
                flipped_frames.add(new_frame.frame_index)
                if old_frame[0].width % 2 == 1:
                    new_frame.sprite_offset.x += 1

        # Flip back clockwise.
        for group in self.groups:
            group.directions = [group.directions[0], *reversed(group.directions[1:])]

            for sequence in group.directions:
                for frame in sequence.frames:
                    frame.reset()

        # A one-row animation has one sequence shared by all compass views.
        for collapsed_anim in collapsed_anims:
            collapsed_anim.set_single_direction(collapsed_anim.directions[0])

        self._resolve_copies()

        self.sheet_image = pyglet.image.ImageData(sheet.width, sheet.height, 'RGBA', sheet.tobytes(),
                                                 pitch=-sheet.width * 4)

        self.frame_width = max_width
        self.frame_height = max_height
        self.shadow_size = shadow_size
        self.shadow_size_present = root.find('ShadowSize') is not None
        self.loaded_shadow_size = shadow_size
        self.file_name = str(file_name)
        self.sheet_format = SheetFormat.MULTI
        self.loaded_action_points = copy.deepcopy(self.action_points)

        self.action_pt_image = pyglet.image.ImageData(ap_sheet.width, ap_sheet.height, 'RGBA', ap_sheet.tobytes(),
                                                    pitch=-ap_sheet.width * 4)

        self.image_grid = TopLeftGrid(self.sheet_image,
                                     rows=max_tex_size,
                                     columns=max_tex_size)

        self.action_grid = TopLeftGrid(self.action_pt_image,
                                      rows=max_tex_size,
                                      columns=max_tex_size)

        self._add_frames_from_grid()

        self.add_recent_list(file_name)

        self.ui.statusBar.showMessage("Frame data and images loaded successfully.", 3000)
        return True

    def _save_export_frame_data(self, file_name, frame_sizes: dict[str, Tuple[int, int]]):

        root = ElementTree.Element("AnimData")

        self._append_shadow_size(root)

        anims_el = ElementTree.SubElement(root, "Anims")

        trim = self.ui.actionTrim_Copies.isChecked()

        for group_anim in self.groups:
            anim_el = ElementTree.SubElement(anims_el, "Anim")
            size = frame_sizes[group_anim.name] if group_anim.name in frame_sizes else None
            if self.create_base_anim_group_xml(anim_el, group_anim.name, group_anim.idx, group_anim, trim=trim,
                                           copy_name=group_anim.copy_name, size=size):
                self.create_multi_sheet_frame_data(anim_el, group_anim)

        ElementTree.indent(root)

        if not file_name:
            # Use loaded file name
            file_name = self.file_name

            # Reset saves.
            for item in self._get_action_list_items():
                item.anim_group.modified = False
                item.update_text()

        tree = ElementTree.ElementTree(root)
        write_xml(tree, file_name)

    def export_single_sheet(self):
        if self.loaded_tree:
            options = QFileDialog.Option.ShowDirsOnly

            directory = QFileDialog.getExistingDirectory(self.window, "Select Directory for Single Sheet", "", options)

            if directory:
                self._export_single_sheet(directory)

    def _export_single_sheet(self, directory):
        return self._export_bundle(directory, SheetFormat.SINGLE)

    def _export_bundle(self, directory, format_name, xml_name=None):
        """Validate and render the complete bundle before touching its destination."""
        directory = pathlib.Path(directory)
        xml_name = xml_name or format_name.value
        try:
            if pathlib.Path(xml_name).suffix.lower() != '.xml':
                raise FormatError('Animation data must be saved as an XML file.')
            validate_export(self, format_name)
            # Staging also catches encoding/layout failures before an overwrite.
            directory.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='mdframeedit-export-', dir=directory.parent) as temporary:
                stage = pathlib.Path(temporary)
                if format_name == SheetFormat.SINGLE:
                    self._write_single_sheet(stage)
                else:
                    self._write_multiple_sheets(stage)
                validate_source(stage / format_name.value, format_name)
                directory.mkdir(parents=True, exist_ok=True)
                files = sorted(stage.iterdir(), key=lambda path: path.suffix.lower() == '.xml')
                replacements = [(path, directory / (xml_name if path.name == format_name.value else path.name))
                                for path in files]
                if format_name == SheetFormat.SINGLE and not (stage / 'Offsets.png').exists():
                    replacements.append((None, directory / 'Offsets.png'))
                backup = stage / 'backup'
                backup.mkdir()
                for _, target in replacements:
                    if target.exists():
                        shutil.copy2(target, backup / target.name)
                changed = []
                try:
                    for source, target in replacements:
                        if source is None:
                            if target.exists():
                                target.unlink()
                                changed.append(target)
                        else:
                            os.replace(source, target)
                            changed.append(target)
                except OSError as write_error:
                    try:
                        for target in reversed(changed):
                            saved = backup / target.name
                            if saved.exists():
                                os.replace(saved, target)
                            else:
                                target.unlink(missing_ok=True)
                    except OSError:
                        rescue = pathlib.Path(tempfile.mkdtemp(prefix='mdframeedit-recovery-', dir=directory.parent))
                        shutil.copytree(backup, rescue, dirs_exist_ok=True)
                        raise FormatError(f'Save failed. Original file backups are in {rescue}.') from write_error
                    raise
        except (FormatError, OSError, ValueError) as error:
            self.create_error_popup(str(error))
            return False
        message = 'Single sheet' if format_name == SheetFormat.SINGLE else 'Multisheet frames'
        self.ui.statusBar.showMessage(f'{message} were output successfully.', 3000)
        return True

    def _write_single_sheet(self, directory):
        self._save_frame_data(str(directory / 'FrameData.xml'))
        with open(directory / 'Anim.png', 'wb') as output:
            self.sheet_image.save(str(directory / 'Anim.png'), file=output)
        changed_points = {index: body for index, body in self.action_points.items()
                          if body != self.loaded_action_points.get(index)}
        if changed_points or (self.action_pt_image is None and self.action_points):
            if self.action_pt_image is None:
                image = Image.new('RGBA', (self.sheet_image.width, self.sheet_image.height))
            else:
                image = Image.frombytes('RGBA', (self.action_pt_image.width, self.action_pt_image.height),
                    self.action_pt_image.get_image_data().get_data('RGBA', -self.action_pt_image.width * 4))
            draw = ImageDraw.Draw(image)
            for index, body in (changed_points or self.action_points).items():
                x = (index % self.image_grid.columns) * self.frame_width + self.frame_width // 2
                y = (index // self.image_grid.columns) * self.frame_height + self.frame_height // 2
                image.paste((0, 0, 0, 0), (x - self.frame_width // 2, y - self.frame_height // 2,
                                           x - self.frame_width // 2 + self.frame_width,
                                           y - self.frame_height // 2 + self.frame_height))
                positions = defaultdict(list)
                for point, color in zip(body.all_pos(), ((255, 0, 0, 255), (0, 255, 0, 255),
                                                       (0, 0, 255, 255), (0, 0, 0, 255))):
                    positions[(x + point.x, y + point.y)].append(color)
                for position, color in overlap_colors(positions).items():
                    draw.point(position, color)
            image.save(directory / 'Offsets.png')
        elif self.action_pt_image is not None:
            with open(directory / 'Offsets.png', 'wb') as output:
                self.action_pt_image.save(str(directory / 'Offsets.png'), file=output)

    def export_multiple_sheets(self):
        if self.loaded_tree:
            options = QFileDialog.Option.ShowDirsOnly

            directory = QFileDialog.getExistingDirectory(self.window, "Select Directory for Multi-Sheets", "", options)

            if directory:
                self._export_multiple_sheets(directory)

    def _export_multiple_sheets(self, directory):
        return self._export_bundle(directory, SheetFormat.MULTI)

    def _write_multiple_sheets(self, directory):
        shadow = Image.frombytes('RGBA', (self.shadow_image.width, self.shadow_image.height),
                                 self.shadow_image.get_image_data().get_data('RGBA', -self.shadow_image.width * 4))
        anchor = get_shadow_location_from_pil_image(shadow) or Offset(shadow.width // 2, shadow.height // 2)
        tiles = {}
        group_sizes = {}
        for group in self.groups:
            if self.ui.actionTrim_Copies.isChecked() and self._copy_target(group, group.copy_name):
                continue
            directions = [group.directions[0], *reversed(group.directions[1:])]
            if self.ui.actionCollapse_Singles.isChecked() and self.is_sequence_collapsable(group):
                directions = directions[:1]
            prepared = []
            half_width = half_height = 1
            for sequence in directions:
                row = []
                for frame in sequence.frames:
                    if frame.frame_index not in tiles:
                        region = self.image_grid[frame.frame_index]
                        tiles[frame.frame_index] = Image.frombytes('RGBA', (region.width, region.height),
                            region.get_image_data().get_data('RGBA', -region.width * 4))
                    tile = tiles[frame.frame_index]
                    image = tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT) if frame.flip else tile
                    bounds = image.getbbox() or (tile.width // 2, tile.height // 2,
                                                tile.width // 2 + 1, tile.height // 2 + 1)
                    sprite = image.crop(bounds)
                    sx = bounds[0] - tile.width // 2 + frame.sprite_offset.x
                    sy = bounds[1] - tile.height // 2 + frame.sprite_offset.y
                    body = self.action_points[frame.frame_index]
                    pivot = tile.width - 1 - 2 * (tile.width // 2)
                    points = [((-point.x + pivot if frame.flip else point.x) + frame.sprite_offset.x,
                               point.y + frame.sprite_offset.y) for point in body.all_pos()]
                    mx, my = frame.shadow_offset.x - anchor.x, frame.shadow_offset.y - anchor.y
                    half_width = max(half_width, abs(sx), abs(sx + sprite.width), abs(mx), abs(mx + shadow.width),
                                     *(max(abs(x), abs(x + 1)) for x, y in points))
                    half_height = max(half_height, abs(sy), abs(sy + sprite.height), abs(my), abs(my + shadow.height),
                                      *(max(abs(y), abs(y + 1)) for x, y in points))
                    row.append((sprite, sx, sy, points, shadow, mx, my))
                prepared.append(row)
            width, height = round_up_to_mult(half_width * 2, 8), round_up_to_mult(half_height * 2, 8)
            group_sizes[group.name] = (width, height)
            size = (width * len(prepared[0]), height * len(prepared))
            sprites = Image.new('RGBA', size)
            offsets = Image.new('RGBA', size)
            shadows = Image.new('RGBA', size)
            draw = ImageDraw.Draw(offsets)
            for row_index, row in enumerate(prepared):
                for column, (sprite, sx, sy, points, shadow, mx, my) in enumerate(row):
                    cx, cy = column * width + width // 2, row_index * height + height // 2
                    sprites.paste(sprite, (cx + sx, cy + sy))
                    shadows.paste(shadow, (cx + mx, cy + my))
                    positions = defaultdict(list)
                    for point, color in zip(points, ((255, 0, 0, 255), (0, 255, 0, 255),
                                                     (0, 0, 255, 255), (0, 0, 0, 255))):
                        positions[(cx + point[0], cy + point[1])].append(color)
                    for position, color in overlap_colors(positions).items():
                        draw.point(position, color)
            for suffix, image in (('Anim', sprites), ('Offsets', offsets), ('Shadow', shadows)):
                image.save(directory / f'{group.name}-{suffix}.png')
        self._save_export_frame_data(str(directory / 'AnimData.xml'), group_sizes)

    def get_attachment_points_from_texture(self, path):
        if os.path.join(path, 'Offsets.png'):
            pass

    def exit_application(self):
        self.window.close()


class SpriteEditorWindow(QtWidgets.QMainWindow):
    """Independent sprite documents with a shared window and active-tab menus."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.settings = QSettings('MDFrameEditor', 'Frame Editor')
        self.panelStates = {}
        for name in ('mainSplitter', 'previewSplitter'):
            state = self.settings.value(f'window/{name}')
            if isinstance(state, QtCore.QByteArray):
                self.panelStates[name] = state
        self._editors = {}
        self._holders = {}
        self._saved_states = {}
        self._active_editor = None
        self.documentTabs = QtWidgets.QTabWidget(self)
        self.documentTabs.setObjectName('documentTabs')
        self.documentTabs.setDocumentMode(True)
        self.documentTabs.setTabsClosable(True)
        self.documentTabs.setMovable(True)
        self.documentTabs.setUsesScrollButtons(True)
        self.documentTabs.tabBar().setElideMode(QtCore.Qt.TextElideMode.ElideMiddle)
        self.setCentralWidget(self.documentTabs)
        self.closeTabAction = QtGui.QAction('Close Tab', self)
        self.closeTabAction.setShortcut(QtGui.QKeySequence.StandardKey.Close)
        self.closeTabAction.triggered.connect(
            lambda: self.close_document(self.documentTabs.currentIndex()))
        self.addAction(self.closeTabAction)
        self._createPreferenceMenus()
        self.documentTabs.currentChanged.connect(self._activate_document)
        self.documentTabs.tabCloseRequested.connect(self.close_document)
        self._add_empty_document()
        self.resize(1040, 800)
        geometry = self.settings.value('window/geometry')
        if self.preferenceActions['rememberWindowSize'].isChecked() and isinstance(geometry, QtCore.QByteArray):
            self.restoreGeometry(geometry)
        # Apply saved dividers after Qt allocates the restored window's layout.
        QtCore.QTimer.singleShot(0, self._restoreVisiblePanelLayout)

    def _createPreferenceMenus(self):
        self.settingsMenu = QtWidgets.QMenu('Settings', self)
        self.viewMenu = QtWidgets.QMenu('View', self)
        self.preferenceActions = {}
        for name, text, default, menu in (
                ('rememberWindowSize', 'Remember Window Size', True, self.settingsMenu),
                ('cameraLocked', 'Lock Camera', True, self.settingsMenu),
                ('showSpriteBounds', 'Show Sprite Dimensions', False, self.viewMenu),
                ('includeWhitespace', 'Include Sprite Whitespace', False, self.viewMenu)):
            action = QtGui.QAction(text, self)
            action.setCheckable(True)
            action.setChecked(self.settings.value(f'preferences/{name}', default, bool))
            action.toggled.connect(partial(self.setPreference, name))
            menu.addAction(action)
            self.preferenceActions[name] = action

    def setPreference(self, name, checked):
        self.settings.setValue(f'preferences/{name}', checked)
        if name == 'rememberWindowSize' and not checked:
            self.settings.remove('window/geometry')
        for editor in self._editors.values():
            self._applyPreviewPreferences(editor)
        self.settings.sync()

    def _applyPreviewPreferences(self, editor):
        showBounds = self.preferenceActions['showSpriteBounds'].isChecked()
        preview = editor.ui.sheetFramePicture
        preview.showBounds = showBounds
        preview.includeWhitespace = self.preferenceActions['includeWhitespace'].isChecked()
        preview.update()
        editor.openGLWidget.showSpriteBounds = showBounds
        editor.openGLWidget.setCameraLocked(self.preferenceActions['cameraLocked'].isChecked())

    def _restoreVisiblePanelLayout(self):
        editor = self.current_editor
        if editor is not None:
            editor.ui.updateEditorLayout()
            self._restorePanelLayout(editor)

    def _capturePanelLayout(self, editor):
        for name in ('mainSplitter', 'previewSplitter'):
            self.panelStates[name] = getattr(editor.ui, name).saveState()

    def _restorePanelLayout(self, editor):
        for name, state in self.panelStates.items():
            getattr(editor.ui, name).restoreState(state)

    def _saveWindowLayout(self):
        if self.current_editor is not None:
            self._capturePanelLayout(self.current_editor)
        if self.preferenceActions['rememberWindowSize'].isChecked():
            self.settings.setValue('window/geometry', self.saveGeometry())
        else:
            self.settings.remove('window/geometry')
        for name, state in self.panelStates.items():
            self.settings.setValue(f'window/{name}', state)
        self.settings.sync()

    @property
    def current_editor(self):
        return self._editors.get(self.documentTabs.currentWidget())

    @staticmethod
    def _path_key(path):
        return os.path.normcase(os.path.realpath(path))

    @staticmethod
    def _document_state(editor):
        groups = tuple(
            (group.idx, group.name, group.copy_name, group.rush_frame, group.hit_frame, group.return_frame,
             tuple(tuple((frame.frame_index, frame.flip, frame.duration,
                          frame.sprite_offset.data(), frame.shadow_offset.data())
                         for frame in sequence.frames) for sequence in group.directions))
            for group in editor.groups)
        points = tuple((index, tuple(point.data() for point in body.all_pos()))
                       for index, body in sorted(editor.action_points.items()))
        return (editor.shadow_size, editor.shadow_size_present, groups, points)

    def _create_document(self):
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
            lambda message, document=editor: self._document_status(document, message))
        self._editors[page] = editor
        self._holders[page] = holder
        return page, editor

    def _add_empty_document(self):
        page, editor = self._create_document()
        self.documentTabs.addTab(page, 'Untitled')
        self.documentTabs.setCurrentWidget(page)
        return editor

    def open_files_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'Open Sprites', '', 'Animation Files (*.xml)')
        return self.open_files(paths)

    def open_files(self, paths):
        opened = []
        for path in paths:
            editor = self.open_file(path)
            if editor is not None:
                opened.append(editor)
        return opened

    def open_file(self, path):
        path = str(pathlib.Path(path).resolve())
        for page, editor in self._editors.items():
            if editor.file_name and self._path_key(editor.file_name) == self._path_key(path):
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
            self.documentTabs.addTab(page, '')
        self._update_tab(page)
        self.documentTabs.setCurrentWidget(page)
        self._document_status(editor, editor.ui.statusBar.currentMessage())
        return editor

    def _update_tab(self, page):
        editor = self._editors[page]
        index = self.documentTabs.indexOf(page)
        if index >= 0:
            folder = pathlib.Path(editor.file_name).parent
            base = folder.name or str(folder)
            previous_path = self.documentTabs.tabToolTip(index)
            previous_folder = pathlib.Path(previous_path).parent if previous_path else None
            previous_base = (previous_folder.name or str(previous_folder)) if previous_folder else None
            # Keep an existing suffix stable when saving, moving, or closing other tabs.
            if base != previous_base:
                used = {self.documentTabs.tabText(i).casefold()
                        for i in range(self.documentTabs.count()) if i != index}
                name = base
                suffix = 1
                while name.casefold() in used:
                    name = f'{base} ({suffix})'
                    suffix += 1
                self.documentTabs.setTabText(index, name)
            self.documentTabs.setTabToolTip(index, editor.file_name)
        if editor is self.current_editor:
            self._update_window_title()

    def _update_window_title(self):
        editor = self.current_editor
        name = self.documentTabs.tabText(self.documentTabs.currentIndex()) if editor and editor.file_name else ''
        title = f'MD Frame Editor ({VERSION})'
        self.setWindowTitle(f'{name} - {title}' if name else title)

    def _activate_document(self, index):
        if self._active_editor is not None:
            self._capturePanelLayout(self._active_editor)
            self._active_editor.openGLWidget.timer.stop()
            pyglet.clock.unschedule(self._active_editor._playing_animation)
        self.menuBar().clear()
        editor = self.current_editor
        self._active_editor = editor
        if editor is not None:
            self._restorePanelLayout(editor)
            for menu in (editor.ui.menuFile, editor.ui.menuBatch):
                self.menuBar().addAction(menu.menuAction())
            self.menuBar().addMenu(self.viewMenu)
            self.menuBar().addMenu(self.settingsMenu)
            editor.openGLWidget.timer.start()
            if editor.animating and editor.current_anim_frame is not None:
                pyglet.clock.schedule_once(editor._playing_animation,
                                           editor.anim_speed * editor.current_anim_frame.duration)
            self.statusBar().showMessage(editor.ui.statusBar.currentMessage())
        else:
            self.statusBar().clearMessage()
        self._update_window_title()

    def _document_status(self, editor, message):
        if editor is self.current_editor:
            self.statusBar().showMessage(message)

    def refresh_recent_files(self):
        for editor in self._editors.values():
            editor.recent_files = editor.settings.value('recent', [], list)
            editor._update_recent_actions()

    def can_save_path(self, editor, path):
        for other in self._editors.values():
            if (other is not editor and other.file_name and
                    self._path_key(other.file_name) == self._path_key(path)):
                editor.create_error_popup('This file is already open in another tab.')
                return False
        return True

    def document_saved(self, editor):
        for page, document in self._editors.items():
            if document is editor:
                self._saved_states[page] = self._document_state(editor)
                self._update_tab(page)
                break

    def _confirm_close(self, page):
        editor = self._editors[page]
        if editor.loaded_tree is None or self._saved_states.get(page) == self._document_state(editor):
            return True
        self.documentTabs.setCurrentWidget(page)
        choice = QtWidgets.QMessageBox.question(
            self, 'Save changes?', f'Save changes to {editor.file_name}?',
            QtWidgets.QMessageBox.StandardButton.Save | QtWidgets.QMessageBox.StandardButton.Discard |
            QtWidgets.QMessageBox.StandardButton.Cancel, QtWidgets.QMessageBox.StandardButton.Save)
        if choice == QtWidgets.QMessageBox.StandardButton.Cancel:
            return False
        if choice == QtWidgets.QMessageBox.StandardButton.Save:
            return editor._save_document(editor.file_name)
        return choice == QtWidgets.QMessageBox.StandardButton.Discard

    def close_document(self, index):
        page = self.documentTabs.widget(index)
        if page is None or not self._confirm_close(page):
            return False
        self.documentTabs.removeTab(index)
        self._dispose_document(page)
        if not self.documentTabs.count():
            self._add_empty_document()
        return True

    def _dispose_document(self, page):
        editor = self._editors.pop(page)
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

    def closeEvent(self, event):
        if not all(self._confirm_close(page) for page in self._editors):
            event.ignore()
            return
        self._saveWindowLayout()
        self.menuBar().clear()
        self._active_editor = None
        for page in list(self._editors):
            self._dispose_document(page)
        super().closeEvent(event)


def excepthook(exc_type, exc_value, exc_tb):
    tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(tb)


if __name__ == "__main__":
    sys.excepthook = excepthook
    app = QtWidgets.QApplication(sys.argv)
    mainWindow = SpriteEditorWindow(app)
    mainWindow.show()
    sys.exit(app.exec())
