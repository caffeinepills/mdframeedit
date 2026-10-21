"""Portable checks for the adapter boundary and custom bundle transactions."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from conversion import SpriteDocument
from data import AnimationSequence, AnimFrame, AnimGroup, SheetFormat
from formats import NativeFormat, SpriteFormat, get_adapter, validate_export
from formats.base import DocumentT
from formats.native import write_document
from utils import FormatError


class ManifestFormat(SpriteFormat):
    """Example foreign metadata format reusing native payloads as companions."""

    format_id = "example_manifest"
    default_filename = "Sprite.example"

    def read(self, path: Path, document_class: type[DocumentT]) -> DocumentT:
        if path.read_text(encoding="utf-8") != "example-v1\npayload.json\n":
            raise FormatError("Invalid example manifest.")
        return NativeFormat().read(path.with_name("payload.json"), document_class)

    def validate_export(self, document: SpriteDocument) -> None:
        NativeFormat().validate_export(document)

    def write(self, document: SpriteDocument, directory: Path, filename: str) -> None:
        self.validate_export(document)
        NativeFormat().write(document, directory, "payload.json")
        (directory / filename).write_text("example-v1\npayload.json\n", encoding="utf-8")


class FormatAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mdframeedit-adapters-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.document = SpriteDocument(
            Image.new("RGBA", (5, 7), (40, 80, 120, 128)), 5, 7,
            groups=[AnimGroup(-1, "Custom/Action", directions=[AnimationSequence([
                AnimFrame(frame_index=0, duration=4)
            ])])],
        )

    def test_native_adapter_preserves_exact_optimized_bytes(self) -> None:
        direct = self.root / "direct"
        direct.mkdir()
        write_document(self.document, direct / "Sprite.json")
        target = self.document.export(self.root / "adapted", NativeFormat())
        for path in direct.iterdir():
            self.assertEqual(path.read_bytes(), target.with_name(path.name).read_bytes())
        data = json.loads(target.read_bytes())
        self.assertEqual(data["version"], 3)
        self.assertEqual(data["frames"], [[0, 4, False, 0, 0, 0, 0]])
        self.assertEqual(data["sequences"], [[0]])

    def test_custom_adapter_requires_no_enum_or_registry_change(self) -> None:
        adapter = ManifestFormat()
        target = self.document.export(self.root / "custom", adapter)
        self.assertEqual(target.name, "Sprite.example")
        self.assertIs(get_adapter(adapter), adapter)
        loaded = SpriteDocument.load(target, adapter)
        self.assertEqual(loaded.sheet_format, "example_manifest")
        self.assertEqual(loaded.groups, self.document.groups)
        self.assertEqual(loaded.sheet_image.tobytes(), self.document.sheet_image.tobytes())
        validate_export(loaded, adapter)
        native = loaded.export(self.root / "native", SheetFormat.NATIVE)
        self.assertEqual(SpriteDocument.load(native).groups, self.document.groups)

    def test_custom_adapter_preserves_document_subclass_and_filename_keywords(self) -> None:
        class DerivedDocument(SpriteDocument):
            pass

        adapter = ManifestFormat()
        target = self.document.export(self.root / "custom", adapter, filename="Chosen.example")
        self.assertIsInstance(DerivedDocument.load(target, adapter), DerivedDocument)
        legacy = self.document.export(self.root / "legacy", adapter, xml_name="Legacy.example")
        self.assertEqual(legacy.name, "Legacy.example")
        with self.assertRaisesRegex(FormatError, "not both"):
            self.document.export(self.root / "conflict", adapter, filename="One.example", xml_name="Two.example")

    def test_metadata_is_committed_last_for_a_custom_extension(self) -> None:
        directory = self.root / "custom"
        committed: list[str] = []
        replace = os.replace

        def record_replace(source: str | Path, target: str | Path) -> None:
            if Path(target).parent == directory:
                committed.append(Path(target).name)
            replace(source, target)

        with patch("conversion.os.replace", side_effect=record_replace):
            self.document.export(directory, ManifestFormat())
        self.assertEqual(committed[-1], "Sprite.example")
        self.assertEqual(set(committed), {"Sprite.example", "payload.json", "payload.png"})

    def test_custom_validation_failure_does_not_replace_existing_bundle(self) -> None:
        class InvalidOutputFormat(ManifestFormat):
            def validate_written(self, directory: Path, filename: str, document_class: type[DocumentT]) -> None:
                raise FormatError("Invalid output")

        target = self.document.export(self.root / "custom", ManifestFormat())
        before = {path.name: path.read_bytes() for path in target.parent.iterdir()}
        with self.assertRaisesRegex(FormatError, "Invalid output"):
            self.document.export(target.parent, InvalidOutputFormat())
        self.assertEqual(before, {path.name: path.read_bytes() for path in target.parent.iterdir()})

    def test_custom_write_failure_rolls_back_all_companions(self) -> None:
        adapter = ManifestFormat()
        target = self.document.export(self.root / "custom", adapter)
        before = {path.name: path.read_bytes() for path in target.parent.iterdir()}
        self.document.sheet_image.putpixel((0, 0), (80, 20, 10, 255))
        replace = os.replace

        def reject_metadata(source: str | Path, destination: str | Path) -> None:
            if Path(destination) == target:
                raise OSError("Metadata is locked")
            replace(source, destination)

        with patch("conversion.os.replace", side_effect=reject_metadata):
            with self.assertRaisesRegex(OSError, "locked"):
                self.document.export(target.parent, adapter)
        self.assertEqual(before, {path.name: path.read_bytes() for path in target.parent.iterdir()})

    def test_adapter_cannot_schedule_removal_outside_the_bundle(self) -> None:
        class UnsafeRemovalFormat(ManifestFormat):
            def obsolete_files(self, directory: Path) -> tuple[str, ...]:
                return ("../outside.png",)

        with self.assertRaisesRegex(FormatError, "Invalid obsolete"):
            self.document.export(self.root / "custom", UnsafeRemovalFormat())


if __name__ == "__main__":
    unittest.main(verbosity=2)
