"""Portable runtime export, mapped frame access, corruption and CLI checks."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from cli import ConversionError, convert_job, create_parser, plan_conversions
from conversion import SpriteDocument
from data import ActionPoints, AnimationSequence, AnimFrame, AnimGroup, Offset, SheetFormat
from formats.runtime import CELL, ENTRY, FRAME, HEADER, MAGIC, POINT, VERSION, RuntimeSprite
from utils import FormatError


def make_document(width=6):
    image = Image.new("RGBA", (width * 3, 8))
    image.putpixel((1, 2), (80, 20, 90, 128))
    image.putpixel((width + 2, 5), (10, 40, 90, 255))
    sequence = AnimationSequence([
        AnimFrame(0, 0, 1, 3, Offset(-1, 2), Offset(2, -1)),
        AnimFrame(1, 1, 0, 5, Offset(2, -1), Offset(-1, 2)),
        AnimFrame(2, 0, 1, 3, Offset(-1, 2), Offset(2, -1)),
    ])
    group = AnimGroup(7, "Walk", 0, 1, 2, [sequence])
    alias = copy.deepcopy(group)
    alias.name, alias.idx, alias.copy_name = "Alias", -1, "Walk"
    return SpriteDocument(image, width, 8, groups=[group, alias], action_points={
        cell: ActionPoints(Offset(-1, 0), Offset(0, 0), Offset(1, 0), Offset(0, -1))
        for cell in range(2)
    }, shadow_size=2)


def unpack_container(path):
    raw = path.read_bytes()
    magic, version, length = HEADER.unpack_from(raw)
    index = json.loads(raw[HEADER.size:HEADER.size + length])
    offset = (HEADER.size + length + 3) & ~3
    return magic, version, index, bytearray(raw[offset:])


def replace_container(path, index, binary):
    index["data_sha256"] = hashlib.sha256(binary).hexdigest()
    raw = json.dumps(index, separators=(",", ":")).encode()
    path.write_bytes(HEADER.pack(MAGIC, VERSION, len(raw)) + raw
                     + bytes(-(HEADER.size + len(raw)) % 4) + binary)


class RuntimeFormatTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mdframeedit-runtime-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.document = make_document()

    def export(self, document=None, filename="Sprite.mdruntime"):
        return (document or self.document).export(self.root / "runtime", SheetFormat.RUNTIME, filename)

    def test_independent_binary_layout_and_deterministic_packed_texture(self):
        before = self.document.sheet_image.tobytes()
        target = self.export(filename="Chosen.mdruntime")
        self.assertEqual({p.name for p in target.parent.iterdir()}, {"Chosen.mdruntime", "Chosen.png"})
        original = target.read_bytes()
        magic, version, index, binary = unpack_container(target)
        self.assertEqual((magic, version), (MAGIC, 1))
        self.assertEqual(index["texture"], "Chosen.png")
        self.assertEqual(index["tables"]["frames"][1], 2)
        sequence_offset, count = index["animations"]["Walk"]["directions"][0]
        self.assertEqual(count, 3)
        entries = [struct.unpack_from("<2i", binary, sequence_offset + i * 8) for i in range(count)]
        self.assertEqual(entries, [(0, 3), (1, 8), (0, 11)])
        frame_offset = index["tables"]["frames"][0]
        self.assertEqual(struct.unpack_from("<7i", binary, frame_offset), (0, 3, 1, 2, -1, -1, 2))
        with Image.open(target.with_suffix(".png")) as image:
            image.load()
            for cell_id in range(2):
                cell_offset = index["tables"]["cells"][0] + cell_id * 32
                x, y, w, h, tx, ty, *_ = struct.unpack_from("<8i", binary, cell_offset)
                crop = image.crop((x, y, x + w, y + h))
                expected = self.document.image_grid[cell_id].crop((tx, ty, tx + w, ty + h))
                self.assertEqual(crop.tobytes(), expected.tobytes())
        self.export(filename="Chosen.mdruntime")
        self.assertEqual(original, target.read_bytes())
        self.assertEqual(before, self.document.sheet_image.tobytes())

    def test_metadata_access_without_graphics_or_frame_expansion(self):
        target = self.export()
        target.with_suffix(".png").unlink()
        with patch("PIL.Image.open", side_effect=AssertionError("No PNG access")), \
             patch("formats.native._load_animations", side_effect=AssertionError("No editor objects")):
            with RuntimeSprite(target) as sprite:
                with patch.object(sprite, "_row", wraps=sprite._row) as reads:
                    frame = sprite.frame("Walk", 7, 1)
                    self.assertEqual(frame, (1, 5, 0, -1, 2, 2, -1))
                    self.assertEqual(reads.call_count, 2)
                self.assertEqual(sprite.frame_at_tick("Walk", 0, 0)[0], 0)
                self.assertEqual(sprite.frame_at_tick("Walk", 0, 3)[0], 1)
                self.assertEqual(sprite.frame_at_tick("Walk", 0, 8)[0], 0)
                self.assertEqual(sprite.frame_at_tick("Walk", 0, 11)[0], 0)
                self.assertEqual(sprite.frame_at_tick("Walk", 0, -1)[0], 0)
                self.assertEqual(sprite.frame_at_tick("Walk", 0, 7), frame)
        self.assertTrue(sprite.closed)
        with self.assertRaises(RuntimeError):
            sprite.frame("Walk", 0, 0)
        sprite.close()

    def test_named_sparse_points_null_cells_and_odd_even_mirroring(self):
        for width in (5, 6):
            document = make_document(width)
            document.action_point_names = ("declared_unset",)
            document.action_points = {0: ActionPoints.from_named({"head": Offset(1, -9)}),
                                      1: ActionPoints.from_named({})}
            with RuntimeSprite(self.export(document)) as sprite:
                self.assertEqual(sprite.points(0), {"head": (1, -9)})
                self.assertEqual(sprite.points(0, mirrored=True), {"head": (-1 if width == 5 else -2, -9)})
                self.assertEqual(sprite.points(1), {})
                self.assertIsNone(sprite.points(2))
                self.assertEqual(sprite.cell(2)[:6], (0, 0, 0, 0, 0, 0))
                self.assertIn("declared_unset", sprite.index["point_names"])

    def test_eight_directions_empty_sequences_events_and_copy_contents(self):
        group = self.document.groups[0]
        group.directions = [copy.deepcopy(group.directions[0]) for _ in range(8)]
        group.directions[1].frames = []
        group.directions[2].frames[0].sprite_offset.x = 12
        with RuntimeSprite(self.export()) as sprite:
            walk = sprite.index["animations"]["Walk"]
            self.assertEqual(len(walk["directions"]), 8)
            self.assertEqual(walk["index"], 7)
            self.assertEqual((walk["rush_frame"], walk["hit_frame"], walk["return_frame"]), (0, 1, 2))
            self.assertEqual(walk["directions"][0], walk["directions"][3])
            self.assertEqual(sprite.sequence("Walk", 1)[1], 0)
            self.assertEqual(sprite.frame("Walk", 2, 0)[3], 12)
            self.assertEqual(sprite.frame("Alias", 2, 0)[3], 2)
            for operation in (lambda: sprite.frame("Walk", 1, 0),
                              lambda: sprite.frame_at_tick("Walk", 1, 0),
                              lambda: sprite.frame("Walk", -1, 0),
                              lambda: sprite.frame("Walk", 8, 0),
                              lambda: sprite.frame("Walk", 0, -1)):
                with self.assertRaises(IndexError):
                    operation()

    def test_unanimated_atlas_and_absent_shadow_size(self):
        self.document.groups = []
        self.document.shadow_size_present = False
        self.document.loaded_shadow_size = self.document.shadow_size
        with RuntimeSprite(self.export()) as sprite:
            self.assertEqual(sprite.index["animations"], {})
            self.assertEqual(sprite.index["tables"]["frames"][1], 0)
            self.assertIsNone(sprite.index["shadow_size"])

    def test_numeric_and_cumulative_overflow_do_not_replace_output(self):
        target = self.export()
        before = {p.name: p.read_bytes() for p in target.parent.iterdir()}
        for value, duration in ((2**31, False), (2**30, True)):
            document = copy.deepcopy(self.document)
            for frame in document.groups[0].directions[0].frames:
                if duration:
                    frame.duration = value
                else:
                    frame.sprite_offset.x = value
            with self.assertRaisesRegex(FormatError, "32-bit"):
                self.export(document)
            self.assertEqual(before, {p.name: p.read_bytes() for p in target.parent.iterdir()})

    def test_truncated_headers_versions_and_checksum_reject_and_release_file(self):
        target = self.export()
        valid = target.read_bytes()
        damaged = bytearray(valid)
        damaged[-1] ^= 1
        cases = [b"", valid[:10], b"BADMAGIC" + valid[8:],
                 HEADER.pack(MAGIC, 99, 0), HEADER.pack(MAGIC, 1, len(valid) * 2),
                 valid[:-1], bytes(damaged)]
        for raw in cases:
            with self.subTest(raw_length=len(raw)):
                target.write_bytes(raw)
                with self.assertRaises(FormatError):
                    RuntimeSprite(target)
                # Windows will reject this replacement if the mapping leaked.
                target.unlink()
                target.write_bytes(valid)

    def test_invalid_numeric_references_spans_and_timing_reject(self):
        target = self.export()
        _, _, original, binary = unpack_container(target)

        def mutate_record(table, record, values):
            result = bytearray(binary)
            record.pack_into(result, original["tables"][table][0], *values)
            return result

        bad_index = copy.deepcopy(original)
        bad_index["animations"]["Walk"]["directions"][0] = [len(binary), 1]
        bad_texture = copy.deepcopy(original)
        bad_texture["texture"] = "../outside.png"
        frame = list(FRAME.unpack_from(binary, original["tables"]["frames"][0]))
        frame[0] = 999
        point = list(POINT.unpack_from(binary, original["tables"]["points"][0]))
        point[0] = 999
        cell = list(CELL.unpack_from(binary, original["tables"]["cells"][0]))
        cell[-1] = 999
        cases = [(bad_index, binary), (bad_texture, binary),
                 (original, mutate_record("frames", FRAME, frame)),
                 (original, mutate_record("sequence_entries", ENTRY, (999, 3))),
                 (original, mutate_record("sequence_entries", ENTRY, (0, 99))),
                 (original, mutate_record("points", POINT, point)),
                 (original, mutate_record("cells", CELL, cell))]
        for index, payload in cases:
            replace_container(target, copy.deepcopy(index), payload)
            with self.assertRaises(FormatError):
                RuntimeSprite(target)

    def test_export_only_and_transaction_rollback(self):
        target = self.export()
        with self.assertRaisesRegex(FormatError, "export-only"):
            SpriteDocument.load(target)
        before = {p.name: p.read_bytes() for p in target.parent.iterdir()}
        self.document.sheet_image.putpixel((1, 2), (90, 80, 10, 255))
        replace = os.replace

        def reject_metadata(source, destination):
            if Path(destination) == target:
                raise OSError("Container is locked")
            replace(source, destination)

        with patch("conversion.os.replace", side_effect=reject_metadata):
            with self.assertRaisesRegex(OSError, "locked"):
                self.export()
        self.assertEqual(before, {p.name: p.read_bytes() for p in target.parent.iterdir()})

    def run_cli(self, source, destination, *extra):
        command = [sys.executable, "-B", str(PROJECT_ROOT / "main.py"), "convert", str(source),
                   "--to", "runtime", "-o", str(destination), "--executor", "serial", *extra]
        result = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        source_frame = SpriteDocument.load(source).groups[0].directions[0].frames[0]
        with RuntimeSprite(destination / "Sprite.mdruntime") as sprite:
            self.assertEqual(sprite.frame("Walk", 0, 0), (
                source_frame.frame_index, source_frame.duration, int(source_frame.flip),
                source_frame.sprite_offset.x, source_frame.sprite_offset.y,
                source_frame.shadow_offset.x, source_frame.shadow_offset.y,
            ))

    def test_cli_native_single_multi_and_process_executor(self):
        for format_name in (SheetFormat.NATIVE, SheetFormat.SINGLE, SheetFormat.MULTI):
            source = self.document.export(self.root / format_name.name, format_name)
            self.run_cli(source, self.root / (format_name.name + "-runtime"))
        source = self.root / "NATIVE" / "Sprite.json"
        self.run_cli(source, self.root / "process", "--executor", "process", "--jobs", "1")

    def test_cli_recursive_source_preference_and_overwrite(self):
        source = self.root / "sources" / "nested"
        native = self.document.export(source, SheetFormat.NATIVE)
        self.document.export(source, SheetFormat.SINGLE)
        output = self.root / "batch"
        args = create_parser().parse_args([str(source.parent), "--to", "runtime", "-r", "-o", str(output)])
        jobs = plan_conversions(args)
        self.assertEqual([job.source for job in jobs], [native])
        from cli import main
        self.assertEqual(main([str(source.parent), "--to", "runtime", "-r", "-o", str(output),
                               "--executor", "serial"]), 0)
        target = output / "nested" / "Sprite.mdruntime"
        self.assertTrue(target.is_file())
        self.assertEqual(main([str(source.parent), "--to", "runtime", "-r", "-o", str(output),
                               "--executor", "serial"]), 1)
        self.assertEqual(main([str(source.parent), "--to", "runtime", "-r", "-o", str(output),
                               "--executor", "serial", "--overwrite"]), 0)

    def plan_layout(self, source, output, layout, *extra):
        args = create_parser().parse_args([str(source), "--to", "runtime", "-r", "-o", str(output),
                                           "--runtime-layout", layout, *extra])
        return plan_conversions(args)

    def test_flat_cli_serial_and_process_share_one_output_folder(self):
        source = self.root / "library"
        for relative in ("0001", "0001/0000/0001", "0003/0000/0001/0002"):
            self.document.export(source / relative, SheetFormat.NATIVE)
        before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
        expected = {f"{stem}{extension}" for stem in ("0001", "0001-0000-0001", "0003-0000-0001-0002")
                    for extension in (".mdruntime", ".png")}
        snapshots = []
        for executor in ("serial", "process"):
            output = self.root / executor
            command = [sys.executable, "-B", str(PROJECT_ROOT / "main.py"), "convert", str(source),
                       "--to", "runtime", "-r", "-o", str(output), "--runtime-layout", "flat",
                       "--executor", executor, "--jobs", "2"]
            result = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual({p.name for p in output.iterdir()}, expected)
            for path in output.glob("*.mdruntime"):
                self.assertIn(str(path), result.stdout)
                with RuntimeSprite(path) as sprite:
                    self.assertEqual(sprite.index["texture"], path.with_suffix(".png").name)
                    self.assertEqual(sprite.frame("Walk", 0, 0)[1], 3)
            snapshots.append({p.name: p.read_bytes() for p in output.iterdir()})
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(before, {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()})

    def test_spritecollab_names_pad_defaults_and_keep_species_when_selected(self):
        source = self.root / "sprite"
        for relative in ("0003", "0003/0000/0001", "0003/0000/0001/0002", "0006/0001"):
            self.document.export(source / relative, SheetFormat.NATIVE)
        output = self.root / "padded"
        jobs = self.plan_layout(source, output, "spritecollab")
        names = {"0003-0000-0000-0000.mdruntime", "0003-0000-0001-0000.mdruntime",
                 "0003-0000-0001-0002.mdruntime", "0006-0001-0000-0000.mdruntime"}
        self.assertEqual({job.filename for job in jobs}, names)
        for job in jobs:
            self.assertEqual(convert_job(job).error, "")
        self.assertEqual({p.name for p in output.glob("*.mdruntime")}, names)
        selected = self.plan_layout(source / "0003", self.root / "selected", "spritecollab")
        self.assertEqual({job.filename for job in selected}, {name for name in names if name.startswith("0003-")})
        single = self.plan_layout(source / "0003" / "Sprite.json", self.root / "single", "spritecollab")
        self.assertEqual(single[0].filename, "0003-0000-0000-0000.mdruntime")
        variant = source / "0003/0000/0001/0002"
        for selected_input in (variant, variant / "Sprite.json"):
            selected = self.plan_layout(selected_input, self.root / "variant", "spritecollab")
            self.assertEqual(selected[0].filename, "0003-0000-0001-0002.mdruntime")

    def test_flat_root_bundle_uses_folder_name_and_preserves_unrelated_files(self):
        source = self.root / "custom-resource"
        self.document.export(source, SheetFormat.NATIVE)
        output = self.root / "flat"
        output.mkdir()
        unrelated = output / "keep.txt"
        unrelated.write_text("untouched", encoding="utf-8")
        job = self.plan_layout(source, output, "flat")[0]
        self.assertEqual(job.filename, "custom-resource.mdruntime")
        self.assertEqual(convert_job(job).error, "")
        before = {p.name: p.read_bytes() for p in output.iterdir()}
        self.assertIn("--overwrite", convert_job(job).error)
        self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})
        overwrite = self.plan_layout(source, output, "flat", "--overwrite")[0]
        self.assertEqual(convert_job(overwrite).error, "")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "untouched")

    def test_flat_collision_rejected_before_output_is_created(self):
        source = self.root / "library"
        for relative in ("a/b", "a-b"):
            self.document.export(source / relative, SheetFormat.NATIVE)
        output = self.root / "flat"
        with self.assertRaisesRegex(ConversionError, "Multiple sprites"):
            self.plan_layout(source, output, "flat")
        self.assertFalse(output.exists())

    def test_spritecollab_padding_collision_rejected_before_writing(self):
        source = self.root / "sprite"
        for relative in ("0003", "0003/0000"):
            self.document.export(source / relative, SheetFormat.NATIVE)
        output = self.root / "padded"
        with self.assertRaisesRegex(ConversionError, "Multiple sprites"):
            self.plan_layout(source, output, "spritecollab")
        self.assertFalse(output.exists())

    def test_runtime_layout_rejects_wrong_format_or_invalid_collab_path(self):
        source = self.document.export(self.root / "named-resource", SheetFormat.NATIVE)
        args = create_parser().parse_args([str(source), "--to", "single", "-o", str(self.root / "out"),
                                           "--runtime-layout", "flat"])
        with self.assertRaisesRegex(ConversionError, "requires --to runtime"):
            plan_conversions(args)
        with self.assertRaisesRegex(ConversionError, "four-digit"):
            self.plan_layout(source, self.root / "out", "spritecollab")
        deep_root = self.root / "sprite"
        self.document.export(deep_root / "0003/0000/0001/0002/0000", SheetFormat.NATIVE)
        with self.assertRaisesRegex(ConversionError, "four-digit"):
            self.plan_layout(deep_root, self.root / "out", "spritecollab")

    def test_flat_named_bundle_rollback_does_not_touch_other_bundles(self):
        source = self.root / "library"
        for name in ("one", "two"):
            self.document.export(source / name, SheetFormat.NATIVE)
        output = self.root / "flat"
        for job in self.plan_layout(source, output, "flat"):
            self.assertEqual(convert_job(job).error, "")
        before = {p.name: p.read_bytes() for p in output.iterdir()}
        self.document.sheet_image.putpixel((1, 2), (255, 0, 0, 255))
        self.document.export(source / "one", SheetFormat.NATIVE)
        job = self.plan_layout(source, output, "flat", "--overwrite")[0]
        replace = os.replace

        def reject_metadata(staged, destination):
            if Path(destination).name == "one.mdruntime":
                raise OSError("Named container is locked")
            replace(staged, destination)

        with patch("conversion.os.replace", side_effect=reject_metadata):
            self.assertIn("locked", convert_job(job).error)
        self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})


if __name__ == "__main__":
    unittest.main(verbosity=2)
