"""Portable project profile, transformation and transactional build checks."""

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
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from conversion import SpriteDocument
from data import ActionPoints, AnimationSequence, AnimFrame, AnimGroup, Offset, SheetFormat
from project import apply_profile, build_sprite, load_profile, palette_data, plan_builds, profile_from_data, load_rig_profile
from utils import FormatError


def settings(**sections):
    return profile_from_data({"format": "mdfe-project", "version": 1, **sections})


def make_document():
    image = Image.new("RGBA", (12, 8))
    image.putpixel((1, 1), (90, 30, 10, 255))
    image.putpixel((3, 4), (40, 80, 20, 128))
    image.putpixel((7, 2), (90, 30, 10, 255))
    sequence = AnimationSequence([
        AnimFrame(0, 0, 0, 4, Offset(3, -3), Offset(9, -9)),
        AnimFrame(1, 1, 1, 6, Offset(-3, 3), Offset(15, -15)),
    ])
    walk = AnimGroup(1, "Walk", 0, 1, 1, [sequence])
    idle = copy.deepcopy(walk)
    idle.name, idle.idx = "Idle", 0
    return SpriteDocument(image, 6, 8, groups=[walk, idle])


class ProjectBuildTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mdfe-project-")
        self.addCleanup(temporary.cleanup)
        # Windows TEMP can use an 8.3 alias while production resolves full paths.
        self.root = Path(temporary.name).resolve()
        self.document = make_document()
        self.source = self.document.export(self.root / "source", SheetFormat.NATIVE)

    def test_defaults_and_invalid_profile_fields(self):
        default = settings()
        self.assertEqual(default.packing.padding, 1)
        self.assertTrue(default.palette.enabled)
        self.assertFalse(default.animations.rush.enabled)
        invalid = [
            {"version": True}, {"version": 2}, {"extra": 1},
            {"packing": {"padding": 0}}, {"packing": {"padding": True}},
            {"packing": {"power_of_two": 1}}, {"packing": {"guttr": 2}},
            {"palette": {"enabled": "yes"}},
            {"animations": {"rush": {"divisor": 0}}},
            {"animations": {"fallbacks": {"Run": []}}},
            {"animations": {"custom": {"Stop": {"source": "Walk", "frames": []}}}},
            {"animations": {"custom": {"Stop": {"source": "Walk", "duration": 0}}}},
            {"animations": {"custom": {"Stop": {"frames": [0]}}}},
            {"animations": {"fallbacks": {"Stop": ["Walk"]}, "custom": {"Stop": {"source": "Walk"}}}},
        ]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(FormatError):
                settings(**change)
        profile = self.root / "profile.json"
        profile.write_text('{"format":"mdfe-project","version":1,"version":1}', encoding="utf-8")
        with self.assertRaisesRegex(FormatError, "Duplicate"):
            load_profile(profile)

    def test_fallbacks_preserve_existing_and_resolve_dependencies_without_aliasing(self):
        profile = settings(animations={"fallbacks": {
            "Idle": ["Missing"], "Run": ["Missing", "Walk", "Idle"], "Sprint": ["Run"]}})
        derived = apply_profile(self.document, profile)
        groups = {g.name: g for g in derived.groups}
        self.assertEqual(groups["Idle"].idx, 0)
        self.assertEqual(groups["Run"].idx, -1)
        self.assertEqual(groups["Run"].directions, groups["Walk"].directions)
        groups["Run"].directions[0].frames[0].duration = 100
        self.assertEqual(groups["Walk"].directions[0].frames[0].duration, 4)
        self.assertEqual(groups["Sprint"].directions[0].frames[0].duration, 4)
        self.assertEqual(self.document.groups[0].directions[0].frames[0].duration, 4)

    def test_yaml_combines_build_settings_and_named_rigs_without_changing_sprites(self):
        path = self.root / "project.yaml"
        path.write_text("""# Shared project conventions and build settings
format: mdfe-project
version: 1
rigs:
  humanoid:
    name: Humanoid
    action_points: [head, back]
    suggested_actions: [Idle, Walk]
  equipment:
    name: Equipment
    action_points: [handle]
packing:
  padding: 3
palette:
  enabled: false
""", encoding="utf-8-sig")
        profile = load_profile(path)
        self.assertEqual(profile.packing.padding, 3)
        self.assertFalse(profile.palette.enabled)
        self.assertEqual(load_rig_profile(path, "equipment").action_points, ("handle",))
        with self.assertRaisesRegex(FormatError, "Choose a named rig"):
            load_rig_profile(path)
        with self.assertRaisesRegex(FormatError, "missing"):
            load_rig_profile(path, "removed")
        before = copy.deepcopy(self.document.action_points)
        self.document.rig_profile = path
        self.document.rig_profile_id = "humanoid"
        source = self.document.export(self.root / "named-source", SheetFormat.NATIVE)
        saved = json.loads(source.read_text())
        self.assertEqual(saved["rig_profile"], "../project.yaml")
        self.assertEqual(saved["rig_profile_id"], "humanoid")
        target = build_sprite(source, profile, self.root / "named-output")
        loaded = SpriteDocument.load(target)
        self.assertEqual(loaded.rig_profile, path)
        self.assertEqual(loaded.rig_profile_id, "humanoid")
        self.assertEqual(loaded.action_points, before)
        self.assertFalse(target.with_name("Palette.json").exists())
        relocated = loaded.export(self.root / "nested" / "copy", SheetFormat.NATIVE)
        self.assertEqual(json.loads(relocated.read_text())["rig_profile"], "../../project.yaml")
        path.unlink()
        # Rig conventions are optional even when the saved selection is missing.
        loaded = SpriteDocument.load(relocated)
        self.assertEqual(loaded.rig_profile_id, "humanoid")
        loaded.export(self.root / "without-profile", SheetFormat.NATIVE)

    def test_yaml_rejects_duplicate_keys_unsafe_tags_and_invalid_rigs(self):
        path = self.root / "invalid.yaml"
        header = "format: mdfe-project\nversion: 1\n"
        for content in (
            "", "[]", "!!python/object/apply:os.system ['never-execute']",
            header + "version: 1\n",
            header + "palette: {enabled: true, enabled: false}\n",
            header + "rigs: {a: {name: A, action_points: [], name: B}}\n",
            header + "rigs: {a: {name: A, action_points: []}, a: {name: B, action_points: []}}\n",
            header + "rigs: []\n",
            header + "rigs: {a: {name: A, action_points: [head, head]}}\n",
            header + "rigs: {a: {name: A, action_points: [], coordinates: []}}\n",
            header + "rigs: {a: {name: A, action_points: [], id: other}}\n",
            header + "rigs: {'bad id': {name: A, action_points: []}}\n",
            header + "rigs: {a: {action_points: []}}\n",
            header + "palette: {enabled: 1}\n",
        ):
            with self.subTest(content=content), self.assertRaises(FormatError):
                path.write_text(content, encoding="utf-8")
                load_profile(path)

    def test_shipped_yaml_profile_is_valid(self):
        profile = load_profile(PROJECT_ROOT / "project.example.yaml")
        self.assertEqual(set(profile.rigs), {"monster"})
        self.assertIn("Stop", profile.animations.custom)

    def test_custom_selection_alias_events_and_shared_direction_identity(self):
        profile = settings(animations={"custom": {
            "Alias": {"source": "Stop"},
            "Stop": {"source": "Walk", "frames": [1, 0, 1], "duration": 9, "index": 8},
            "Idle": {"source": "Idle", "frames": [0]},
        }})
        derived = apply_profile(self.document, profile)
        groups = {g.name: g for g in derived.groups}
        stop = groups["Stop"]
        self.assertTrue(stop.is_single_direction)
        self.assertEqual([f.frame_index for f in stop.directions[0].frames], [1, 0, 1])
        self.assertEqual([f.duration for f in stop.directions[0].frames], [9, 9, 9])
        self.assertEqual((stop.rush_frame, stop.hit_frame, stop.return_frame), (1, 0, 0))
        self.assertEqual(groups["Alias"].directions, stop.directions)
        self.assertEqual(groups["Alias"].idx, -1)
        self.assertEqual(groups["Idle"].idx, 0)
        self.assertEqual(groups["Idle"].hit_frame, -1)
        stop.directions[0].frames[0].duration = 20
        self.assertEqual(stop.directions[0].frames[2].duration, 9)
        self.assertEqual(groups["Alias"].directions[0].frames[0].duration, 9)

    def test_eight_independent_directions_remain_independent(self):
        self.document.groups[0].directions = [copy.deepcopy(self.document.groups[0].directions[0]) for _ in range(8)]
        derived = apply_profile(self.document, settings(animations={"custom": {"Stop": {"source": "Walk", "frames": [0]}}}))
        stop = derived.groups[-1]
        self.assertFalse(stop.is_single_direction)
        stop.directions[0].frames[0].duration = 12
        self.assertEqual(stop.directions[1].frames[0].duration, 4)

    def test_rush_matches_old_converter_and_processes_shared_sequences_once(self):
        before = copy.deepcopy(self.document.groups)
        derived = apply_profile(self.document, settings(animations={"rush": {"enabled": True, "actions": ["Walk"]}}))
        first, second = derived.groups[0].directions[0].frames
        self.assertEqual(first.sprite_offset, Offset(9, -9))
        self.assertEqual(second.sprite_offset, Offset(11, -11))
        self.assertEqual(second.shadow_offset, Offset(1, -1))
        self.assertEqual(derived.groups[1].directions[0].frames[1].sprite_offset, Offset(15, -15))
        self.assertEqual(self.document.groups, before)
        self.assertEqual([f.duration for f in derived.groups[0].directions[0].frames], [4, 6])

    def test_invalid_rules_fail_without_output(self):
        cases = [
            {"fallbacks": {"Run": ["Missing"]}},
            {"fallbacks": {"A": ["B"], "B": ["A"]}},
            {"custom": {"A": {"source": "B"}, "B": {"source": "A"}}},
            {"custom": {"Stop": {"source": "Missing"}}},
            {"custom": {"Stop": {"source": "Walk", "frames": [2]}}},
            {"custom": {"Stop": {"source": "Walk", "index": 1}}},
            {"rush": {"enabled": True, "actions": ["Missing"]}},
        ]
        for rules in cases:
            with self.subTest(rules=rules), self.assertRaises(FormatError):
                build_sprite(self.source, settings(animations=rules), self.root / "invalid-output")
            self.assertFalse((self.root / "invalid-output").exists())

    def test_build_packing_palette_source_isolation_and_rebuild(self):
        before = {p.name: p.read_bytes() for p in self.source.parent.iterdir()}
        profile = settings(packing={"padding": 3, "power_of_two": True})
        target = build_sprite(self.source, profile, self.root / "output")
        self.assertEqual({p.name for p in target.parent.iterdir()}, {"Sprite.json", "Sprite.png", "Palette.json"})
        data = json.loads(target.read_bytes())
        with Image.open(target.with_suffix(".png")) as image:
            self.assertEqual(image.width & (image.width - 1), 0)
            self.assertEqual(image.height & (image.height - 1), 0)
            for region in data["regions"]:
                x, y, w, h, tx, ty = region
                for px in range(x - 3, x + w + 3):
                    for py in range(y - 3, y + h + 3):
                        if not (x <= px < x+w and y <= py < y+h):
                            self.assertEqual(image.getpixel((px, py)), (0, 0, 0, 0))
        palette = json.loads(target.with_name("Palette.json").read_bytes())
        self.assertEqual(palette["colors"], [[40, 80, 20, 128], [90, 30, 10, 255]])
        self.assertEqual(palette["counts"], [1, 2])
        self.assertEqual(SpriteDocument.load(target).groups, self.document.groups)
        with self.assertRaisesRegex(FormatError, "overwrite"):
            build_sprite(self.source, profile, target.parent)
        target.with_name("Build.json").write_text('{"format":"mdframeedit-build","version":1}', encoding="utf-8")
        build_sprite(self.source, settings(palette={"enabled": False}), target.parent, overwrite=True)
        self.assertFalse(target.with_name("Palette.json").exists())
        self.assertFalse(target.with_name("Build.json").exists())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.source.parent.iterdir()})

    def test_palette_counts_logical_pixels_including_optional_transparency(self):
        palette = palette_data(self.document, True)
        self.assertEqual(sum(palette["counts"]), 96)
        self.assertEqual(palette["colors"][0], [0, 0, 0, 0])
        self.assertEqual(palette["counts"][0], 93)

    def test_commit_failure_restores_entire_previous_build(self):
        target = build_sprite(self.source, settings(), self.root / "output")
        before = {p.name: p.read_bytes() for p in target.parent.iterdir()}
        replace = os.replace

        def fail_metadata(source, destination):
            if Path(destination) == target and "backup" not in Path(source).parts:
                raise OSError("Metadata locked")
            replace(source, destination)

        with patch("conversion.os.replace", side_effect=fail_metadata), self.assertRaisesRegex(OSError, "locked"):
            build_sprite(self.source, settings(packing={"padding": 2}, palette={"enabled": False}),
                         target.parent, overwrite=True)
        self.assertEqual(before, {p.name: p.read_bytes() for p in target.parent.iterdir()})

    def test_output_cannot_overlap_source_folder(self):
        for destination in (self.source.parent, self.source.parent / "nested", self.root):
            with self.subTest(destination=destination), self.assertRaisesRegex(FormatError, "separate"):
                build_sprite(self.source, settings(), destination, overwrite=True)

    def test_cli_reads_profile_again_each_build_and_accepts_xml_and_native(self):
        profile = self.root / "project.yaml"
        xml = self.document.export(self.root / "xml", SheetFormat.SINGLE)
        output = self.root / "cli-output"
        for enabled, source in ((True, xml), (False, self.source.parent)):
            profile.write_text(yaml.safe_dump({"format": "mdfe-project", "version": 1,
                "palette": {"enabled": enabled}}), encoding="utf-8")
            result = subprocess.run([sys.executable, str(PROJECT_ROOT / "cli.py"), "build", str(source),
                "--profile", str(profile), "-o", str(output), "--overwrite"],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((output / "Palette.json").exists(), enabled)
        profile.write_text('{"format":"bad"}', encoding="utf-8")
        result = subprocess.run([sys.executable, str(PROJECT_ROOT / "cli.py"), "build", str(self.source),
            "--profile", str(profile), "-o", str(output), "--overwrite"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("profile", result.stderr)

    def run_batch(self, source, output, overwrite=False):
        profile = self.root / "batch-profile.yaml"
        profile.write_text('format: mdfe-project\nversion: 1\n', encoding="utf-8")
        return subprocess.run([sys.executable, str(PROJECT_ROOT / "cli.py"), "build", str(source),
            "--profile", str(profile), "-o", str(output), *(["--overwrite"] if overwrite else [])],
            capture_output=True, text=True, timeout=30)

    def test_batch_preserves_root_and_nested_folders_and_prefers_native(self):
        source, output = self.root / "batch-source", self.root / "batch-output"
        self.document.export(source, SheetFormat.SINGLE)
        self.document.groups[0].directions[0].frames[0].duration = 17
        native = self.document.export(source, SheetFormat.NATIVE)
        multi = copy.deepcopy(self.document)
        multi.action_points = {i: ActionPoints(Offset(-2, 1), Offset(), Offset(2, 1), Offset(0, -2))
                               for i in range(len(multi.image_grid))}
        nested = multi.export(source / "0001" / "variant", SheetFormat.MULTI)
        before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
        jobs = plan_builds(source, output)
        self.assertEqual([(job.source, job.destination) for job in jobs],
                         [(native, output), (nested, output / "0001" / "variant")])
        result = self.run_batch(source, output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Built 2; failed 0.", result.stdout)
        for path in (output / "Sprite.json", output / "0001" / "variant" / "Sprite.json"):
            self.assertEqual(SpriteDocument.load(path).groups[0].directions[0].frames[0].duration, 17)
            self.assertTrue(path.with_suffix(".png").exists())
            self.assertTrue(path.with_name("Palette.json").exists())
        self.assertEqual(before, {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()})

    def test_batch_failure_continues_other_sprites_and_returns_failure(self):
        source, output = self.root / "batch-source", self.root / "batch-output"
        for folder in ("first", "broken", "last"):
            self.document.export(source / folder, SheetFormat.NATIVE)
        (source / "broken" / "Sprite.json").write_text('{"format":"invalid","version":3}', encoding="utf-8")
        result = self.run_batch(source, output)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Built 2; failed 1.", result.stdout)
        self.assertIn("broken", result.stderr)
        self.assertFalse((output / "broken").exists())
        for folder in ("first", "last"):
            self.assertEqual(SpriteDocument.load(output / folder / "Sprite.json").groups, self.document.groups)

    def test_batch_existing_parent_does_not_prevent_building_new_child(self):
        source, output = self.root / "batch-source", self.root / "batch-output"
        self.document.export(source, SheetFormat.NATIVE)
        self.assertEqual(self.run_batch(source, output).returncode, 0)
        before = {p.name: p.read_bytes() for p in output.iterdir()}
        self.document.export(source / "child", SheetFormat.NATIVE)
        result = self.run_batch(source, output)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Built 1; failed 1.", result.stdout)
        self.assertIn("overwrite", result.stderr)
        self.assertTrue((output / "child" / "Sprite.json").exists())
        self.assertEqual(before, {name: (output / name).read_bytes() for name in before})
        self.assertEqual(self.run_batch(source, output, overwrite=True).returncode, 0)

    def test_batch_planning_rejects_overlapping_trees_and_empty_sources(self):
        for output in (self.source.parent, self.source.parent / "output", self.root):
            with self.subTest(output=output), self.assertRaisesRegex(FormatError, "separate"):
                plan_builds(self.source.parent, output)
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(FormatError, "No Sprite.json"):
            plan_builds(empty, self.root / "output")
        jobs = plan_builds(self.source, self.root / "output")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].destination, self.root / "output")


if __name__ == "__main__":
    unittest.main(verbosity=2)
