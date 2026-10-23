"""Action-point mouse gestures, controls and undo without a GPU."""

from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pyglet
pyglet.options["shadow_window"] = False
from PySide6 import QtCore, QtGui, QtWidgets

import main
from data import Offset
from tests.runner.test_smoke import make_sprite


class PreviewStub(QtWidgets.QWidget):
    def __init__(self, width: int, height: int, parent: QtWidgets.QWidget,
                 window: QtWidgets.QMainWindow, editor: main.AnimationEditor) -> None:
        super().__init__(parent)
        self.timer = QtCore.QTimer(self)
        self.camera = SimpleNamespace(zoom=1)

    def isValid(self) -> bool:
        return False

    def makeCurrent(self) -> None:
        pass

    def cleanup_gl_resources(self) -> None:
        pass

    def setCameraLocked(self, locked: bool) -> None:
        self.cameraLocked = locked


class ActionPointDragTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        settings = main.QSettings(str(root / "settings.ini"), main.QSettings.Format.IniFormat)
        self.stack.enter_context(patch.object(main, "QSettings", return_value=settings))
        self.stack.enter_context(patch.object(main, "PygletWidget", PreviewStub))
        self.stack.enter_context(patch.object(main.AnimationEditor, "create_error_popup", side_effect=AssertionError))
        self.window = main.SpriteEditorWindow(self.app)
        self.addCleanup(self.close_window)
        self.editor = self.window.open_file(make_sprite(root / "sprite"))
        self.preview = self.editor.ui.sheetFramePicture
        self.preview.resize(240, 180)
        self.editor.ui.showActionPointsCheckBox.setChecked(True)

    def close_window(self) -> None:
        for page, editor in self.window._editors.items():
            state = main.SpriteEditorWindow._document_state(editor)
            editor.savedEditSignature = state
            self.window._saved_states[page] = state
        self.window.close()
        self.app.processEvents()

    def mouse(self, kind: QtCore.QEvent.Type, position: QtCore.QPointF) -> None:
        button = QtCore.Qt.MouseButton.LeftButton
        event = QtGui.QMouseEvent(kind, position, position, button,
            button if kind != QtCore.QEvent.Type.MouseButtonRelease else QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(self.preview, event)

    def point_position(self, index: int) -> QtCore.QPointF:
        origin, scale = self.preview.frameTransform()
        x, y, _ = self.preview.points[index]
        return origin + QtCore.QPointF(x + 0.5, y + 0.5) * scale

    def test_drag_changes_source_point_and_controls_with_one_undo(self) -> None:
        self.editor.current_anim_frame.flip = 1
        for point, field, control in ((0, "left_hand", "leftHand"), (1, "center", "center"),
                                      (2, "right_hand", "rightHand"), (3, "head", "head")):
            with self.subTest(point=field):
                before = copy.deepcopy(self.editor.action_points[0])
                history = self.editor.undoStack.index()
                origin, scale = self.preview.frameTransform()
                start = self.point_position(point)
                end = start + QtCore.QPointF(5, -4) * scale
                self.mouse(QtCore.QEvent.Type.MouseButtonPress, start)
                self.mouse(QtCore.QEvent.Type.MouseMove, end)
                self.assertEqual(self.preview.frameTransform(), (origin, scale))
                self.mouse(QtCore.QEvent.Type.MouseButtonRelease, end)
                expected = getattr(before, field) + Offset(5, -4)
                self.assertEqual(getattr(self.editor.action_points[0], field), expected)
                self.assertEqual(tuple(b.value() for b in self.editor.ui.actionPointSpinBoxes[control]), expected.data())
                self.assertEqual(self.editor.undoStack.index(), history + 1)
                self.editor.undoStack.undo()
                self.assertEqual(self.editor.action_points[0], before)
                self.editor.undoStack.redo()
                self.assertEqual(getattr(self.editor.action_points[0], field), expected)
                self.editor.undoStack.undo()

    def test_clicks_do_not_move_points_and_escape_cancels(self) -> None:
        before = copy.deepcopy(self.editor.action_points)
        history = self.editor.undoStack.index()
        start = self.point_position(3)
        for position in (start, QtCore.QPointF(0, 0)):
            self.mouse(QtCore.QEvent.Type.MouseButtonPress, position)
            self.mouse(QtCore.QEvent.Type.MouseButtonRelease, position + QtCore.QPointF(1, 0))
        self.assertEqual(self.editor.action_points, before)
        self.mouse(QtCore.QEvent.Type.MouseButtonPress, start)
        self.mouse(QtCore.QEvent.Type.MouseMove, start + QtCore.QPointF(70, -30))
        self.assertNotEqual(self.editor.action_points, before)
        self.app.sendEvent(self.preview, QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,
            QtCore.Qt.Key.Key_Escape, QtCore.Qt.KeyboardModifier.NoModifier))
        self.assertEqual(self.editor.action_points, before)
        self.assertEqual(self.editor.undoStack.index(), history)


if __name__ == "__main__":
    unittest.main(verbosity=2)
