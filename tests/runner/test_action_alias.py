"""Offline regression checks for aliases, output isolation, logs, and the dialog."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from action_alias import add_action_aliases
from formats import validate_source


def make_single(folder, names=("Idle",), copies=()):
    folder.mkdir(parents=True)
    Image.new("RGBA", (2, 2), (100, 150, 200, 255)).save(folder / "Anim.png")
    root = ET.Element("FrameData")
    ET.SubElement(root, "FrameWidth").text = "2"
    ET.SubElement(root, "FrameHeight").text = "2"
    anims = ET.SubElement(root, "Anims")
    for index, name in enumerate(names):
        anim = ET.SubElement(anims, "Anim")
        ET.SubElement(anim, "Name").text = name
        ET.SubElement(anim, "Index").text = str(index)
        sequence = ET.SubElement(ET.SubElement(anim, "Sequences"), "AnimSequence")
        frame = ET.SubElement(sequence, "AnimFrame")
        for tag, value in (("FrameIndex", 0), ("Duration", 3), ("HFlip", 0)):
            ET.SubElement(frame, tag).text = str(value)
        for tag in ("Sprite", "Shadow"):
            offset = ET.SubElement(frame, tag)
            ET.SubElement(offset, "XOffset").text = "0"
            ET.SubElement(offset, "YOffset").text = "0"
    for name, target in copies:
        anim = ET.SubElement(anims, "Anim")
        ET.SubElement(anim, "Name").text = name
        ET.SubElement(anim, "CopyOf").text = target
    path = folder / "FrameData.xml"
    ET.ElementTree(root).write(path)
    return path


def snapshot(folder):
    return {path.relative_to(folder): path.read_bytes() for path in folder.rglob("*") if path.is_file()}


class ActionAliasTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.output = self.root / "output"

    def test_ordered_fallbacks_existing_missing_errors_and_log(self):
        make_single(self.source / "primary", ("Idle", "Hover"))
        make_single(self.source / "secondary")
        existing = make_single(self.source / "existing", ("Idle", "fLY"))
        missing = make_single(self.source / "missing", ("Sleep",))
        broken = self.source / "broken" / "FrameData.xml"
        broken.parent.mkdir()
        broken.write_text("not xml")
        (self.source / "notes.txt").write_text("keep me")
        before = snapshot(self.source)
        log, counts = add_action_aliases(self.source, self.output, " Fly ", ["hover", "Idle"])
        self.assertEqual(counts, {"ADDED": 2, "ALREADY_EXISTS": 1, "NO_FALLBACK": 1, "ERROR": 1})
        for sprite, target in (("primary", "Hover"), ("secondary", "Idle")):
            path = self.output / sprite / "FrameData.xml"
            tree = validate_source(path)
            alias = tree.getroot().find("Anims")[-1]
            self.assertEqual({child.tag: child.text for child in alias}, {
                "Name": "Fly", "Index": "2" if sprite == "primary" else "1", "CopyOf": target,
            })
            self.assertEqual((path.parent / "Anim.png").read_bytes(), (self.source / sprite / "Anim.png").read_bytes())
        for path in (existing, missing, broken):
            self.assertEqual(path.read_bytes(), (self.output / path.relative_to(self.source)).read_bytes())
        self.assertEqual(snapshot(self.source), before)
        self.assertEqual((self.output / "notes.txt").read_text(), "keep me")
        report = log.read_text()
        for status in counts:
            self.assertIn(status, report)
        self.assertIn("CopyOf Hover", report)
        self.assertIn("TOTAL", report)

    def test_copy_chain_is_resolved_to_direct_source(self):
        make_single(self.source, copies=(("Hover", "Float"), ("Float", "Idle")))
        _, counts = add_action_aliases(self.source, self.output, "Fly", ["Hover"])
        self.assertEqual(counts, {"ADDED": 1})
        self.assertEqual(validate_source(self.output / "FrameData.xml").getroot().find("Anims")[-1].findtext("CopyOf"), "Idle")

    def test_multi_sheet_alias_uses_local_source_and_keeps_artwork(self):
        from tests.runner.test_smoke import make_sprite

        self.source.mkdir()
        make_sprite(self.source / "multi")
        before = snapshot(self.source)
        _, counts = add_action_aliases(self.source, self.output, "Fly", ["Idle"], 20)
        self.assertEqual(counts, {"ADDED": 1})
        alias = validate_source(self.output / "multi" / "AnimData.xml").getroot().find("Anims")[-1]
        self.assertEqual(alias.findtext("CopyOf"), "Idle")
        self.assertEqual(alias.findtext("Index"), "20")
        for path, data in before.items():
            if path.suffix == ".png":
                self.assertEqual((self.output / path).read_bytes(), data)
        self.assertEqual(snapshot(self.source), before)

    def test_index_conflict_is_skipped(self):
        make_single(self.source)
        before = (self.source / "FrameData.xml").read_bytes()
        _, counts = add_action_aliases(self.source, self.output, "Fly", ["Idle"], 0)
        self.assertEqual(counts, {"INDEX_CONFLICT": 1})
        self.assertEqual((self.output / "FrameData.xml").read_bytes(), before)

    def test_output_cannot_overlap_or_replace_existing_folder(self):
        make_single(self.source)
        for destination in (self.source, self.source / "child", self.root):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                add_action_aliases(self.source, destination, "Fly", ["Idle"])
        self.output.mkdir()
        with self.assertRaises(ValueError):
            add_action_aliases(self.source, self.output, "Fly", ["Idle"])

    def test_bad_configuration_creates_no_output(self):
        make_single(self.source)
        for action, fallbacks in (("", ["Idle"]), ("Fly", []), ("Fly", ["fLY"])):
            with self.subTest(action=action, fallbacks=fallbacks), self.assertRaises(ValueError):
                add_action_aliases(self.source, self.output, action, fallbacks)
            self.assertFalse(self.output.exists())

    def test_existing_log_is_preserved(self):
        make_single(self.source)
        (self.source / "action-alias.log").write_text("previous log")
        log, _ = add_action_aliases(self.source, self.output, "Fly", ["Idle"])
        self.assertEqual(log.name, "action-alias-1.log")
        self.assertEqual((self.output / "action-alias.log").read_text(), "previous log")

    def test_failed_write_keeps_output_xml_and_logs_error(self):
        make_single(self.source)
        with patch("action_alias.os.replace", side_effect=OSError("test write failure")):
            log, counts = add_action_aliases(self.source, self.output, "Fly", ["Idle"])
        self.assertEqual(counts, {"ERROR": 1})
        self.assertIn("test write failure", log.read_text())
        self.assertEqual((self.output / "FrameData.xml").read_bytes(), (self.source / "FrameData.xml").read_bytes())
        self.assertEqual(len(list(self.output.glob("*.xml"))), 1)

    def test_invalid_copy_does_not_create_alias(self):
        make_single(self.source, copies=(("Hover", "missing"),))
        _, counts = add_action_aliases(self.source, self.output, "Fly", ["Hover", "Idle"])
        self.assertEqual(counts, {"ERROR": 1})

    def test_copy_failure_retains_a_log_and_leaves_source_unchanged(self):
        make_single(self.source)
        before = snapshot(self.source)
        with patch("action_alias.shutil.copytree", side_effect=OSError("test copy failure")):
            with self.assertRaisesRegex(OSError, "Partial output and log retained"):
                add_action_aliases(self.source, self.output, "Fly", ["Idle"])
        self.assertEqual(snapshot(self.source), before)
        self.assertIn("COPY_ERROR", (self.output / "action-alias.log").read_text())

    def test_ambiguous_fallback_requires_exact_spelling(self):
        make_single(self.source, ("Idle", "IDLE"))
        log, counts = add_action_aliases(self.source, self.output, "Fly", ["idle"])
        self.assertEqual(counts, {"ERROR": 1})
        self.assertIn("Ambiguous fallback", log.read_text())
        second_output = self.root / "exact"
        _, counts = add_action_aliases(self.source, second_output, "Fly", ["Idle"])
        self.assertEqual(counts, {"ADDED": 1})
        alias = validate_source(second_output / "FrameData.xml").getroot().find("Anims")[-1]
        self.assertEqual(alias.findtext("CopyOf"), "Idle")

    def test_dialog_runs_worker_and_exposes_log(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtCore, QtWidgets
        from gui.batchadd import ActionAliasDialog

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        make_single(self.source)
        dialog = ActionAliasDialog(str(self.source))
        self.addCleanup(dialog.close)
        dialog.outputEdit.setText(str(self.output))
        dialog.actionEdit.setText("Fly")
        dialog.fallbackEdit.setPlainText("Hover\nIdle")
        dialog.apply()
        self.assertFalse(dialog.applyButton.isEnabled())
        dialog.reject()
        loop = QtCore.QEventLoop()
        dialog.worker.finished.connect(loop.quit)
        QtCore.QTimer.singleShot(10000, loop.quit)
        loop.exec()
        app.processEvents()
        dialog.wait_for_worker()
        app.processEvents()
        self.assertIsNone(dialog.worker)
        self.assertTrue(dialog.logButton.isEnabled(), dialog.statusLabel.text())
        self.assertTrue(Path(dialog.log_path).is_file())
        self.assertIn("added: 1", dialog.statusLabel.text())
        self.assertTrue(dialog.applyButton.isEnabled())


if __name__ == "__main__":
    unittest.main()
