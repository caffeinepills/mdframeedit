"""Portable checks for exact, size-aware PNG encoding and save verification."""
from __future__ import annotations

import random
import subprocess
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conversion import SpriteDocument
from data import ActionPoints, AnimFrame, Offset, SheetFormat
from utils import FormatError, png_save_options, save_png


def palette_image(count):
    rng = random.Random(123)
    # Includes identical RGB with distinct alpha and distinct invisible RGB.
    colors = [(10, 20, 30, 0), (40, 50, 60, 0), (10, 20, 30, 128), (10, 20, 30, 255)]
    colors.extend((i, 255 - i, (i * 71) % 256, (i * 31) % 256) for i in range(count - 4))
    colors = colors[:count]
    image = Image.new("RGBA", (128, 128))
    image.putdata([colors[i % count] for i in range(count)] +
                  [rng.choice(colors) for _ in range(128 * 128 - count)])
    return image


class PngSavingTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mdframeedit-png-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def check_pixels(self, path, image):
        with Image.open(path) as saved:
            self.assertEqual(saved.size, image.size)
            self.assertEqual(saved.convert("RGBA").tobytes(), image.convert("RGBA").tobytes())

    def test_verification_and_optimization_switches_are_independent(self):
        image = palette_image(16)
        original_save = Image.Image.save
        for verify in (True, False):
            for optimize in (True, False):
                with self.subTest(verify=verify, optimize=optimize):
                    options = []

                    def record(source, output, *args, **kwargs):
                        options.append(kwargs["optimize"])
                        return original_save(source, output, *args, **kwargs)

                    target = self.root / "switches.png"
                    with patch("utils.Image.open", wraps=Image.open) as decode, patch.object(Image.Image, "save", record):
                        save_png(image, target, verify=verify, optimize=optimize)
                    self.assertEqual(decode.call_count, (2 if optimize else 1) if verify else 0)
                    self.assertTrue(options)
                    self.assertEqual(set(options), {optimize})
                    self.check_pixels(target, image)
                    with Image.open(target) as saved:
                        self.assertEqual(saved.mode, "P" if optimize else "RGBA")

    def test_scoped_settings_restore_after_failure_and_nested_overrides(self):
        image = palette_image(16)
        target = self.root / "scoped.png"
        with self.assertRaisesRegex(RuntimeError, "stop"):
            with png_save_options(verify=False, optimize=False):
                with patch("utils.Image.open", side_effect=AssertionError("unexpected decode")):
                    save_png(image, target)
                with png_save_options():
                    save_png(image, target)
                with Image.open(target) as saved:
                    self.assertEqual(saved.mode, "P")
                raise RuntimeError("stop")
        with patch("utils.Image.open", wraps=Image.open) as decode:
            save_png(image, target)
        self.assertEqual(decode.call_count, 2)

    def test_cli_flags_reach_serial_and_spawned_workers(self):
        from cli import create_parser, plan_conversions

        image = palette_image(16)
        document = SpriteDocument.from_image(image, 128, 128)
        for sequence in document.groups[0].directions:
            sequence.frames.append(AnimFrame(frame_index=0, duration=1))
        source = document.export(self.root / "source", SheetFormat.NATIVE)
        for executor in ("serial", "process"):
            for verify in (True, False):
                for optimize in (True, False):
                    with self.subTest(executor=executor, verify=verify, optimize=optimize):
                        output = self.root / f"{executor}-{verify}-{optimize}"
                        args = [str(source), "--to", "single", "-o", str(output), "--executor", executor]
                        if not verify:
                            args.append("--no-png-verification")
                        if not optimize:
                            args.append("--no-png-optimization")
                        job = plan_conversions(create_parser().parse_args(args))[0]
                        self.assertEqual((job.verify_png, job.optimize_png), (verify, optimize))
                        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] / "cli.py"), *args],
                                                capture_output=True, text=True, timeout=60)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.check_pixels(output / "Anim.png", image)
                        with Image.open(output / "Anim.png") as saved:
                            self.assertEqual(saved.mode, "P" if optimize else "RGBA")

    def test_build_cli_passes_png_options_to_packed_texture(self):
        from cli import build_main

        image = palette_image(16)
        document = SpriteDocument.from_image(image, 128, 128)
        source = document.export(self.root / "source", SheetFormat.NATIVE)
        profile = self.root / "profile.yaml"
        profile.write_text("format: mdfe-project\nversion: 1\n", encoding="utf-8")
        output = self.root / "build"
        # Source loading still decodes; only the encoder round-trip is skipped.
        from formats.native import save_png as real_save
        calls = []

        def record(image, path):
            with patch("utils.Image.open", side_effect=AssertionError("unexpected encoder decode")):
                real_save(image, path)
            calls.append(path.name)

        with patch("formats.native.save_png", side_effect=record):
            status = build_main([str(source), "--profile", str(profile), "-o", str(output),
                                 "--no-png-verification", "--no-png-optimization"])
        self.assertEqual(status, 0)
        self.assertEqual(calls, ["Sprite.png"])
        with Image.open(output / "Sprite.png") as saved:
            self.assertEqual(saved.mode, "RGBA")

    def test_palette_depths_preserve_all_rgba_bytes_and_reduce_size(self):
        for count, depth in ((2, 1), (4, 2), (16, 4), (256, 8)):
            with self.subTest(colors=count):
                image = palette_image(count)
                before = image.tobytes()
                target = self.root / "test.png"
                save_png(image, target)
                self.check_pixels(target, image)
                self.assertEqual(image.mode, "RGBA")
                self.assertEqual(image.tobytes(), before)
                regular = BytesIO()
                image.save(regular, format="PNG", optimize=True)
                self.assertLess(target.stat().st_size, len(regular.getvalue()))
                header = target.read_bytes()
                self.assertEqual((header[24], header[25]), (depth, 3))
                first = header
                save_png(image, target)
                self.assertEqual(target.read_bytes(), first)

    def test_more_than_256_rgba_colors_keep_truecolor(self):
        image = palette_image(256)
        image.putpixel((0, 0), (1, 2, 3, 4))
        self.assertIsNone(image.getcolors(256))
        target = self.root / "many.png"
        save_png(image, target)
        self.check_pixels(target, image)
        with Image.open(target) as saved:
            self.assertEqual(saved.mode, "RGBA")

    def test_palette_overhead_keeps_smaller_regular_png(self):
        image = Image.new("RGBA", (1, 1), (12, 34, 56, 78))
        target = self.root / "tiny.png"
        save_png(image, target)
        self.check_pixels(target, image)
        with Image.open(target) as saved:
            self.assertEqual(saved.mode, "RGBA")

    def test_rgb_and_indexed_inputs_preserve_decoded_pixels(self):
        images = [palette_image(16).convert("RGB"), palette_image(16).convert("P"),
                  Image.new("RGBA", (128, 128), (12, 34, 56, 255)),
                  Image.new("RGBA", (128, 128), (12, 34, 56, 0))]
        for image in images:
            with self.subTest(mode=image.mode):
                target = self.root / "input.png"
                save_png(image, target)
                self.check_pixels(target, image)

    def test_multi_export_verifies_artwork_offsets_and_shadows(self):
        document = SpriteDocument.from_image(palette_image(16), 128, 128)
        for sequence in document.groups[0].directions:
            sequence.frames.append(AnimFrame(frame_index=0, duration=1))
        document.action_points[0] = ActionPoints(
            Offset(-1, 0), Offset(0, 0), Offset(1, 0), Offset(0, -1))
        written = {}

        def record(image, path):
            written[path.name] = image.copy()
            save_png(image, path)

        with patch("conversion.save_png", side_effect=record):
            target = document.export(self.root / "multi", SheetFormat.MULTI)
        self.assertEqual(set(written), {"Default-Anim.png", "Default-Offsets.png", "Default-Shadow.png"})
        for name, image in written.items():
            self.check_pixels(target.with_name(name), image)

    def test_corrupted_palette_candidate_falls_back_to_verified_rgba(self):
        image = palette_image(16)
        original_save = Image.Image.save

        def corrupt_palette(source, output, *args, **kwargs):
            if source.mode == "P":
                source = source.copy()
                source.putpixel((0, 0), (source.getpixel((0, 0)) + 1) % 16)
            return original_save(source, output, *args, **kwargs)

        target = self.root / "fallback.png"
        with patch.object(Image.Image, "save", corrupt_palette):
            save_png(image, target)
        self.check_pixels(target, image)
        with Image.open(target) as saved:
            self.assertEqual(saved.mode, "RGBA")

    def test_verification_failure_preserves_existing_bundle(self):
        image = palette_image(16)
        document = SpriteDocument.from_image(image, 16, 16)
        target = document.export(self.root / "native", SheetFormat.NATIVE)
        before = {path.name: path.read_bytes() for path in target.parent.iterdir()}
        original_save = Image.Image.save

        def corrupt(source, output, *args, **kwargs):
            source = source.copy()
            source.putpixel((0, 0), (1, 2, 3, 4))
            return original_save(source, output, *args, **kwargs)

        with patch.object(Image.Image, "save", corrupt), self.assertRaisesRegex(FormatError, "PNG verification"):
            document.export(target.parent, SheetFormat.NATIVE)
        self.assertEqual({path.name: path.read_bytes() for path in target.parent.iterdir()}, before)

    def test_native_and_single_exports_use_shared_png_writer(self):
        image = palette_image(16)
        document = SpriteDocument.from_image(image, 16, 16)
        for format_name in (SheetFormat.NATIVE, SheetFormat.SINGLE):
            with self.subTest(format=format_name):
                target = document.export(self.root / format_name.name, format_name)
                png = target.with_suffix(".png") if format_name == SheetFormat.NATIVE else target.with_name("Anim.png")
                self.check_pixels(png, image)
                with Image.open(png) as saved:
                    self.assertEqual(saved.mode, "P")


if __name__ == "__main__":
    unittest.main(verbosity=2)
