"""Self-contained desktop smoke tests without a GPU or other test files."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


def make_sprite(directory: Path) -> Path:
    """Create a complete one-frame sprite without downloading any assets."""
    directory.mkdir()
    image = Image.new("RGBA", (4, 4), (240, 120, 60, 255))
    image.save(directory / "Idle-Anim.png")
    offsets = Image.new("RGBA", image.size)
    for position, color in (
        ((0, 1), (255, 0, 0, 255)),
        ((1, 2), (0, 255, 0, 255)),
        ((2, 1), (0, 0, 255, 255)),
        ((1, 0), (0, 0, 0, 255)),
    ):
        offsets.putpixel(position, color)
    offsets.save(directory / "Idle-Offsets.png")
    shadow = Image.new("RGBA", image.size)
    shadow.putpixel((2, 3), (255, 255, 255, 255))
    shadow.save(directory / "Idle-Shadow.png")
    xml = directory / "AnimData.xml"
    xml.write_text(
        "<AnimData><ShadowSize>0</ShadowSize><Anims><Anim>"
        "<Name>Idle</Name><Index>0</Index><FrameWidth>4</FrameWidth>"
        "<FrameHeight>4</FrameHeight><Durations><Duration>1</Duration>"
        "</Durations></Anim></Anims></AnimData>",
        encoding="utf-8",
    )
    return xml


class EditorSmokeTest(unittest.TestCase):
    def test_editor_starts_loads_a_sprite_and_closes(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import pyglet

        # Hosted runners need not provide the OpenGL 3.3 driver used for rendering.
        pyglet.options["shadow_window"] = False
        import main

        class PreviewStub(main.QtWidgets.QWidget):
            def __init__(
                self,
                width: int,
                height: int,
                parent: main.QtWidgets.QWidget | None,
                window: main.QtWidgets.QMainWindow,
                editor: main.AnimationEditor,
            ) -> None:
                super().__init__(parent)
                self.timer = main.QtCore.QTimer(self)
                self.camera = SimpleNamespace(zoom=1)

            def isValid(self) -> bool:
                return False

            def makeCurrent(self) -> None:
                pass

            def cleanup_gl_resources(self) -> None:
                pass

            def setCameraLocked(self, locked: bool) -> None:
                self.cameraLocked = locked

        app = main.QtWidgets.QApplication.instance() or main.QtWidgets.QApplication([])
        app.setQuitOnLastWindowClosed(True)
        errors = []
        with tempfile.TemporaryDirectory(prefix="mdframeedit-smoke-") as folder:
            root = Path(folder)
            source = make_sprite(root / "sprite")
            settings = main.QSettings(
                str(root / "settings.ini"), main.QSettings.Format.IniFormat
            )
            with (
                patch.object(main, "QSettings", return_value=settings),
                patch.object(main, "PygletWidget", PreviewStub),
                patch.object(
                    main.AnimationEditor,
                    "create_error_popup",
                    side_effect=AssertionError,
                ),
                patch.object(sys, "excepthook", lambda *error: errors.append(error)),
            ):
                window = main.SpriteEditorWindow(app)
                try:
                    action = window.preferenceActions["autoReloadArtwork"]
                    self.assertFalse(action.isEnabled())
                    editor = window.open_file(source)
                    self.assertIsNotNone(editor)
                    self.assertEqual(editor.ui.actionListWidget.count(), 1)
                    self.assertEqual(editor.ui.loadedSheetFrameList.count(), 1)
                    self.assertEqual(editor.current_anim_group.name, "Idle")
                    self.assertEqual(editor.ui.groupBox_5.title(), "Sprite Sheet (Multi)")
                    self.assertFalse(window.windowIcon().isNull())
                    self.assertFalse(action.isEnabled())
                    self.assertFalse(action.isChecked())
                    action.setChecked(True)
                    self.assertEqual(editor.artworkWatcher.files(), [])
                    single = root / "single"
                    single.mkdir()
                    self.assertTrue(editor._export_single_sheet(single))
                    editor = window.open_file(single / "FrameData.xml")
                    self.assertIsNotNone(editor)
                    self.assertEqual(editor.ui.groupBox_5.title(), "Sprite Sheet (Single)")
                    self.assertTrue(action.isEnabled())
                    window.open_file(source)
                    self.assertFalse(action.isEnabled())
                    self.assertTrue(action.isChecked())
                    window.open_file(single / "FrameData.xml")
                    self.assertTrue(action.isEnabled())
                    self.assertTrue(action.isChecked())
                    self.assertIn(
                        (single / "Anim.png").resolve(),
                        [Path(path).resolve() for path in editor.artworkWatcher.files()],
                    )
                    self.assertTrue(
                        settings.value("preferences/autoReloadArtwork", False, bool)
                    )
                    frame = editor.current_anim_frame
                    frame.duration = 7
                    points = editor.action_points
                    selected = editor.selected_sheet_frame
                    digest = editor.artworkDigest
                    with Image.open(single / "Anim.png") as image:
                        changed = image.convert("RGBA")
                    changed.putpixel((0, 0), (20, 30, 40, 255))
                    changed.save(single / "replacement.png")
                    (single / "replacement.png").replace(single / "Anim.png")
                    # Exercise the actual file watcher, including atomic replacement.
                    loop = main.QtCore.QEventLoop()
                    poll = main.QtCore.QTimer()
                    poll.timeout.connect(
                        lambda: loop.quit() if editor.artworkDigest != digest else None
                    )
                    poll.start(20)
                    deadline = main.QtCore.QTimer()
                    deadline.setSingleShot(True)
                    deadline.timeout.connect(loop.quit)
                    deadline.start(5000)
                    loop.exec()
                    poll.stop()
                    deadline.stop()
                    self.assertNotEqual(editor.artworkDigest, digest)
                    self.assertEqual(
                        editor._sprite_document().sheet_image.getpixel((0, 0)),
                        (20, 30, 40, 255),
                    )
                    self.assertIs(editor.current_anim_frame, frame)
                    self.assertEqual(frame.duration, 7)
                    self.assertIs(editor.action_points, points)
                    self.assertEqual(editor.selected_sheet_frame, selected)
                    digest = editor.artworkDigest
                    Image.new("RGBA", (1, 1)).save(single / "Anim.png")
                    editor.reloadChangedArtwork()
                    self.assertEqual(editor.artworkDigest, digest)
                    self.assertIn("Reopen", editor.ui.statusBar.currentMessage())
                    action.setChecked(False)
                    self.assertEqual(editor.artworkWatcher.files(), [])
                    self.assertFalse(editor.artworkReloadTimer.isActive())
                    # Avoid a save prompt for the deliberate unsaved frame edit.
                    frame.duration = 1
                    window.show()
                    main.QtCore.QTimer.singleShot(250, window.close)
                    watchdog = main.QtCore.QTimer()
                    watchdog.setSingleShot(True)
                    watchdog.timeout.connect(lambda: app.exit(1))
                    watchdog.start(10000)
                    self.assertEqual(app.exec(), 0)
                    watchdog.stop()
                    self.assertFalse(window.isVisible())
                    self.assertFalse(errors, errors)
                finally:
                    window.close()

    def test_cli_converts_both_formats(self) -> None:
        executable = os.environ.get("MDFRAMEEDIT_TEST_EXECUTABLE")
        command = (
            [str(Path(executable).resolve())]
            if executable
            else [sys.executable, str(PROJECT_ROOT / "main.py")]
        )
        with tempfile.TemporaryDirectory(prefix="mdframeedit-smoke-cli-") as folder:
            root = Path(folder)
            source = make_sprite(root / "source")
            for target, output in (
                ("single", root / "single"),
                ("multi", root / "multi"),
            ):
                result = subprocess.run(
                    [
                        *command,
                        "convert",
                        str(source),
                        "--to",
                        target,
                        "--output",
                        str(output),
                        "--jobs",
                        "2",
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                # Windowed builds may not expose stdout. Validate their files below.
                if not executable:
                    self.assertIn("Converted 1; failed 0.", result.stdout)
                source = output / (
                    "FrameData.xml" if target == "single" else "AnimData.xml"
                )
                self.assertTrue(source.is_file())
            with Image.open(root / "multi" / "Idle-Anim.png") as image:
                self.assertIn(
                    (16, (240, 120, 60, 255)), image.convert("RGBA").getcolors()
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
