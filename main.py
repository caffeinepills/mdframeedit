from __future__ import annotations

import sys

if __name__ == "__main__" and len(sys.argv) > 1:
    from cli import main as cli_main

    sys.exit(cli_main())

import copy
import time
import os
import pathlib
import signal
import traceback
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, replace
from functools import partial, wraps
from hashlib import sha256
from typing import TYPE_CHECKING, TypeVar

import pyglet
from PIL import Image

pyglet.options.com_mta = False
import warnings

warnings.simplefilter("ignore", UserWarning)
sys.coinit_flags = 2

from pyglet.gl import GL_NEAREST
from pyglet.math import clamp
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QFileInfo, QSettings
from PySide6.QtGui import QImage, QSurfaceFormat
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
from formats import SpriteFormat, validate_export
from formats.native import read_texture_layout
from preview import is_preview_path
from formats.packing import TextureLayout, unpack_texture
from gui.batch import ActionAliasDialog, BatchEditDialog
from gui.editor import Ui_MainWindow
from gui.info import SpriteInfoDialog
from gui.preview import TopLeftGrid, PygletWidget, create_point_marker
from gui.layer_workspace import LayerWorkspace
from gui.image_edit import IMAGE_FILTER, IMAGE_SUFFIXES, ImageAtlasDialog, ResizeFramesDialog
from utils import (
    FormatError,
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


@dataclass(frozen=True)
class CopiedActionPoint:
    name: str
    position: tuple[int, int] | None


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


class ResizeFramesCommand(SpriteEditCommand):
    def __init__(
        self, editor: AnimationEditor, before: EditState, after: EditState,
        beforeDocument: SpriteDocument, afterDocument: SpriteDocument,
    ) -> None:
        super().__init__(editor, "Edit Frame Cells", before, after)
        self.beforeDocument = beforeDocument
        self.afterDocument = afterDocument

    def undo(self) -> None:
        self.editor.replaceSheetArtwork(self.beforeDocument)
        super().undo()

    def redo(self) -> None:
        if not self.firstRedo:
            self.editor.replaceSheetArtwork(self.afterDocument)
        super().redo()


EditResult = TypeVar("EditResult")


def undoable(text: str) -> Callable[
    [Callable[..., EditResult]], Callable[..., EditResult]
]:
    """Record one complete editor operation, including nested edit handlers."""
    def decorate(method: Callable[..., EditResult]) -> Callable[..., EditResult]:
        @wraps(method)
        def record(editor: AnimationEditor, *args: object, **kwargs: object) -> EditResult:
            if editor.frozen_preview:
                return None
            if editor.sheet_format is None or editor.editDepth or editor.restoringEdit:
                return method(editor, *args, **kwargs)
            if getattr(editor, "point_preview", None) is not None:
                editor.point_preview.pause_for_edit(editor)
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






REDUCE_RUSH_FRAMES = False


class AnimationEditor(AnimationSerialization):
    ui: Ui_MainWindow
    shadow_image: pyglet.image.AbstractImage | None
    sprite: pyglet.sprite.Sprite | None
    ANIMATION_SPEED_SLIDER_VALUES = (0.1, 0.25, 0.5, 1, 2)
    MAX_RECENT = 10

    def __init__(
        self,
        app: QtWidgets.QApplication,
        window: QtWidgets.QMainWindow,
        ui: Ui_MainWindow,
        document_host: SpriteEditorWindow | None = None,
        preview_factory: Callable | None = None,
    ) -> None:
        self._configure_open_gl()
        self.app = app
        self.window = window
        self.ui = ui
        self.document_host = document_host
        self.workspace = None
        self.point_preview = None
        self.groups: list[AnimGroup] = []
        self.file_name = ""
        self.source_image_path: pathlib.Path | None = None
        self.texture_name = "Anim.png"
        self.frozen_preview = False
        self.frozen_export_paths = {}
        self.sheet_format: SheetFormat | str | None = None
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
        self.actionPointsClipboard: ActionPoints | CopiedActionPoint | None = None
        self.copyActionPointAction = self.editMenu.addAction(
            "Copy Selected Action Point", self.copyActionPoint
        )
        self.copyActionPointAction.setEnabled(False)
        self.copyActionPointAction.setToolTip("Copy the selected named point from the current sheet frame, including its unset state.")
        ui.loadedSheetFrameList.addAction(self.copyActionPointAction)
        self.copyActionPointsAction = self.editMenu.addAction(
            "Copy All Action Points", self.copyActionPoints
        )
        self.copyActionPointsAction.setToolTip("Copy every named point from the current sheet frame. Pasting replaces the target frames' point sets.")
        self.pasteActionPointsAction = self.editMenu.addAction(
            "Paste Action Points", self.pasteActionPoints
        )
        ui.loadedSheetFrameList.setToolTip("Ctrl+click to select individual frames; Shift+click to select a range. Right-click to copy one point or all points. Ctrl+C copies all points; Ctrl+V pastes onto the selected frames.")
        for action, shortcut in (
            (self.copyActionPointsAction, QtGui.QKeySequence.StandardKey.Copy),
            (self.pasteActionPointsAction, QtGui.QKeySequence.StandardKey.Paste),
        ):
            action.setEnabled(False)
            action.setShortcuts(shortcut)
            action.setShortcutContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
            ui.loadedSheetFrameList.addAction(action)
        ui.loadedSheetFrameList.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.ActionsContextMenu
        )
        ui.loadedSheetFrameList.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        ui.loadedSheetFrameList.itemSelectionChanged.connect(self.updateActionPointActions)
        self.editMenu.aboutToShow.connect(self.updateActionPointActions)
        self.editMenu.addSeparator()
        self.resizeFramesAction = self.editMenu.addAction(
            "Edit Frame Cells...", self.openResizeFrames
        )
        self.resizeFramesAction.setEnabled(False)
        self.layersAction = self.editMenu.addAction("Preview Sprites…", self.openLayers)
        self.editMenu.aboutToShow.connect(lambda: self.layersAction.setEnabled(self.sheet_image is not None))
        self.editMenu.aboutToShow.connect(
            lambda: self.resizeFramesAction.setEnabled(self.sheet_image is not None)
        )
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
        self._read_recent_files()
        self.artworkPath: pathlib.Path | None = None
        # Hash for file change.
        self.artworkDigest: bytes | None = None
        self.nativeTextureLayout: TextureLayout | None = None
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
        self.recent_preview_actions: list[QtGui.QAction] = []

        for actions, menu in ((self.recentFileActions, self.ui.menuRecent),
                              (self.recent_preview_actions, self.ui.menuRecentPreviews)):
            for i in range(self.MAX_RECENT):
                action = QtGui.QAction(self.window, visible=False)
                action.triggered.connect(
                    lambda checked=False, a=action: self.load_recent_file(a)
                )
                actions.append(action)
                menu.addAction(action)

        self._update_recent_actions()

        self.scale = 2.0  # default sprite scaling.

        self.anim_speed = 1 / 60
        self.sheet_image: pyglet.image.ImageData | None = None
        self.image_grid: TopLeftGrid | None = None

        self.action_pt_image: pyglet.image.ImageData | None = None
        self.action_grid: TopLeftGrid | None = None
        self.action_points: dict[int, ActionPoints] = {}
        self.action_point_names = ()
        self.rig_profile = None
        self.rig_profile_id = None
        self._rig_profile_loaded = None
        self._rig_profile_stamp = None
        self._rig_profile_warning = ""
        self.sheet_point_names = []
        self.selected_sheet_frame: int | None = None
        self.sheet_preview_images: dict[int, QImage] = {}
        self.loaded_action_points: dict[int, ActionPoints] = {}

        self.shadow_image = self._shadow_image_data(template_image())
        self._shadow_size = 0
        self._shadow_display_images: dict[int, pyglet.image.ImageData] = {}

        self.sprite = None
        self.shadow: pyglet.sprite.Sprite | None = None

        self.marker_size = 3
        self.action_point_marker = create_point_marker(
            self.marker_size, (255, 255, 255, 255)
        )
        self.action_point_marker.anchor_x = self.marker_size // 2
        self.action_point_marker.anchor_y = self.marker_size // 2
        self._action_point_markers = {}
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
        self.cell_edit_revision = 0
        self.shadow_size = 0

        self.copied_sequence: AnimationSequence | None = None

        # self.window.setFixedSize(670, 836)

        self.current_sequence: AnimationSequence | None = None
        self.current_anim_frame: AnimFrame | None = None
        self.displayed_anim_frame: AnimFrame | None = None
        self.onionSprites: list[pyglet.sprite.Sprite] = []
        self.onion_shadow_sprites: list[pyglet.sprite.Sprite] = []
        self.onion_shadows_enabled = self.settings.value("preferences/onionShadows", False, bool)
        self.onionPreviewEnabled = self.settings.value("preferences/onionPreview", False, bool)
        self.onionPreviousFrames = max(0, min(5, self.settings.value("preferences/onionPreviousFrames", 1, int)))
        self.onionNextFrames = max(0, min(5, self.settings.value("preferences/onionNextFrames", 1, int)))
        self.onionOpacity = max(0, min(255, self.settings.value("preferences/onionOpacity", 72, int)))
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
                partial(self.choose_direction, self.directionButtons.index(button))
            )
            button.setCheckable(True)

        self.ui.buttonDown.toggle()
        self.ui.rotateLeftButton.clicked.connect(lambda: self.rotate_direction(-1))
        self.ui.rotateRightButton.clicked.connect(lambda: self.rotate_direction(1))

        self.openGLWidget = self.ui.openGLWidget = (preview_factory or PygletWidget)(
            240, 220, self.ui.verticalFrame_3, self.window, self
        )
        # A maximized window can be shorter than its layout minimum on RDP.
        # Let the canvas yield height to the playback/footer rows instead of
        # painting over them. Keep one pixel for a valid GL projection.
        self.openGLWidget.setMinimumSize(QtCore.QSize(240, 1))
        self.openGLWidget.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
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
        self.ui.onionPreviewCheckBox.setChecked(self.onionPreviewEnabled)
        self.ui.onionPreviewCheckBox.toggled.connect(self.changeOnionPreview)
        self.ui.onionShadowsCheckBox.setChecked(self.onion_shadows_enabled)
        self.ui.onionShadowsCheckBox.toggled.connect(self.change_onion_shadows)
        self.setOnionRange(-self.onionPreviousFrames, self.onionNextFrames)
        self.ui.onionRangeSlider.rangeChanged.connect(self.changeOnionRange)
        self.setOnionOpacity(self.onionOpacity)
        self.ui.onionOpacitySlider.valueChanged.connect(self.changeOnionOpacity)

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
        self.pointDragState: tuple[int, EditState] | None = None
        self.ui.sheetFramePicture.pointDragStarted.connect(self.beginPointDrag)
        self.ui.sheetFramePicture.pointDragged.connect(self.dragActionPoint)
        self.ui.sheetFramePicture.pointDragFinished.connect(self.finishPointDrag)
        self.ui.spriteOpacitySlider.valueChanged.connect(
            partial(self.setPreviewOpacity, "sprite")
        )
        self.ui.shadowOpacitySlider.valueChanged.connect(
            partial(self.setPreviewOpacity, "shadow")
        )
        self.ui.actionPointsOpacitySlider.valueChanged.connect(
            partial(self.setPreviewOpacity, "actionPoints")
        )
        self.ui.actionPointsSizeSlider.valueChanged.connect(self.changeActionPointSize)
        for pointName in ("leftHand", "rightHand", "head", "center"):
            for spinBox in self.ui.actionPointSpinBoxes[pointName]:
                spinBox.valueChanged.connect(
                    partial(self.actionPointChanged, pointName)
                )
        self.ui.loadRigProfileButton.clicked.connect(self.chooseRigProfile)
        self.ui.unlinkRigProfileButton.clicked.connect(lambda: self.setRigProfile(None))
        self.ui.actionPointComboBox.currentIndexChanged.connect(self.refreshNamedPointControls)
        self.ui.addActionPointButton.clicked.connect(self.promptLocalPoint)
        self.ui.pointDefinedCheckBox.toggled.connect(self.setNamedPointDefined)
        for spin in self.ui.namedPointSpinBoxes:
            spin.valueChanged.connect(self.namedPointChanged)
        self.refreshNamedPointControls()

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
            self.action_point_names, self.rig_profile,
            tuple(sorted(item.idx for item in self.ui.loadedSheetFrameList.selectedItems())),
            self.rig_profile_id,
        )

    def chooseRigProfile(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self.window, "Load Project or Rig Profile", str(self.rig_profile or ""), "Project and Rig Profiles (*.yaml *.yml)")
        if path:
            from project import load_rig_profiles
            try:
                rigs = load_rig_profiles(path)
                if not rigs:
                    raise FormatError("This project profile has no rigs. Add entries under rigs to use it for Action Points.")
            except FormatError as error:
                self.create_error_popup(str(error))
                return
            ids = list(rigs)
            selected = ids[0]
            if len(ids) > 1:
                labels = [f"{rigs[key].name} ({key})" for key in ids]
                current = ids.index(self.rig_profile_id) if self.rig_profile_id in ids else 0
                label, accepted = QtWidgets.QInputDialog.getItem(
                    self.window, "Choose Rig", "Rig:", labels, current, False)
                if not accepted:
                    return
                selected = ids[labels.index(label)]
            self.setRigProfile(path, selected)

    @undoable("Change Rig Profile")
    def setRigProfile(self, path, rig_id=None):
        self.rig_profile = pathlib.Path(path).resolve() if path else None
        self.rig_profile_id = rig_id if path else None
        self.refreshRigProfile(force=True)

    def refreshRigProfile(self, *, force=False):
        if self.frozen_preview:
            return
        from project import load_rig_profile
        path = self.rig_profile
        try:
            stat = path.stat() if path else None
            stamp = (str(path), self.rig_profile_id, stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns) if stat else None
        except OSError:
            stamp = (str(path), self.rig_profile_id, "missing")
        if not force and stamp == self._rig_profile_stamp:
            return
        self._rig_profile_stamp = stamp
        self._rig_profile_loaded = None
        self._rig_profile_warning = ""
        if path:
            try:
                self._rig_profile_loaded = load_rig_profile(path, self.rig_profile_id)
            except FormatError as error:
                self._rig_profile_warning = str(error)
        self.refreshNamedPointControls()

    def refreshNamedPointControls(self, *_):
        from project import available_point_names
        ui = self.ui
        names = available_point_names(self.action_points, self.action_point_names, self._rig_profile_loaded)
        previous = ui.actionPointComboBox.currentData()
        with QtCore.QSignalBlocker(ui.actionPointComboBox):
            ui.actionPointComboBox.clear()
            body = self.action_points.get(self.selected_sheet_frame)
            for name in names:
                defined = body is not None and body.get(name) is not None
                scope = "Rig Profile" if self._rig_profile_loaded and name in self._rig_profile_loaded.action_points else "Local"
                ui.actionPointComboBox.addItem(f"{name} [{scope}]" + (" (unset)" if not defined else ""), name)
            if previous in names:
                ui.actionPointComboBox.setCurrentIndex(names.index(previous))
        name = ui.actionPointComboBox.currentData()
        point = body.get(name) if body and name else None
        defined_names = [key for key, _ in body.items()] if body else []
        self.ui.sheetFramePicture.selectedPoint = defined_names.index(name) if name in defined_names else None
        self.ui.sheetFramePicture.setToolTip(
            f"Action Point: {name or '(none)'}. Drag a dot to move it; Escape cancels.")
        self.ui.sheetFramePicture.update()
        selected = self.selected_sheet_frame is not None and name is not None
        with QtCore.QSignalBlocker(ui.pointDefinedCheckBox):
            ui.pointDefinedCheckBox.setChecked(point is not None)
            ui.pointDefinedCheckBox.setEnabled(selected)
        for spin, value in zip(ui.namedPointSpinBoxes, point.data() if point else (0, 0)):
            with QtCore.QSignalBlocker(spin):
                spin.setValue(value)
                spin.setEnabled(selected and point is not None)
        profile = self._rig_profile_loaded
        label = "Rig Profile: " + (profile.name if profile else "unavailable" if self.rig_profile else "none")
        missing = [name for name in profile.suggested_actions if not any(g.name == name for g in self.groups)] if profile else []
        if self._rig_profile_warning:
            label += " — cannot load (relink with Load Profile)"
        if missing:
            label += "\nSuggested actions missing: " + ", ".join(missing)
        ui.rigProfileLabel.setText(label)
        ui.rigProfileLabel.setToolTip(str(self.rig_profile or "") +
                                    (f"\nRig: {self.rig_profile_id}" if self.rig_profile_id else "") +
                                    "\n" + self._rig_profile_warning)
        ui.unlinkRigProfileButton.setEnabled(self.rig_profile is not None)
        ui.loadRigProfileButton.setEnabled(self.image_grid is not None)
        ui.addActionPointButton.setEnabled(self.image_grid is not None)
        self.updateActionPointActions()

    def promptLocalPoint(self):
        name, accepted = QtWidgets.QInputDialog.getText(self.window, "New Action Point", "Name:")
        if accepted:
            try:
                self.addLocalPoint(name)
            except FormatError as error:
                self.create_error_popup(str(error))

    @undoable("Add Action Point")
    def addLocalPoint(self, name):
        from project import validate_point_name
        validate_point_name(name)
        if name not in self.action_point_names:
            self.action_point_names = (*self.action_point_names, name)
        self.refreshNamedPointControls()
        self.ui.actionPointComboBox.setCurrentIndex(self.ui.actionPointComboBox.findData(name))

    @undoable("Set Action Point")
    def setNamedPointDefined(self, defined):
        index, name = self.selected_sheet_frame, self.ui.actionPointComboBox.currentData()
        if index is None or name is None:
            return
        body = self.action_points.setdefault(index, ActionPoints.from_named({}))
        body.set(name, Offset() if defined else None)
        self.refreshNamedPointControls()
        self.refreshActionPointEdits(index)

    @undoable("Move Action Point")
    def namedPointChanged(self, *_):
        index, name = self.selected_sheet_frame, self.ui.actionPointComboBox.currentData()
        if index is None or name is None:
            return
        body = self.action_points.get(index)
        if body is None or body.get(name) is None:
            return
        body.set(name, Offset(*(spin.value() for spin in self.ui.namedPointSpinBoxes)))
        self.selectSheetFrame(index)
        self.refreshActionPointEdits(index)






    def push_edit_command(self, text: str, before: EditState, after: EditState) -> None:
        self.undoStack.push(SpriteEditCommand(self, text, before, after))

    def openLayers(self):
        if self.sheet_image is not None:
            self.workspace.show_add_menu(self.ui.layerTabs)

    def clearPointPreview(self):
        if self.point_preview is not None:
            self.point_preview.release_view(self)

    def updateLayerPreview(self) -> None:
        if getattr(self, "point_preview", None) is not None:
            self.point_preview.render(self)
            return
        if self.sprite is not None and self.workspace:
            self.sprite.visible = self.workspace.body_visible

    def openResizeFrames(self) -> None:
        if self.sheet_image is None:
            return
        dialog = ResizeFramesDialog(self)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def replaceSheetArtwork(self, document: SpriteDocument) -> None:
        if self.openGLWidget.isValid():
            self.openGLWidget.makeCurrent()
        self.clearOnionPreview()

        def previewImage(image: Image.Image | None) -> pyglet.image.ImageData | None:
            if image is None:
                return None
            return pyglet.image.ImageData(
                image.width, image.height, "RGBA", image.tobytes(), pitch=-image.width * 4
            )

        self.sheet_image = previewImage(document.sheet_image)
        self.action_pt_image = previewImage(document.action_pt_image)
        self.frame_width, self.frame_height = document.frame_width, document.frame_height
        self.cell_edit_revision = document.cell_edit_revision
        rows, columns = document.image_grid.rows, document.image_grid.columns
        self.image_grid = TopLeftGrid(self.sheet_image, rows=rows, columns=columns)
        self.action_grid = (
            TopLeftGrid(self.action_pt_image, rows=rows, columns=columns)
            if self.action_pt_image is not None else None
        )
        with QtCore.QSignalBlocker(self.ui.loadedSheetFrameList):
            self.ui.loadedSheetFrameList.clear()
            self.sheet_preview_images.clear()
            self._add_frames_from_grid()

    def resizeFrameCells(self, width: int, height: int, *, centerArtwork: bool = False) -> bool:
        if self.frozen_preview:
            return False
        if (width, height) == (self.frame_width, self.frame_height) and not centerArtwork:
            return False
        before = self.captureEditState()
        original = self._sprite_document()
        resized = original.resized_frame_cells(width, height, center_artwork=centerArtwork)
        if resized is original:
            return False
        self.replaceSheetArtwork(resized)
        self.restoreEditState(replace(before, groups=resized.groups, action_points=resized.action_points))
        after = self.captureEditState()
        self.undoStack.push(ResizeFramesCommand(self, before, after, original, resized))
        self.ui.statusBar.showMessage(f"Frame cells updated ({width} x {height}). Save to update the files.", 5000)
        return True

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
            self.action_point_names, self.rig_profile = state.action_point_names, state.rig_profile
            self.rig_profile_id = state.rig_profile_id
            self.refreshRigProfile(force=True)
            self.clearPointPreview()
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
                        or current[4:] != self.savedEditSignature[4:]
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
            selected_cells = set(state.sheet_frame_selection)
            with QtCore.QSignalBlocker(self.ui.loadedSheetFrameList):
                self.ui.loadedSheetFrameList.clearSelection()
                for row in range(self.ui.loadedSheetFrameList.count()):
                    item = self.ui.loadedSheetFrameList.item(row)
                    item.setSelected(item.idx in selected_cells)
            self.updateActionPointActions()
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
        if index != self.selected_sheet_frame:
            self.ui.sheetFramePicture.finishDrag(True)
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
            if self.ui.loadedSheetFrameList.currentRow() != row:
                self.ui.loadedSheetFrameList.setCurrentRow(row, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self.ui.sheetFrameScopeLabel.setText(
            f"Sheet frame: {index if index is not None else '-'}"
        )
        body = self.action_points.get(index, ActionPoints())
        for pointName, spinBoxes in self.ui.actionPointSpinBoxes.items():
            fieldName = {"leftHand": "left_hand", "rightHand": "right_hand"}.get(
                pointName, pointName
            )
            offset = body.get(fieldName) or Offset()
            for spinBox, value in zip(spinBoxes, offset.data()):
                with QtCore.QSignalBlocker(spinBox):
                    spinBox.setValue(value)
                    spinBox.setEnabled(index is not None)
        self.refreshNamedPointControls()
        self.refreshSheetFramePreview()

        self.updateActionPointActions()

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
        self.sheet_point_names = [name for name, _ in body.items()] if body else []
        points = (
            []
            if body is None
            else [
                (point.x + image.width // 2, point.y + image.height // 2, color)
                for name, point in body.items()
                for color in [self.actionPointColor(name)]
            ]
        )
        self.ui.sheetFramePicture.setFrame(
            qim,
            points,
            self.ui.showActionPointsCheckBox.isChecked(),
            self.ui.actionPointsOpacitySlider.value(),
        )

    def setArtworkReloadEnabled(self, enabled: bool) -> None:
        if self.frozen_preview:
            enabled = False
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
        self.nativeTextureLayout = None
        if self.file_name and self.sheet_format in (SheetFormat.SINGLE, SheetFormat.NATIVE) and self.sheet_image is not None:
            self.artworkPath = pathlib.Path(self.file_name).resolve().with_name(self.texture_name)
            if self.sheet_format == SheetFormat.NATIVE:
                self.nativeTextureLayout = read_texture_layout(self.file_name)
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
            if self.nativeTextureLayout is not None:
                if self.nativeTextureLayout.frame_size != (self.frame_width, self.frame_height):
                    raise FormatError("Frame dimensions changed. Reopen the sheet to load its new layout.")
                image = unpack_texture(image, self.nativeTextureLayout)
            pixels = image.tobytes()
        except FormatError as error:
            self.ui.statusBar.showMessage(str(error), 5000)
            self.updateArtworkWatch()
            return
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
        self.updateOnionPreview(frame)
        self.openGLWidget.update()
        self.ui.statusBar.showMessage("Artwork reloaded.", 3000)

    def beginPointDrag(self) -> None:
        if self.frozen_preview:
            return
        if self.point_preview is not None:
            self.point_preview.pause_for_edit(self)
        index = self.selected_sheet_frame
        if index is not None and index in self.action_points:
            self.pointDragState = (index, self.captureEditState())

    def dragActionPoint(self, point: int, x: int, y: int) -> None:
        if self.pointDragState is None:
            return
        index, _ = self.pointDragState
        name = self.sheet_point_names[point]
        self.ui.actionPointComboBox.setCurrentIndex(self.ui.actionPointComboBox.findData(name))
        offset = Offset(max(-32768, min(32767, x - self.frame_width // 2)),
                        max(-32768, min(32767, y - self.frame_height // 2)))
        body = self.action_points[index]
        if body.get(name) == offset:
            return
        body.set(name, offset)
        self.selectSheetFrame(index)
        self.refreshActionPointEdits(index)

    def finishPointDrag(self, cancelled: bool) -> None:
        state, self.pointDragState = self.pointDragState, None
        if state is None:
            return
        _, before = state
        after = self.captureEditState()
        if before.signature == after.signature:
            return
        if cancelled:
            self.restoreEditState(before)
        else:
            self.undoStack.push(SpriteEditCommand(self, "Move Action Point", before, after))

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
        self.refreshActionPointEdits(index)

    def updateActionPointActions(self) -> None:
        owner = self.document_host or self
        index = self.selected_sheet_frame
        selected = (
            index is not None and self.image_grid is not None
            and 0 <= index < len(self.image_grid)
        )
        self.copyActionPointsAction.setEnabled(selected and index in self.action_points)
        self.copyActionPointAction.setEnabled(selected and self.ui.actionPointComboBox.currentData() is not None)
        self.pasteActionPointsAction.setEnabled(
            selected and owner.actionPointsClipboard is not None
        )

    def copyActionPoints(self) -> None:
        body = self.action_points.get(self.selected_sheet_frame)
        if body is None:
            return
        owner = self.document_host or self
        owner.actionPointsClipboard = copy.deepcopy(body)
        self.updateActionPointClipboardActions()
        self.ui.statusBar.showMessage(
            f"Copied all action points from sheet frame {self.selected_sheet_frame}.", 4000
        )

    def copyActionPoint(self) -> None:
        index = self.selected_sheet_frame
        name = self.ui.actionPointComboBox.currentData()
        if index is None or name is None or self.image_grid is None or not 0 <= index < len(self.image_grid):
            return
        body = self.action_points.get(index)
        point = body.get(name) if body is not None else None
        (self.document_host or self).actionPointsClipboard = CopiedActionPoint(name, point.data() if point is not None else None)
        self.updateActionPointClipboardActions()
        self.ui.statusBar.showMessage(f"Copied action point '{name}' from sheet frame {index}.", 4000)

    def updateActionPointClipboardActions(self) -> None:
        editors = self.document_host._editors.values() if self.document_host else (self,)
        for editor in editors:
            editor.updateActionPointActions()

    @undoable("Paste Action Points")
    def pasteActionPoints(self) -> bool:
        source = (self.document_host or self).actionPointsClipboard
        index = self.selected_sheet_frame
        if (
            source is None or index is None or self.image_grid is None
            or not 0 <= index < len(self.image_grid)
        ):
            return False
        indices = sorted({item.idx for item in self.ui.loadedSheetFrameList.selectedItems()}) or [index]
        changes = {}
        for target in indices:
            previous = self.action_points.get(target)
            if isinstance(source, CopiedActionPoint):
                point = previous.get(source.name) if previous is not None else None
                if (point.data() if point is not None else None) == source.position:
                    continue
                body = copy.deepcopy(previous) if previous is not None else ActionPoints.from_named({})
                body.set(source.name, Offset(*source.position) if source.position is not None else None)
            else:
                body = copy.deepcopy(source)
            if body != previous:
                changes[target] = body
        if not changes:
            return False
        if len(indices) > 1:
            scope = f"action point '{source.name}'" if isinstance(source, CopiedActionPoint) else "all action points"
            choice = QtWidgets.QMessageBox.question(
                self.window, "Paste Action Points?",
                f"Paste {scope} onto {len(indices)} selected sheet frames?\n\n"
                "This changes every animation that uses these sheet cells.",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if choice != QtWidgets.QMessageBox.StandardButton.Yes:
                return False
        self.action_points.update(changes)
        self.selectSheetFrame(index)
        if isinstance(source, CopiedActionPoint):
            self.ui.actionPointComboBox.setCurrentIndex(self.ui.actionPointComboBox.findData(source.name))
        self.refreshActionPointEdits(set(changes))
        return True

    def refreshActionPointEdits(self, index: int | Iterable[int]) -> None:
        indices = {index} if isinstance(index, int) else set(index)
        for groupItem in self._get_action_list_items():
            if any(
                other.frame_index in indices
                for sequence in groupItem.anim_group.directions
                for other in sequence.frames
            ):
                groupItem.anim_group.modified = True
                groupItem.update_text()
        if not self.animating:
            self.set_animation()
        elif self.sprite is not None:
            self.openGLWidget.makeCurrent()
            self.updateActionPointPreview(self.displayed_anim_frame or self.current_anim_frame)
            self.openGLWidget.update()
        self.updateActionPointActions()
        self.refreshSheetFramePreview()
        self.ui.statusBar.showMessage(
            "Action points updated for all frames using this sheet image.", 4000
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
            self.ui.shadowSizeComboBox.setEnabled(self.sheet_format is not None)
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
            self.updateOnionPreview(self.displayed_anim_frame or self.current_anim_frame)
            self.openGLWidget.update()

    @undoable("Change Shadow Size")
    def apply_shadow_size(self, size: int) -> None:
        if self.sheet_format is None:
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
            self.newWindow = ActionAliasDialog(source, self.window)
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

    def save_action_trigger(self) -> bool:
        if self.frozen_preview:
            return False
        if self.sheet_format is None:
            return False
        if self.sheet_format != SheetFormat.NATIVE or not self.file_name:
            return self.save_as_action_trigger()
        return self._save_document(self.file_name)

    def save_as_action_trigger(self) -> bool:
        if self.frozen_preview:
            return False
        if self.sheet_format is not None:
            if self.file_name:
                suggested = pathlib.Path(self.file_name)
            elif self.source_image_path is not None:
                suggested = self.source_image_path.with_name(f"{self.source_image_path.stem}-sprite.json")
            else:
                suggested = pathlib.Path("Sprite.json")
            if self.sheet_format != SheetFormat.NATIVE:
                suggested = suggested.with_name(SheetFormat.NATIVE.value)
            file_name, _ = QFileDialog.getSaveFileName(
                self.window, "Save Sprite", str(suggested), "Sprite document (*.json)"
            )
            if file_name:
                target = pathlib.Path(file_name)
                if not target.suffix:
                    target = target.with_suffix(".json")
                return self._save_document(target)
        return False

    def _save_document(self, file_name: str | PathLike[str]) -> bool:
        if self.frozen_preview:
            return False
        if not file_name:
            return False
        target = pathlib.Path(file_name)
        if self.document_host and not self.document_host.can_save_path(self, target):
            return False
        if not self._export_bundle(target.parent, SheetFormat.NATIVE, target.name):
            return False
        self.file_name = str(target)
        self.sheet_format = SheetFormat.NATIVE
        self.loaded_tree = None
        self.texture_name = target.with_suffix(".png").name
        self.single_loaded = True
        self.ui.groupBox_5.setTitle("Sprite Sheet (JSON)")
        self.trackArtwork()
        self.shadow_size_present = self.shadow_size_present or self.shadow_size != self.loaded_shadow_size
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

    def _read_recent_files(self) -> None:
        """Partition the old mixed history and retain independent capped histories."""
        sprites, previews = [], []
        seen = set()
        for key in ("recent_sprites", "recent_previews", "recent"):
            for path in self.settings.value(key, [], list):
                if not isinstance(path, str) or not path:
                    continue
                identity = os.path.normcase(os.path.realpath(path))
                if identity in seen:
                    continue
                seen.add(identity)
                (previews if is_preview_path(path) else sprites).append(path)
        self.recent_files = sprites[:self.MAX_RECENT]
        self.recent_preview_files = previews[:self.MAX_RECENT]
        self.settings.setValue("recent_sprites", self.recent_files)
        self.settings.setValue("recent_previews", self.recent_preview_files)
        self.settings.remove("recent")

    def add_recent_list(self, path: str | PathLike[str]) -> None:
        path = str(pathlib.Path(path).resolve())
        self._read_recent_files()
        key = "recent_previews" if is_preview_path(path) else "recent_sprites"
        entries = self.recent_preview_files if is_preview_path(path) else self.recent_files
        identity = os.path.normcase(os.path.realpath(path))
        entries = [path, *(entry for entry in entries if os.path.normcase(os.path.realpath(entry)) != identity)]
        self.settings.setValue(key, entries[:self.MAX_RECENT])
        self._read_recent_files()
        self._update_recent_actions()
        if self.document_host:
            self.document_host.refresh_recent_files()

    def _update_recent_actions(self) -> None:
        self._populate_recent_actions(self.recent_files, self.recentFileActions)
        self._populate_recent_actions(self.recent_preview_files, self.recent_preview_actions)

    def _populate_recent_actions(self, entries, actions) -> None:
        recent_count = min(len(entries), self.MAX_RECENT)
        paths = [pathlib.Path(path) for path in entries[:recent_count]]

        for i in range(recent_count):
            action = actions[i]
            path = entries[i]
            parts = paths[i].parts
            # Include only enough parent folders to distinguish matching names.
            depth = 1
            while depth < len(parts) and any(
                j != i and other.parts[-depth:] == parts[-depth:]
                for j, other in enumerate(paths)
            ):
                depth += 1
            label = str(pathlib.Path(*parts[-depth:]))
            action.setText(label.replace("&", "&&"))
            action.setToolTip(path)
            action.setStatusTip(path)
            action.setVisible(True)
            action.setData(path)

        for m in range(recent_count, self.MAX_RECENT):
            actions[m].setVisible(False)

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
                # Copies retain their own sequences when their source is deleted.
                # Detach the relationship so native saves cannot retain a dangling
                # reference. The surrounding undo command restores both changes.
                for item in self._get_action_list_items():
                    if item.anim_group.copy_name == current.anim_group.name:
                        item.anim_group.copy_name = ""
                        item.anim_group.modified = True
                        item.update_text()
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

        if ok and name.strip() and self.image_grid is not None:
            name = name.strip()
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
                index_id = max((group.idx for group in self.groups), default=-1) + 1

            group = AnimGroup(index_id, name)
            self.groups.append(group)
            item = AnimGroupItem(group, self)
            self.ui.actionListWidget.addItem(item)
            self.ui.actionListWidget.setCurrentItem(item)
            item.mouseClickEvent(None)
            self.ui.statusBar.showMessage(
                "Action created. Choose a direction and double-click sheet cells to add animation frames.", 8000
            )

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

    def rotate_direction(self, step: int) -> None:
        """Turn through the direction pad, wrapping at either end."""
        direction = (self.current_direction + step) % len(self.directionButtons)
        self.directionButtons[direction].click()

    def choose_direction(self, direction: int) -> None:
        """Apply a user direction choice to the resource controlling its facing."""
        preview = self.point_preview
        target = self
        if preview is not None and self is not preview.root and preview.member(self).follow_facing:
            target = preview.root
        target.set_direction(direction)
        target.directionButtons[direction].setChecked(True)
        if preview is not None:
            preview.advance(0)
            preview.refresh()

    def set_direction(self, direction: int) -> None:
        if direction == self.current_direction:
            self.directionButtons[direction].setChecked(True)
            return

        for button in self.directionButtons:
            if button != self.directionButtons[direction]:
                button.setChecked(False)

        self.current_direction = direction
        playing = self.animating
        pyglet.clock.unschedule(self._playing_animation)

        self.ui.animationFrameList.clear()

        if self.current_anim_group:
            if self.current_anim_group.directions:
                self.current_sequence = self.current_anim_group.directions[
                    self.current_direction
                ]

                if self.current_sequence.frames:
                    self.current_anim_frame = self.current_sequence.frames[0]
                    self.set_anim_frame_values(self.current_anim_frame)
                else:
                    self.clear_anim_frame()
                    if self.sprite is not None:
                        self.sprite.visible = False
                        self.shadow.visible = False

                if self.current_sequence:
                    self.set_sequence_list()

                self.ui.animationFrameList.setCurrentRow(0)
                if playing and self.current_sequence.frames:
                    pyglet.clock.schedule_once(self._playing_animation,
                                               self.anim_speed * self.current_anim_frame.duration)

        self.updateBatchEditDialog()

    def clear_anim_frame(self) -> None:
        """Clears information boxes of the Frame Data."""
        self.current_anim_frame = None
        self.displayed_anim_frame = None
        if self.onionSprites or self.onion_shadow_sprites:
            self.openGLWidget.makeCurrent()
        for sprite in (*self.onionSprites, *self.onion_shadow_sprites):
            sprite.visible = False
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
        for index, frame in enumerate(self.current_sequence.frames):
            frame.idx = index
        for item in self._get_frame_list_items():
            item.update_text()
        if not self.current_sequence.frames:
            self.clear_anim_frame()
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
            self.window, "Select Sprite File", "", "Sprite Files (*.json *.xml)"
        )
        if file_name:
            self.load_animation(file_name)

    def load_animation(self, file_name: str | PathLike[str], *, add_to_recent: bool = True) -> bool:
        loaded = self._load_sprite_document(file_name, add_to_recent=add_to_recent)
        if loaded and self.ui.actionListWidget.count():
            self.ui.actionListWidget.setCurrentRow(0)
            self.ui.actionListWidget.item(0).mouseClickEvent(None)
        return loaded

    def load_sheet(self, file_name: str | PathLike[str]) -> bool:
        return self._load_sprite_document(file_name, SheetFormat.SINGLE)

    def _load_sprite_document(
        self, file_name: str | PathLike[str], expected: SheetFormat | SpriteFormat | None = None,
        *, add_to_recent: bool = True,
    ) -> bool:
        try:
            document = SpriteDocument.load(file_name, expected)
        except (FormatError, OSError, ValueError, ElementTree.ParseError) as error:
            self.create_error_popup(str(error))
            return False

        return self._install_sprite_document(document, str(file_name), add_to_recent=add_to_recent)

    def _install_sprite_document(self, document: SpriteDocument, file_name: str = "", *, add_to_recent: bool = True) -> bool:
        """Install already loaded/validated sprite data without requiring files."""

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
        self.cell_edit_revision = document.cell_edit_revision
        self.loaded_tree = document.source_tree
        self.sheet_format = document.sheet_format or SheetFormat.NATIVE
        self.texture_name = document.texture_name
        self.action_point_names = document.action_point_names
        self.rig_profile = document.rig_profile
        self.rig_profile_id = document.rig_profile_id
        self.refreshRigProfile(force=True)
        format_labels: dict[SheetFormat | str, str] = {
            SheetFormat.NATIVE: "JSON", SheetFormat.SINGLE: "Single", SheetFormat.MULTI: "Multi",
        }
        modeName = format_labels.get(self.sheet_format, str(self.sheet_format))
        self.ui.groupBox_5.setTitle(f"Sprite Sheet ({modeName})")
        self.single_loaded = self.sheet_format != SheetFormat.MULTI
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
        if file_name and add_to_recent:
            self.add_recent_list(file_name)
        self.trackArtwork()
        self.savedEditSignature = SpriteEditorWindow._document_state(self)
        self.ui.statusBar.showMessage(
            "Frame data and images loaded successfully.", 3000
        )
        if self.workspace:
            self.workspace.reset()
        return True

    @undoable("Add Frame")
    def add_new_animation_frame(self, frame_idx: int) -> None:
        """Create new animation frame in the sequence. Adds to the end."""
        if self.current_sequence:
            anim_idx = len(self.current_sequence.frames)
            anim_frame = AnimFrame(anim_idx, frame_idx, duration=max(1, self.ui.durationSpinBox.value()))

            self._add_anim_frame(anim_frame)

            self.clear_copy_group()

            self._notify_changes()

    def _add_anim_frame(self, anim_frame: AnimFrame) -> None:
        self.current_sequence.frames.append(anim_frame)

        item = AnimFrameItem(anim_frame, self)
        self.ui.animationFrameList.addItem(item)

        self._update_anim_frame_widgets()
        self.current_anim_frame = anim_frame
        self.ui.animationFrameList.setCurrentItem(item)
        self.set_anim_frame_values(anim_frame)
        self.set_animation()

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
        self.ui.sheetFramePicture.finishDrag(True)
        self.clearOnionPreview()
        self.clearPointPreview()
        self.undoStack.clear()
        self.savedEditSignature = None
        self.ui.groupBox_5.setTitle("Sprite Sheet")
        self.artworkReloadTimer.stop()
        self.artworkPath = None
        self.artworkDigest = None
        self.updateArtworkWatch()
        self.sheet_preview_images.clear()
        self.single_loaded = None
        self.sheet_format = None
        self.loaded_tree = None
        self.source_image_path = None
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

    def get_sprite_position(self, anim_frame: AnimFrame | None = None) -> tuple[float, float, float]:
        anim_frame = anim_frame or self.current_anim_frame
        return (
            (self.openGLWidget.width() // 2)
            + (anim_frame.sprite_offset.x * self.scale),
            (self.openGLWidget.height() // 3)
            + (-anim_frame.sprite_offset.y * self.scale),
            0,
        )

    def get_shadow_position(self, anim_frame: AnimFrame | None = None) -> tuple[float, float, float]:
        anim_frame = anim_frame or self.current_anim_frame
        return (
            (self.openGLWidget.width() // 2)
            + (anim_frame.shadow_offset.x * self.scale),
            (self.openGLWidget.height() // 3)
            + (-anim_frame.shadow_offset.y * self.scale),
            0,
        )

    def get_action_point_positions(
        self, anim_frame: AnimFrame | None = None
    ) -> tuple[tuple[float, float, float], ...]:
        anim_frame = anim_frame or self.current_anim_frame
        ap = self.action_points[anim_frame.frame_index]

        points = [point for _, point in ap.items()]
        if anim_frame.flip:
            width = self.image_grid[anim_frame.frame_index].width
            pivot = width - 1 - 2 * (width // 2)
            points = [Offset(pivot - point.x, point.y) for point in points]

        return tuple(
            (
                self.openGLWidget.width() // 2
                + (anim_frame.sprite_offset.x + point.x) * self.scale,
                self.openGLWidget.height() // 3
                - (anim_frame.sprite_offset.y + point.y) * self.scale,
                0,
            )
            for point in points
        )

    @staticmethod
    def actionPointColor(name):
        return {"left_hand": (255, 0, 0), "center": (0, 255, 0),
                "right_hand": (0, 0, 255), "head": (0, 0, 0),
                "shadow_point": (255, 255, 255)}.get(name, (255, 180, 0))

    def actionPointMarker(self, name):
        color = self.actionPointColor(name)
        size = 2 * self.ui.actionPointsSizeSlider.value() + 1
        key = (size, color)
        if key not in self._action_point_markers:
            image = create_point_marker(size, (*color, 255))
            image.anchor_x = image.anchor_y = size // 2
            self._action_point_markers[key] = image
        return self._action_point_markers[key]

    def changeActionPointSize(self, size):
        self.ui.actionPointsSizeLabel.setText(str(size))
        self.ui.sheetFramePicture.pointMarkerSize = size
        self.ui.sheetFramePicture.update()
        if self.sprite is not None:
            self.openGLWidget.makeCurrent()
            self.updateActionPointPreview(self.displayed_anim_frame or self.current_anim_frame)
        self.openGLWidget.update()

    def updateActionPointPreview(self, anim_frame: AnimFrame | None) -> None:
        if self.sprite is None or anim_frame is None:
            return
        positions = (
            self.get_action_point_positions(anim_frame)
            if anim_frame.frame_index in self.action_points
            else ()
        )
        shadow_position = (
            self.openGLWidget.width() // 2 + anim_frame.shadow_offset.x * self.scale,
            self.openGLWidget.height() // 3 - anim_frame.shadow_offset.y * self.scale,
            0,
        )
        body = self.action_points.get(anim_frame.frame_index)
        names = [name for name, _ in body.items()] if body else []
        names.append("shadow_point")
        for index, (name, position) in enumerate(zip(names, (*positions, shadow_position))):
            image = self.actionPointMarker(name)
            marker = self.ap_sprites[index] if index < len(self.ap_sprites) else None
            if marker is None:
                marker = pyglet.sprite.Sprite(
                    image,
                    batch=self.openGLWidget.batch,
                    group=pyglet.graphics.Group(10000 if name == "center" else 9999),
                )
                marker.scale = self.scale
                marker.opacity = self.ui.actionPointsOpacitySlider.value()
                self.ap_sprites.append(marker)
            marker.image = image
            if name in ("left_hand", "center", "right_hand", "head", "shadow_point"):
                setattr(self, name, marker)
            if marker is not None:
                if position is not None:
                    marker.position = position
                marker.visible = (
                    position is not None
                    and self.ui.showActionPointsCheckBox.isChecked()
                )
        for marker in self.ap_sprites[len(names):]:
            marker.visible = False

    def setOnionPreviewEnabled(self, enabled: bool) -> None:
        self.onionPreviewEnabled = enabled
        with QtCore.QSignalBlocker(self.ui.onionPreviewCheckBox):
            self.ui.onionPreviewCheckBox.setChecked(enabled)
        self.updateOnionPreview(self.displayed_anim_frame or self.current_anim_frame)
        self.openGLWidget.update()

    def changeOnionPreview(self, enabled: bool) -> None:
        self.settings.setValue("preferences/onionPreview", enabled)
        editors = self.document_host.preview_editors() if self.document_host else (self,)
        for editor in editors:
            editor.setOnionPreviewEnabled(enabled)
        self.settings.sync()

    def set_onion_shadows_enabled(self, enabled: bool) -> None:
        self.onion_shadows_enabled = enabled
        with QtCore.QSignalBlocker(self.ui.onionShadowsCheckBox):
            self.ui.onionShadowsCheckBox.setChecked(enabled)
        self.updateOnionPreview(self.displayed_anim_frame or self.current_anim_frame)
        self.openGLWidget.update()

    def change_onion_shadows(self, enabled: bool) -> None:
        self.settings.setValue("preferences/onionShadows", enabled)
        editors = self.document_host.preview_editors() if self.document_host else (self,)
        for editor in editors:
            editor.set_onion_shadows_enabled(enabled)
        self.settings.sync()

    def setOnionRange(self, lower: int, upper: int) -> None:
        self.onionPreviousFrames = max(0, min(5, -lower))
        self.onionNextFrames = max(0, min(5, upper))
        with QtCore.QSignalBlocker(self.ui.onionRangeSlider):
            self.ui.onionRangeSlider.setValues(-self.onionPreviousFrames, self.onionNextFrames)
        self.ui.onionRangeLabel.setText(f"{-self.onionPreviousFrames} to +{self.onionNextFrames}")
        self.updateOnionPreview(self.displayed_anim_frame or self.current_anim_frame)
        self.openGLWidget.update()

    def changeOnionRange(self, lower: int, upper: int) -> None:
        self.settings.setValue("preferences/onionPreviousFrames", -lower)
        self.settings.setValue("preferences/onionNextFrames", upper)
        editors = self.document_host.preview_editors() if self.document_host else (self,)
        for editor in editors:
            editor.setOnionRange(lower, upper)
        self.settings.sync()

    def setOnionOpacity(self, opacity: int) -> None:
        self.onionOpacity = max(0, min(255, opacity))
        with QtCore.QSignalBlocker(self.ui.onionOpacitySlider):
            self.ui.onionOpacitySlider.setValue(self.onionOpacity)
        self.ui.onionOpacityLabel.setText(f"{round(self.onionOpacity * 100 / 255)}%")
        self.updateOnionPreview(self.displayed_anim_frame or self.current_anim_frame)
        self.openGLWidget.update()

    def changeOnionOpacity(self, opacity: int) -> None:
        self.settings.setValue("preferences/onionOpacity", opacity)
        editors = self.document_host.preview_editors() if self.document_host else (self,)
        for editor in editors:
            editor.setOnionOpacity(opacity)
        self.settings.sync()

    def clearOnionPreview(self) -> None:
        if self.onionSprites or self.onion_shadow_sprites:
            self.openGLWidget.makeCurrent()
            for sprite in (*self.onionSprites, *self.onion_shadow_sprites):
                sprite.delete()
            self.onionSprites.clear()
            self.onion_shadow_sprites.clear()

    def onionPreviewNeighbors(self, frame: AnimFrame | None) -> list[tuple[AnimFrame, int]]:
        if not self.onionPreviewEnabled or self.current_sequence is None:
            return []
        frames = self.current_sequence.frames
        index = next((i for i, item in enumerate(frames) if item is frame), None)
        if index is None or len(frames) < 2:
            return []
        offsets = [*range(-self.onionPreviousFrames, 0), *range(1, self.onionNextFrames + 1)]
        neighbors = []
        seen = {index}
        for offset in sorted(offsets, key=lambda value: (abs(value), value)):
            neighbor = (index + offset) % len(frames)
            if neighbor not in seen:
                seen.add(neighbor)
                neighbors.append((frames[neighbor], abs(offset)))
        return neighbors

    def onionPreviewFrames(self, frame: AnimFrame | None) -> list[AnimFrame]:
        return [neighbor for neighbor, _ in self.onionPreviewNeighbors(frame)]

    def updateOnionPreview(self, frame: AnimFrame | None) -> None:
        if not self.openGLWidget.isValid() or self.openGLWidget._pyglet_context is None:
            return
        self.openGLWidget.makeCurrent()
        neighbors = self.onionPreviewNeighbors(frame)
        for index, (neighbor, distance) in enumerate(neighbors):
            image = self.image_grid[neighbor.frame_index]
            if neighbor.flip:
                image = image.get_texture().get_transform(flip_x=True)
                image.anchor_x = image.width // 2
                image.anchor_y = image.height // 2
            if index == len(self.onionSprites):
                sprite = pyglet.sprite.Sprite(
                    image, batch=self.openGLWidget.batch, group=pyglet.graphics.Group(1)
                )
                sprite.color = (190, 190, 190)
                self.onionSprites.append(sprite)
            sprite = self.onionSprites[index]
            sprite.image = image
            sprite.position = self.get_sprite_position(neighbor)
            sprite.scale = self.scale
            sprite.opacity = round(self.onionOpacity / distance)
            sprite.visible = True
        for sprite in self.onionSprites[len(neighbors):]:
            sprite.visible = False
        shadow_neighbors = neighbors if self.onion_shadows_enabled else []
        for index, (neighbor, distance) in enumerate(shadow_neighbors):
            image = self._display_shadow_image()
            if index == len(self.onion_shadow_sprites):
                self.onion_shadow_sprites.append(pyglet.sprite.Sprite(
                    image, batch=self.openGLWidget.batch, group=pyglet.graphics.Group(-1)
                ))
            sprite = self.onion_shadow_sprites[index]
            sprite.image = image
            sprite.position = self.get_shadow_position(neighbor)
            sprite.scale = self.scale
            sprite.opacity = round(self.onionOpacity / distance)
            sprite.visible = True
        for sprite in self.onion_shadow_sprites[len(shadow_neighbors):]:
            sprite.visible = False

    def _playing_animation(self, dt: float) -> None:
        if self.current_anim_frame is None or self.current_sequence is None or not self.current_sequence.frames:
            self.animating = False
            return
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
        if getattr(self, "point_preview", None) is not None:
            self.point_preview.toggle(self)
            return
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
                    first_frame = self.current_anim_group.directions[self.current_direction].frames[0]
                    frame = self.current_anim_frame or first_frame
                    sprite_pos = self.get_sprite_position(frame)
                    shadow_pos = self.get_shadow_position(frame)

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
                        group=pyglet.graphics.Group(2),
                    )
                    self.sprite.scale = self.scale
                    self.sprite.opacity = self.ui.spriteOpacitySlider.value()

                if self.sprite:
                    self.sprite.visible = True
                    self.shadow.visible = True
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

            self.sprite.position = self.get_sprite_position(anim_frame)
            self.shadow.position = self.get_shadow_position(anim_frame)

            self.updateActionPointPreview(anim_frame)
            self.updateOnionPreview(anim_frame)

    def import_multiple_sheets(self, file_name: str | PathLike[str]) -> bool:
        return self._load_sprite_document(file_name, SheetFormat.MULTI)

    def export_single_sheet(self) -> None:
        if self.sheet_format is not None:
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
            cell_edit_revision=self.cell_edit_revision,
            action_point_names=self.action_point_names, rig_profile=self.rig_profile,
            rig_profile_id=self.rig_profile_id,
            trim_copies=self.ui.actionTrim_Copies.isChecked(),
            collapse_singles=self.ui.actionCollapse_Singles.isChecked(),
        )

    def _export_bundle(
        self,
        directory: str | PathLike[str],
        format_name: SheetFormat | SpriteFormat,
        xml_name: str | None = None,
    ) -> bool:
        if self.frozen_preview:
            return False
        try:
            if self.image_grid is None:
                raise FormatError("No sprite data to save.")
            self._sprite_document().export(directory, format_name, filename=xml_name)
        except (FormatError, OSError, ValueError) as error:
            self.create_error_popup(str(error))
            return False
        message = (
            "Sprite bundle" if isinstance(format_name, SpriteFormat) else
            "Sprite document" if format_name == SheetFormat.NATIVE else
            "Single sheet" if format_name == SheetFormat.SINGLE else "Multisheet frames"
        )
        self.ui.statusBar.showMessage(f"{message} were output successfully.", 3000)
        return True

    def export_multiple_sheets(self) -> None:
        if self.sheet_format is not None:
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
        self.maximizedLayoutTimer = QtCore.QTimer(self)
        self.maximizedLayoutTimer.setSingleShot(True)
        self.maximizedLayoutTimer.timeout.connect(self.fit_maximized_layout)
        for name in ("mainSplitter", "previewSplitter"):
            state = self.settings.value(f"window/{name}")
            if isinstance(state, QtCore.QByteArray):
                self.panelStates[name] = state
        self._editors: dict[QtWidgets.QWidget, AnimationEditor] = {}
        self._holders: dict[QtWidgets.QWidget, QtWidgets.QMainWindow] = {}
        self._saved_states: dict[QtWidgets.QWidget, tuple[object, ...]] = {}
        self._active_editor: AnimationEditor | None = None
        self._linked_source_errors = {}
        self.linkedSourceTimer = QtCore.QTimer(self)
        self.linkedSourceTimer.setInterval(500)
        self.linkedSourceTimer.timeout.connect(self.refresh_rig_profiles)
        self.linkedSourceTimer.start()
        self.animationSettingsClipboard: AnimGroup | None = None
        self.actionPointsClipboard: ActionPoints | CopiedActionPoint | None = None
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
        self.newAction = QtGui.QAction("New…", self)
        self.newAction.setShortcut(QtGui.QKeySequence.StandardKey.New)
        self.newAction.triggered.connect(self.new_from_image_dialog)
        self.addAction(self.newAction)
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
                "Auto Reload Sheet",
                False,
                self.settingsMenu,
            ),
            ("showSpriteBounds", "Show Sprite Dimensions", False, self.viewMenu),
            ("includeWhitespace", "Include Sprite Whitespace", False, self.viewMenu),
            ("showOnionControls", "Show Onion Skinning", True, self.viewMenu),
            ("viewLayers", "View Layers", False, self.viewMenu),
        ):
            if name == "showOnionControls":
                menu.addSeparator()
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
        editor.setOnionPreviewEnabled(self.settings.value("preferences/onionPreview", False, bool))
        editor.set_onion_shadows_enabled(self.settings.value("preferences/onionShadows", False, bool))
        editor.ui.onionPreviewGroupBox.setVisible(
            self.preferenceActions["showOnionControls"].isChecked()
        )
        if editor.workspace:
            editor.workspace.set_visible(self.preferenceActions["viewLayers"].isChecked())

    def preview_editors(self):
        for editor in self._editors.values():
            yield editor

    def updateArtworkReloadAction(self) -> None:
        """Enable the reload preference only for an active single-sheet document."""
        editor = self.current_editor
        self.preferenceActions["autoReloadArtwork"].setEnabled(
            editor is not None and editor.sheet_format in (SheetFormat.SINGLE, SheetFormat.NATIVE)
        )

    def _restoreVisiblePanelLayout(self) -> None:
        editor = self.current_editor
        if editor is not None:
            subject = editor.workspace.active_editor if editor.workspace else editor
            subject.ui.updateEditorLayout()
            self._restorePanelLayout(editor)

    def _capturePanelLayout(self, editor: AnimationEditor) -> None:
        if editor.workspace:
            editor = editor.workspace.active_editor
        for name in ("mainSplitter", "previewSplitter"):
            self.panelStates[name] = getattr(editor.ui, name).saveState()

    def _restorePanelLayout(self, editor: AnimationEditor) -> None:
        if editor.workspace:
            editor = editor.workspace.active_editor
        for name, state in self.panelStates.items():
            getattr(editor.ui, name).restoreState(state)

    def set_control_group_collapsed(self, name: str, collapsed: bool) -> None:
        self.settings.setValue(f"window/collapsed/{name}", collapsed)
        for editor in self._editors.values():
            group = editor.ui.collapsible_groups[name]
            if group.is_collapsed() != collapsed:
                with QtCore.QSignalBlocker(group):
                    group.set_collapsed(collapsed)
                editor.ui.control_group_collapsed(name, collapsed)
            editor.ui.editorLayoutTimer.start(0)
        self.settings.sync()

    def fit_maximized_layout(self) -> None:
        """Recover the monitor bounds after an expanded layout enlarged the window."""
        handle = self.windowHandle()
        if (sys.platform != "win32" or not self.isVisible() or not self.isMaximized()
                or handle is None or self.screen() is None):
            return
        available = self.screen().availableGeometry()
        margins = handle.frameMargins()
        target = available.adjusted(margins.left(), margins.top(),
                                    -margins.right(), -margins.bottom())
        target.setWidth(max(target.width(), self.minimumWidth()))
        target.setHeight(max(target.height(), self.minimumHeight()))
        if handle.geometry() != target:
            # QWidget.resize/setGeometry clears the maximized state. QWindow's
            # geometry keeps it while undoing growth beyond the desktop.
            handle.setGeometry(target)

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
            (index, body.signature())
            for index, body in sorted(editor.action_points.items())
        )
        return (
            editor.shadow_size, editor.shadow_size_present, groups, points,
            editor.frame_width, editor.frame_height,
            editor.cell_edit_revision,
            tuple(editor.action_point_names), str(editor.rig_profile or ""),
            editor.rig_profile_id,
        )

    def refresh_rig_profiles(self):
        for editor in self.preview_editors():
            editor.refreshRigProfile()

    def _create_document(self) -> tuple[QtWidgets.QWidget, AnimationEditor]:
        # Reuse the editor UI without embedding a second menu/status bar in a tab.
        holder = QtWidgets.QMainWindow(self)
        holder.hide()
        ui = Ui_MainWindow()
        ui.setupUi(holder)
        ui.collapseLayoutTimer.timeout.connect(lambda: self.maximizedLayoutTimer.start(0))
        ui.editorLayoutTimer.timeout.connect(lambda: self.maximizedLayoutTimer.start(0))
        page = holder.takeCentralWidget()
        editor = AnimationEditor(self.app, self, ui, document_host=self)
        for name, group in ui.collapsible_groups.items():
            group.set_collapsed(self.settings.value(f"window/collapsed/{name}", False, bool))
            group.collapsed_changed.connect(partial(self.set_control_group_collapsed, name))
        editor.workspace = LayerWorkspace(editor, page, holder, AnimationEditor)
        page = editor.workspace.widget
        self._applyPreviewPreferences(editor)
        self._restorePanelLayout(editor)
        editor.openGLWidget.timer.stop()
        ui.menuFile.insertAction(ui.actionExit, self.closeTabAction)
        ui.menuFile.insertAction(ui.actionLoad, self.newAction)
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

    def create_frozen_document(self, document: SpriteDocument, name: str, *, source_label: str = "", saved_name: bool = False) -> AnimationEditor:
        """Install an independent, fileless comparison with no authoring or reloads."""
        import re
        from uuid import uuid4
        used = {self.documentTabs.tabText(i).casefold() for i in range(self.documentTabs.count())}
        legacy = re.fullmatch(r"(.*) \((?:Snapshot|Frozen)\)(?: \((\d+)\))?", name) if saved_name else None
        if legacy:
            base = legacy[1]
            suffix = int(legacy[2]) + 1 if legacy[2] else 1
            name = base + " " + "^" * suffix
        elif saved_name:
            base = name.rstrip("^").rstrip()
            suffix = len(name) - len(name.rstrip("^"))
        else:
            base, suffix = name, 1
            name = base + " ^"
        while name.casefold() in used:
            suffix += 1
            name = base + " " + "^" * suffix
        page, editor = self._create_document()
        try:
            editor._install_sprite_document(copy.deepcopy(document), add_to_recent=False)
            if editor.groups:
                editor.ui.actionListWidget.setCurrentRow(0)
                editor.ui.actionListWidget.item(0).mouseClickEvent(None)
            editor.frozen_preview = True
            editor.frozen_snapshot_id = uuid4().hex
            editor.frozen_origin_label = source_label
            editor.untitled_name = name
            editor.setArtworkReloadEnabled(False)
            # Disable authoring containers so selection updates cannot re-enable edits.
            for widget in (editor.ui.actionPointsGroupBox, editor.ui.groupBox_7,
                           editor.ui.frameDataPanel, editor.ui.shadowSizeComboBox,
                           editor.ui.sheetFramePicture, editor.ui.actionAddButton,
                           editor.ui.actionDuplicateButton, editor.ui.actionDeleteButton,
                           editor.ui.pasteSequenceButton, editor.ui.hitPointButton,
                           editor.ui.returnPointButton, editor.ui.rushPointButton,
                           *editor.ui.sequenceButtonWidgets):
                widget.setEnabled(False)
            editor.editMenu.setEnabled(False)
            for action in (editor.ui.actionSave, editor.ui.actionSave_As,
                           editor.ui.actionExportAll_Animations, editor.ui.actionExportSingle_Animation):
                action.setEnabled(False)
            self._saved_states[page] = self._document_state(editor)
            self.documentTabs.addTab(page, name)
            self.documentTabs.setCurrentWidget(page)
            self._update_tab(page)
            return editor
        except Exception:
            self._dispose_document(page)
            raise

    def open_files_dialog(self) -> list[AnimationEditor]:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open Sprites and Previews", "", "Sprites and Previews (*.json *.xml *.mdpreview);;Sprite Files (*.json *.xml);;Editor Previews (*.mdpreview)"
        )
        return self.open_files(paths)

    def new_from_image_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose Image for New Sprite", "", IMAGE_FILTER)
        if path:
            self.open_file(path)

    def open_image(self, path: str, *, add_to_recent: bool = True) -> AnimationEditor | None:

        try:
            with Image.open(path) as source:
                if getattr(source, "n_frames", 1) != 1:
                    raise FormatError("Open a single-frame image containing the atlas cells.")
                image = source.convert("RGBA")
            dialog = ImageAtlasDialog(image, self,
                cell_width=self.settings.value("imageImport/cellWidth", 32, int),
                cell_height=self.settings.value("imageImport/cellHeight", 32, int))
            try:
                if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                    return None
                document = dialog.document
            finally:
                dialog.deleteLater()
        except (FormatError, OSError, ValueError) as error:
            self.current_editor.create_error_popup(str(error))
            return None
        page = self.documentTabs.currentWidget()
        editor = self.current_editor
        if editor is None or editor.sheet_format is not None:
            page, editor = self._create_document()
        editor._install_sprite_document(document)
        editor.source_image_path = pathlib.Path(path)
        editor.ui.actionListWidget.setCurrentRow(0)
        editor.ui.actionListWidget.item(0).mouseClickEvent(None)
        editor.selectSheetFrame(0)
        base = f"{pathlib.Path(path).stem} (unsaved)"
        used = {self.documentTabs.tabText(i) for i in range(self.documentTabs.count()) if self.documentTabs.widget(i) is not page}
        name, suffix = base, 1
        while name in used:
            name = f"{base} ({suffix})"
            suffix += 1
        editor.untitled_name = name
        self._saved_states[page] = self._document_state(editor)
        if self.documentTabs.indexOf(page) < 0:
            self.documentTabs.addTab(page, "")
        self.documentTabs.setCurrentWidget(page)
        self._update_tab(page)
        self.updateArtworkReloadAction()
        self.updateInfoAction()
        self.settings.setValue("imageImport/cellWidth", document.frame_width)
        self.settings.setValue("imageImport/cellHeight", document.frame_height)
        if add_to_recent:
            editor.add_recent_list(path)
        editor.ui.statusBar.showMessage("Sprite created with a Default action. Choose a direction and double-click sheet cells to build its animation. Use + to add more actions. Save to create JSON and PNG files.", 15000)
        return editor

    def open_files(self, paths: Iterable[str | PathLike[str]]) -> list[AnimationEditor]:
        opened = []
        for path in paths:
            editor = self.open_file(path)
            if editor is not None:
                opened.append(editor)
        return opened

    def open_file(self, path: str | PathLike[str], *, add_to_recent: bool = True) -> AnimationEditor | None:
        path = str(pathlib.Path(path).resolve())
        if is_preview_path(path):
            from gui.point_preview import load_preview
            try:
                return load_preview(self, path, add_to_recent=add_to_recent).root
            except (FormatError, OSError, ValueError) as error:
                self.current_editor.create_error_popup(str(error))
                return None
        for page, editor in self._editors.items():
            if editor.file_name and self._path_key(editor.file_name) == self._path_key(
                path
            ):
                self.documentTabs.setCurrentWidget(page)
                return editor
            if (not editor.file_name and getattr(editor, "source_image_path", None) is not None
                    and self._path_key(editor.source_image_path) == self._path_key(path)):
                self.documentTabs.setCurrentWidget(page)
                return editor
        if pathlib.Path(path).suffix.lower() in IMAGE_SUFFIXES:
            return self.open_image(path, add_to_recent=add_to_recent)
        page = self.documentTabs.currentWidget()
        editor = self.current_editor
        reuse_empty = editor is not None and editor.sheet_format is None
        if not reuse_empty:
            page, editor = self._create_document()
        try:
            loaded = editor.load_animation(path, add_to_recent=add_to_recent)
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
            if not editor.file_name:
                self.documentTabs.setTabText(index, getattr(editor, "untitled_name", "Untitled"))
                self.documentTabs.setTabToolTip(index, ("Snapshot (read-only): " + editor.frozen_origin_label)
                                               if editor.frozen_preview else "Unsaved sprite")
                if editor is self.current_editor:
                    self._update_window_title()
                return
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
            if editor and editor.sheet_format is not None
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
            self.batchEditDialog.setEditor(editor.workspace.active_editor if editor and editor.workspace else editor)
        if editor is not None:
            editor.undoAction.setShortcuts(QtGui.QKeySequence.StandardKey.Undo)
            editor.redoAction.setShortcuts(QtGui.QKeySequence.StandardKey.Redo)
            self._restorePanelLayout(editor)
            if editor.point_preview is not None:
                editor.point_preview.refresh()
            subject = editor.workspace.active_editor if editor.workspace else editor
            for menu in (editor.ui.menuFile, subject.editMenu):
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
            editor._read_recent_files()
            editor._update_recent_actions()

    def can_save_path(self, editor: AnimationEditor, path: str | PathLike[str]) -> bool:
        target = pathlib.Path(path)
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
        if editor.point_preview is not None:
            editor.point_preview.source_saved(editor)
        self.updateArtworkReloadAction()
        for page, document in self._editors.items():
            if document is editor:
                self._saved_states[page] = self._document_state(editor)
                self._update_tab(page)
                break


    def spriteContentChanged(self, editor: AnimationEditor) -> bool:
        if editor.frozen_preview:
            return False
        page = next(page for page, other in self._editors.items() if other is editor)
        return self._saved_states[page] != self._document_state(editor)

    def _confirm_close(self, page: QtWidgets.QWidget) -> bool:
        editor = self._editors[page]
        if editor.frozen_preview:
            return True
        if editor.sheet_format is None or (editor.file_name and self._saved_states.get(
            page
        ) == self._document_state(editor)):
            return True
        self.documentTabs.setCurrentWidget(page)
        choice = QtWidgets.QMessageBox.question(
            self,
            "Save changes?",
            f"Save changes to {editor.file_name or self.documentTabs.tabText(self.documentTabs.indexOf(page))}?",
            QtWidgets.QMessageBox.StandardButton.Save
            | QtWidgets.QMessageBox.StandardButton.Discard
            | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Save,
        )
        if choice == QtWidgets.QMessageBox.StandardButton.Cancel:
            return False
        if choice == QtWidgets.QMessageBox.StandardButton.Save:
            return editor.save_action_trigger()
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
        if editor.point_preview is not None:
            editor.point_preview.remove(editor)
        if editor.workspace:
            editor.workspace.dispose()
        self._dispose_editor_resources(editor)
        self._saved_states.pop(page, None)
        page.deleteLater()
        self._holders.pop(page).deleteLater()

    def _dispose_editor_resources(self, editor: AnimationEditor) -> None:
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

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if not all(self._confirm_close(page) for page in self._editors):
            event.ignore()
            return
        self._saveWindowLayout()
        self.linkedSourceTimer.stop()
        if self.batchEditDialog is not None:
            self.batchEditDialog.setEditor(None)
            self.batchEditDialog.close()
        self.menuBar().clear()
        self._active_editor = None
        for page in list(self._editors):
            if page in self._editors:
                self._dispose_document(page)
        super().closeEvent(event)


def excepthook(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_tb: TracebackType | None,
) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        app = QtWidgets.QApplication.instance()
        if app is not None:
            # Exceptions in Qt callbacks do not unwind QApplication.exec().
            app.exit(130)
            return
    tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(tb)


def run_gui() -> int:
    app = QtWidgets.QApplication(sys.argv)
    previous_excepthook = sys.excepthook
    previous_sigint = signal.signal(signal.SIGINT, lambda *_: app.exit(130))
    sys.excepthook = excepthook
    # Keep Python signal delivery responsive even when no preview is repainting.
    interrupt_timer = QtCore.QTimer(app)
    interrupt_timer.timeout.connect(lambda: None)
    interrupt_timer.start(100)
    try:
        main_window = SpriteEditorWindow(app)
        main_window.show()
        return app.exec()
    finally:
        interrupt_timer.stop()
        signal.signal(signal.SIGINT, previous_sigint)
        sys.excepthook = previous_excepthook


if __name__ == "__main__":
    sys.exit(run_gui())
