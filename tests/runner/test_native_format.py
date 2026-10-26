"""Portable native JSON persistence and XML interoperability regression tests."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from conversion import SpriteDocument
from data import ActionPoints, AnimationSequence, AnimFrame, AnimGroup, Offset, SheetFormat
from formats.native import DURATION, FLIP, FRAME_INDEX, document_data, read_texture_layout, write_packed_document
from formats.packing import pack_texture, unpack_texture
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
        self.root = Path(temporary.name).resolve()
        self.document = make_document()

    def save(self, document=None, name="Sprite.json"):
        return (document or self.document).export(self.root / "native", SheetFormat.NATIVE, name)

    def save_packed(self, document=None):
        path = self.root / "packed" / "Sprite.json"
        path.parent.mkdir(exist_ok=True)
        write_packed_document(document or self.document, path)
        return path

    def assert_document_equal(self, before, after):
        self.assertEqual(document_data(before, "Sprite.png"), document_data(after, "Sprite.png"))
        self.assertEqual(before.sheet_image.tobytes(), after.sheet_image.tobytes())

    def test_native_roundtrip_preserves_pixels_metadata_and_shared_directions(self):
        self.document.sheet_image.putpixel((0, 7), (12, 34, 56, 0))
        target = self.save(name="Chosen.json")
        self.assertEqual({p.name for p in target.parent.iterdir()}, {"Chosen.json", "Chosen.png"})
        loaded = SpriteDocument.load(target)
        self.assert_document_equal(self.document, loaded)
        self.assertTrue(loaded.groups[0].is_single_direction)
        self.assertEqual(loaded.sheet_format, SheetFormat.NATIVE)
        self.assertIsNone(loaded.action_pt_image)
        self.assertEqual(loaded.texture_name, "Chosen.png")
        with Image.open(target.with_suffix(".png")) as image:
            self.assertEqual(image.size, self.document.sheet_image.size)
            self.assertEqual(image.convert("RGBA").tobytes(), self.document.sheet_image.tobytes())
        self.assertEqual(json.loads(target.read_bytes())["regions"], [[0, 0, 6, 8, 0, 0], [6, 0, 6, 8, 0, 0]])
        # Repeated saves are deterministic and ignore XML compression settings.
        original = target.read_bytes()
        loaded.trim_copies = loaded.collapse_singles = False
        self.save(loaded, "Chosen.json")
        self.assertEqual(target.read_bytes(), original)

    def test_blank_multi_cells_preserve_timing_and_roundtrip_without_markers(self):
        xml = self.document.export(self.root / "blank-source", SheetFormat.MULTI)
        tree = ET.parse(xml)
        anim = tree.find("Anims/Anim")
        name = anim.findtext("Name")
        w, h = int(anim.findtext("FrameWidth")), int(anim.findtext("FrameHeight"))
        box = (w, 0, 2 * w, h)
        for suffix in ("Anim", "Offsets", "Shadow"):
            path = xml.with_name(f"{name}-{suffix}.png")
            with Image.open(path) as source:
                image = source.convert("RGBA")
            image.paste((0, 0, 0, 0), box)
            image.save(path)
        source_bytes = {p.name: p.read_bytes() for p in xml.parent.iterdir()}

        def check_blank(document):
            group = document.groups[0]
            self.assertEqual([f.duration for f in group.directions[0].frames], [3, 5])
            self.assertEqual((group.rush_frame, group.hit_frame, group.return_frame), (0, 1, 1))
            frame = group.directions[0].frames[1]
            self.assertIsNone(document.image_grid[frame.frame_index].getbbox())
            self.assertNotIn(frame.frame_index, document.action_points)

        loaded = SpriteDocument.load(xml)
        check_blank(loaded)
        for number, format_name in enumerate((
            SheetFormat.NATIVE, SheetFormat.SINGLE, SheetFormat.MULTI, SheetFormat.MULTI,
        )):
            loaded = SpriteDocument.load(loaded.export(self.root / f"blank-{number}", format_name))
            check_blank(loaded)
        self.assertEqual(source_bytes, {p.name: p.read_bytes() for p in xml.parent.iterdir()})

        # A partially populated triplet must still fail.
        for suffix, color in (("Anim", (20, 30, 40, 255)),
                              ("Offsets", (255, 0, 0, 255)),
                              ("Shadow", (255, 255, 255, 255))):
            path = xml.with_name(f"{name}-{suffix}.png")
            with Image.open(path) as source:
                image = source.convert("RGBA")
            image.putpixel((w, 0), color)
            image.save(path)
            with self.assertRaisesRegex(FormatError, "missing or ambiguous attachment markers"):
                SpriteDocument.load(xml)
            path.write_bytes(source_bytes[path.name])

    def test_xml_ignores_cutscene_idle_in_both_formats(self):
        for format_name in (SheetFormat.SINGLE, SheetFormat.MULTI):
            with self.subTest(format=format_name):
                xml = self.document.export(self.root / format_name.name, format_name)
                baseline = SpriteDocument.load(xml)
                tree = ET.parse(xml)
                tree.getroot().insert(0, ET.Element("CutsceneIdle"))
                tree.write(xml, encoding="utf-8", xml_declaration=True)
                source = xml.read_bytes()

                loaded = SpriteDocument.load(xml)
                self.assert_document_equal(baseline, loaded)
                self.assertEqual(xml.read_bytes(), source)
                exported = loaded.export(self.root / (format_name.name + "-ignored"), format_name)
                self.assertIsNone(ET.parse(exported).getroot().find("CutsceneIdle"))

                # Ignoring this known runtime flag must not allow arbitrary tags.
                tree.getroot().append(ET.Element("UnknownFlag"))
                tree.write(xml, encoding="utf-8", xml_declaration=True)
                with self.assertRaisesRegex(FormatError, "Unsupported AnimData/UnknownFlag"):
                    SpriteDocument.load(xml)

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
        self.assertEqual([job.source.resolve() for job in jobs], [native.resolve()])
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
            (lambda d: d.update(version=99), "version"),
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
            (lambda d: d.update(grid_size=[0, 1]), "integer"),
            (lambda d: d["regions"].pop(), "per logical cell"),
            (lambda d: d["regions"][0].__setitem__(0, 999), "outside"),
            (lambda d: d["regions"][0].__setitem__(2, 0), "positive"),
            (lambda d: d["regions"][0].__setitem__(4, 999), "logical frame"),
            (lambda d: d["regions"][0].__setitem__(0, True), "integer"),
            (lambda d: d["regions"][0].append(1), "six"),
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
        self.assertEqual(data["version"], 3)
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

    def test_packed_regions_preserve_world_pixels_for_even_and_odd_flipped_frames(self):
        for width, height in ((6, 8), (7, 9)):
            image = Image.new("RGBA", (width * 2, height * 2))
            image.putpixel((1, 2), (70, 90, 120, 128))
            image.putpixel((3, 4), (40, 80, 10, 255))
            image.putpixel((width + 2, height + 1), (120, 40, 10, 1))
            document = SpriteDocument(image, width, height, groups=[AnimGroup(-1, "Idle", directions=[
                AnimationSequence([AnimFrame(frame_index=0, duration=3, flip=flip,
                    sprite_offset=Offset(-3, 5)) for flip in (0, 1)])
            ])], action_points={2: ActionPoints(Offset(-8, 12), Offset(), Offset(), Offset())})
            target = self.save_packed(document)
            data = json.loads(target.read_bytes())
            with Image.open(target.with_suffix(".png")) as source:
                packed = source.convert("RGBA")
            self.assertLess(packed.width * packed.height, image.width * image.height)
            self.assertEqual(data["grid_size"], [2, 2])
            self.assertEqual(data["regions"][1:3], [None, None])
            self.assertIsNotNone(data["regions"][3])  # Unreferenced artwork survives.
            self.assert_document_equal(document, SpriteDocument.load(target))
            x, y, w, h, tx, ty = data["regions"][0]
            crop = packed.crop((x, y, x + w, y + h))
            for flip in (False, True):
                expected = {}
                original = document.image_grid[0]
                for py in range(height):
                    for px in range(width):
                        color = original.getpixel((px, py))
                        if color[3]:
                            wx = (width - 1 - px if flip else px) - width // 2 - 3
                            expected[wx, py - height // 2 + 5] = color
                actual = {}
                left = (width - tx - w if flip else tx) - width // 2 - 3
                for py in range(h):
                    for px in range(w):
                        color = crop.getpixel((w - 1 - px if flip else px, py))
                        if color[3]:
                            actual[left + px, ty + py - height // 2 + 5] = color
                self.assertEqual(actual, expected)

    def test_completely_empty_atlas_and_missing_points_keep_logical_cells(self):
        self.document.sheet_image = Image.new("RGBA", self.document.sheet_image.size)
        self.document.action_points.clear()
        target = self.save()
        data = json.loads(target.read_bytes())
        self.assertEqual(data["regions"], [[0, 0, 6, 8, 0, 0], [6, 0, 6, 8, 0, 0]])
        with Image.open(target.with_suffix(".png")) as image:
            self.assertEqual(image.size, (12, 8))
        self.assert_document_equal(self.document, SpriteDocument.load(target))
        packed_target = self.save_packed()
        self.assertEqual(json.loads(packed_target.read_bytes())["regions"], [None, None])
        with Image.open(packed_target.with_suffix(".png")) as image:
            self.assertEqual(image.size, (1, 1))
        self.assert_document_equal(self.document, SpriteDocument.load(packed_target))

    def test_packed_input_saves_as_editable_grid_and_accepts_new_artwork_in_margins(self):
        packed = SpriteDocument.load(self.save_packed())
        target = self.save(packed)
        with Image.open(target.with_suffix(".png")) as source:
            image = source.convert("RGBA")
        self.assertEqual(image.size, (12, 8))
        image.putpixel((0, 7), (20, 60, 100, 128))
        image.save(target.with_suffix(".png"))
        loaded = SpriteDocument.load(target)
        self.assertEqual(loaded.image_grid[0].getpixel((0, 7)), (20, 60, 100, 128))
        self.assertEqual(loaded.groups, packed.groups)
        self.assertEqual(loaded.action_points, packed.action_points)

    def test_each_crop_has_a_transparent_padding_including_fallback_and_texture_edges(self):
        image = Image.new("RGBA", (12, 10))
        for index in range(6):
            left, top = index % 3 * 4, index // 3 * 5
            for y in range(top, top + 5 - index % 3):
                for x in range(left, left + 4 - index % 2):
                    image.putpixel((x, y), (40 + index, 80, 120, 128))
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                if fallback:
                    with patch("formats.packing._place", return_value=None):
                        packed, layout = pack_texture(image, (4, 5))
                else:
                    packed, layout = pack_texture(image, (4, 5))
                self.assertEqual(unpack_texture(packed, layout).tobytes(), image.tobytes())
                for region in layout.regions:
                    x, y, w, h, tx, ty = region
                    self.assertGreaterEqual(x, 1)
                    self.assertGreaterEqual(y, 1)
                    self.assertLess(x + w, packed.width)
                    self.assertLess(y + h, packed.height)
                    for px in range(x - 1, x + w + 1):
                        self.assertEqual(packed.getpixel((px, y - 1)), (0, 0, 0, 0))
                        self.assertEqual(packed.getpixel((px, y + h)), (0, 0, 0, 0))
                    for py in range(y - 1, y + h + 1):
                        self.assertEqual(packed.getpixel((x - 1, py)), (0, 0, 0, 0))
                        self.assertEqual(packed.getpixel((x + w, py)), (0, 0, 0, 0))

    def test_packed_artwork_reload_uses_saved_regions_and_rejects_layout_changes(self):
        target = self.save_packed()
        layout = read_texture_layout(target)
        self.assertIsNotNone(layout)
        with Image.open(target.with_suffix(".png")) as source:
            packed = source.convert("RGBA")
        x, y, w, h, tx, ty = layout.regions[0]
        packed.putpixel((x, y), (90, 70, 40, 128))
        restored = unpack_texture(packed, layout)
        self.assertEqual(restored.getpixel((tx, ty)), (90, 70, 40, 128))
        self.assertEqual(restored.crop((6, 0, 12, 8)).tobytes(), self.document.image_grid[1].tobytes())
        with self.assertRaisesRegex(FormatError, "Reopen"):
            unpack_texture(Image.new("RGBA", (packed.width + 1, packed.height)), layout)

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
