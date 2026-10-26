"""Exercise interrupts across the real Qt event-loop boundary in child processes."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ShutdownTest(unittest.TestCase):
    def run_shutdown(self, action: str, expected_code: int) -> str:
        script = """
import signal
import sys
import pyglet
pyglet.options['shadow_window'] = False
import main

class Window(main.QtWidgets.QWidget):
    def __init__(self, app):
        super().__init__()
        main.QtCore.QTimer.singleShot(100, self.stop)

    def stop(self):
        ACTION

main.SpriteEditorWindow = Window
sys.exit(main.run_gui())
""".replace("ACTION", action)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, expected_code, output)
        return output

    def test_sigint_exits_event_loop(self) -> None:
        output = self.run_shutdown("signal.raise_signal(signal.SIGINT)", 130)
        self.assertNotIn("KeyboardInterrupt", output)
        self.assertNotIn("Traceback", output)

    def test_keyboard_interrupt_in_qt_callback_exits_event_loop(self) -> None:
        output = self.run_shutdown("raise KeyboardInterrupt", 130)
        self.assertNotIn("Traceback", output)

    def test_normal_close_exits_successfully(self) -> None:
        self.run_shutdown("self.close()", 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
