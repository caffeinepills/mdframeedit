"""Portable native JSON persistence and XML interoperability regression tests."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from conversion import SpriteDocument
from data import ActionPoints, AnimationSequence, AnimFrame, AnimGroup, Offset, SheetFormat
from formats.native import DURATION, FLIP, FRAME_INDEX, document_data
from utils import FormatError


def make_document():
    image = Image.new("RGBA", (12, 8))
    for x, y, color in ((1, 1, (30, 60, 90, 128)), (4, 6, (80, 120, 40, 255)),
                         (7, 2, (90, 20, 40, 255)), (10, 5, (10, 50, 100, 255))):
        image.putpixel((x, y), color)
    sequence = AnimationSequence([AnimFrame(0, 0, 1, 3, Offset(-1, 2), Offset(2, -1)),
                                  AnimFrame(1, 1, 0, 5, Offset(2, -1), Offset(-1, 2))])
    group = AnimGroup(7, "Walk", 0, 1, 1, [sequence])
    alias = copy.deepcopy(group)
    alias.name, alias.idx, alias.copy_name = "Alias", -1, "Walk"
    return SpriteDocument(image, 6, 8, groups=[group, alias], action_points={
        i: ActionPoints(Offset(-1, 0), Offset(0, 0), Offset(1, 0), Offset(0, -1))
        for i in range(2)
    }, shadow_size=2)


class NativeFormatTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mdframeedit-native-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.document = make_document()

    def save(self, document=None, name="Sprite.json"):
        return (document or self.document).export(self.root / "native", SheetFormat.NATIVE, name)

    def assert_document_equal(self, before, after):
        self.assertEqual(document_data(before, "Sprite.png"), document_data(after, "Sprite.png"))
        self.assertEqual(before.sheet_image.tobytes(), after.sheet_image.tobytes())

    def test_native_roundtrip_preserves_pixels_metadata_and_shared_directions(self):
        target = self.save(name="Chosen.json")
        self.assertEqual({p.name for p in target.parent.iterdir()}, {"Chosen.json", "Chosen.png"})
        loaded = SpriteDocument.load(target)
        self.assert_document_equal(self.document, loaded)
        self.assertTrue(loaded.groups[0].is_single_direction)
        self.assertEqual(loaded.sheet_format, SheetFormat.NATIVE)
        self.assertIsNone(loaded.action_pt_image)
        self.assertEqual(loaded.texture_name, "Chosen.png")
        # Repeated saves are deterministic and ignore XML compression settings.
        original = target.read_bytes()
        loaded.trim_copies = loaded.collapse_singles = False
        self.save(loaded, "Chosen.json")
        self.assertEqual(target.read_bytes(), original)

    def test_native_xml_native_roundtrip_in_both_export_formats(self):
        native = SpriteDocument.load(self.save())
        for format_name in (SheetFormat.SINGLE, SheetFormat.MULTI):
            with self.subTest(format=format_name):
                xml = native.export(self.root / format_name.name, format_name)
                loaded = SpriteDocument.load(xml)
                # XML can repack artwork and rename sheet indices; compare its
                # normalized export after a native save/load to catch placement,
                # marker and timing changes independently of native serialization.
                json_path = loaded.export(self.root / (format_name.name + "-json"), SheetFormat.NATIVE)
                reopened = SpriteDocument.load(json_path)
                self.assert_document_equal(loaded, reopened)
                output = reopened.export(self.root / (format_name.name + "-again"), format_name)
                for path in xml.parent.iterdir():
                    self.assertEqual(path.read_bytes(), (output.parent / path.name).read_bytes())

    def test_empty_cells_missing_points_and_independent_directions_keep_indices(self):
        self.document.action_points.pop(0)
        directions = [copy.deepcopy(self.document.groups[0].directions[0]) for _ in range(8)]
        directions[3].frames.clear()
        directions[4].frames[0].duration = 9
        self.document.groups[0].directions = directions
        loaded = SpriteDocument.load(self.save())
        self.assert_document_equal(self.document, loaded)
        self.assertEqual(set(loaded.action_points), {1})
        self.assertFalse(loaded.groups[0].is_single_direction)
        self.assertFalse(loaded.groups[0].directions[3].frames)

    def test_native_can_save_points_xml_cannot_represent(self):
        self.document.action_points[0].head = Offset(100, -200)
        loaded = SpriteDocument.load(self.save())
        self.assertEqual(loaded.action_points[0].head, Offset(100, -200))
        with self.assertRaisesRegex(FormatError, "outside"):
            loaded.export(self.root / "xml", SheetFormat.SINGLE)
        self.document.action_points[0].head = self.document.action_points[0].left_hand
        self.assert_document_equal(self.document, SpriteDocument.load(self.save()))
        with self.assertRaisesRegex(FormatError, "overlaps"):
            self.document.export(self.root / "xml", SheetFormat.SINGLE)

    def test_missing_shadow_and_events_remain_omitted(self):
        self.document.shadow_size = self.document.loaded_shadow_size = 1
        self.document.shadow_size_present = False
        for group in self.document.groups:
            group.rush_frame = group.hit_frame = group.return_frame = -1
        loaded = SpriteDocument.load(self.save())
        self.assert_document_equal(self.document, loaded)
        xml = loaded.export(self.root / "xml", SheetFormat.SINGLE).read_text()
        self.assertNotIn("ShadowSize", xml)
        self.assertNotIn("RushFrame", xml)

    def test_edited_copy_is_never_replaced_with_source_data(self):
        self.document.groups[1].directions[0].frames[0].sprite_offset.x += 5
        self.assert_document_equal(self.document, SpriteDocument.load(self.save()))

    def test_odd_cells_preserve_exact_action_point_coordinates(self):
        self.document = SpriteDocument(
            Image.new("RGBA", (5, 7), (10, 20, 30, 128)), 5, 7,
            groups=[AnimGroup(-1, "Custom/Action", directions=[AnimationSequence([
                AnimFrame(frame_index=0, flip=1, duration=3)
            ])])],
            action_points={0: ActionPoints(Offset(-2, -3), Offset(0, 0), Offset(2, 3), Offset(1, -1))},
        )
        loaded = SpriteDocument.load(self.save())
        self.assert_document_equal(self.document, loaded)
        self.assertEqual(loaded.action_points[0].left_hand, Offset(-2, -3))

    def test_folder_cli_prefers_native_and_palette_reads_json(self):
        source = self.document.export(self.root / "mixed", SheetFormat.SINGLE)
        native = self.document.export(source.parent, SheetFormat.NATIVE)
        from cli import convert_job, create_parser, palette_main, plan_conversions
        args = create_parser().parse_args([str(source.parent), "--to", "multi", "-o", str(self.root / "output")])
        jobs = plan_conversions(args)
        self.assertEqual([job.source for job in jobs], [native])
        self.assertFalse(convert_job(jobs[0]).error)
        with patch("builtins.print"):
            self.assertEqual(palette_main([str(native)]), 0)
        self.assertEqual(len(plan_conversions(create_parser().parse_args([
            str(source.parent), "--to", "native", "-o", str(self.root / "another")
        ]))), 1)

    def test_invalid_json_rejected_with_clear_errors(self):
        target = self.save()
        original = json.loads(target.read_text())
        mutations = [
            (lambda d: d.update(version=3), "version"),
            (lambda d: d.update(texture="../outside.png"), "filename"),
            (lambda d: d.update(extra=True), "unknown"),
            (lambda d: d.update(frame_size=[True, 8]), "integer"),
            (lambda d: d["action_points"].pop(), "per atlas cell"),
            (lambda d: d["frames"][0].__setitem__(FRAME_INDEX, 2), "outside"),
            (lambda d: d["frames"][0].__setitem__(DURATION, 0), "integer"),
            (lambda d: d["frames"][0].__setitem__(FLIP, 1), "boolean"),
            (lambda d: d["animations"]["Walk"].update(copy_of="Alias"), "cyclic"),
            (lambda d: d["sequences"][0].__setitem__(0, 999), "outside"),
            (lambda d: d["sequences"][0].__setitem__(0, True), "integer"),
            (lambda d: d["animations"]["Walk"]["directions"].__setitem__(0, 999), "outside"),
            (lambda d: d["frames"][0].pop(), "seven"),
            (lambda d: d["frames"].append([0, 1, False, 0, 0, 0, 0]), "unreferenced"),
            (lambda d: d["animations"]["Walk"].update(hit_frame=2), "outside"),
        ]
        for mutate, message in mutations:
            with self.subTest(message=message):
                data = copy.deepcopy(original)
                mutate(data)
                target.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(FormatError, message):
                    SpriteDocument.load(target)
        target.write_text('{"version":1,"version":1}', encoding="utf-8")
        with self.assertRaisesRegex(FormatError, "Duplicate"):
            SpriteDocument.load(target)

    def test_engine_uses_parsed_tables_without_resolving_copies_or_defaults(self):
        data = json.loads(self.save().read_bytes())
        self.assertEqual(data["version"], 2)
        self.assertEqual(list(data["animations"]), ["Walk", "Alias"])
        directions = data["animations"]["Walk"]["directions"]
        alias_directions = data["animations"]["Alias"]["directions"]
        self.assertEqual(directions, alias_directions)
        self.assertEqual(len(data["sequences"]), 1)
        self.assertEqual(len(data["frames"]), 2)
        sequence = data["sequences"][directions[0]]
        self.assertEqual(data["frames"][sequence[0]], [0, 3, True, 2, -1, -1, 2])
        self.assertEqual(data["frames"][sequence[1]], [1, 5, False, -1, 2, 2, -1])
        self.assertEqual(data["animations"]["Walk"]["hit_frame"], 1)

    def test_shared_storage_retains_independent_editor_frames_and_timeline_positions(self):
        sequence = self.document.groups[0].directions[0]
        sequence.frames.append(copy.deepcopy(sequence.frames[0]))
        self.document.groups[0].directions = [copy.deepcopy(sequence) for _ in range(8)]
        loaded = SpriteDocument.load(self.save())
        group = loaded.groups[0]
        self.assertFalse(group.is_single_direction)
        self.assertEqual(len(group.directions[0].frames), 3)
        first = group.directions[0].frames[0]
        first.duration = 17
        first.sprite_offset.x = 80
        self.assertEqual(group.directions[0].frames[2].duration, 3)
        self.assertEqual(group.directions[1].frames[0].duration, 3)
        self.assertEqual(loaded.groups[1].directions[0].frames[0].duration, 3)
        self.assertEqual(group.directions[0].frames[2].sprite_offset.x, 2)
        self.assertEqual(group.directions[0].frames[2].idx, 2)
        self.assert_document_equal(loaded, SpriteDocument.load(self.save(loaded)))

    def test_version_one_reads_and_upgrades_only_when_saved(self):
        target = self.save()
        legacy = {
            "format": "mdframeedit", "version": 1, "texture": "Sprite.png",
            "frame_size": [6, 8], "shadow_size": None,
            "action_points": [None, None],
            "animations": [{"name": "Legacy", "directions": [[
                {"frame": 1, "duration": 5, "flip": True,
                 "sprite_offset": [-3, 2], "shadow_offset": [4, -1]},
            ]]}],
        }
        target.write_text(json.dumps(legacy), encoding="utf-8")
        before = target.read_bytes()
        loaded = SpriteDocument.load(target)
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(loaded.groups[0].is_single_direction)
        self.assertEqual(loaded.groups[0].directions[0].frames[0].sprite_offset, Offset(-3, 2))
        self.assertFalse(loaded.shadow_size_present)
        self.save(loaded)
        self.assertEqual(json.loads(target.read_bytes())["version"], 2)
        self.assert_document_equal(loaded, SpriteDocument.load(target))
        legacy["animations"][0]["directions"][0][0]["duration"] = False
        target.write_text(json.dumps(legacy), encoding="utf-8")
        with self.assertRaisesRegex(FormatError, "integer"):
            SpriteDocument.load(target)

    def test_failed_save_rolls_back_json_and_texture(self):
        target = self.save()
        before = {p.name: p.read_bytes() for p in target.parent.iterdir()}
        self.document.sheet_image.putpixel((0, 0), (100, 0, 0, 255))
        replace = os.replace

        def fail_json(source, destination):
            if Path(source).name == "Sprite.json" and "backup" not in Path(source).parts:
                raise OSError("simulated write failure")
            return replace(source, destination)

        with patch("conversion.os.replace", side_effect=fail_json):
            with self.assertRaisesRegex(OSError, "simulated"):
                self.save()
        self.assertEqual(before, {p.name: p.read_bytes() for p in target.parent.iterdir()})

    def test_cli_import_and_xml_exports(self):
        source = self.document.export(self.root / "source", SheetFormat.SINGLE)
        for target_format in ("native", "single", "multi"):
            output = self.root / ("cli-" + target_format)
            result = subprocess.run([sys.executable, str(PROJECT_ROOT / "cli.py"), str(source),
                                     "--to", target_format, "-o", str(output), "--executor", "serial"],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            source = output / {"native": "Sprite.json", "single": "FrameData.xml", "multi": "AnimData.xml"}[target_format]
            self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
