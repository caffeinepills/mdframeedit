"""Portable image-to-atlas checks without GUI or local test dependencies."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conversion import SpriteDocument
from data import SheetFormat
from utils import FormatError


class ImageAtlasTest(unittest.TestCase):
    def test_document_creation_and_cli_work_without_gui_dependencies(self):
        probe = textwrap.dedent('''
            import builtins
            import runpy
            import sys
            import tempfile
            from pathlib import Path
            original_import = builtins.__import__
            def headless_import(name, *args, **kwargs):
                if name.split('.')[0] in ('PySide6', 'pyglet', 'gui'):
                    raise AssertionError('GUI dependency imported: ' + name)
                return original_import(name, *args, **kwargs)
            builtins.__import__ = headless_import
            from PIL import Image
            from conversion import SpriteDocument
            from data import AnimFrame, SheetFormat
            from cli import main
            import formats
            import preview
            import project
            document = SpriteDocument.from_image(Image.new('RGBA', (8, 8)), 4, 4)
            for sequence in document.groups[0].directions:
                sequence.frames.append(AnimFrame(frame_index=0, duration=1))
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = document.export(root / 'native', SheetFormat.NATIVE)
                assert main(['convert', str(source), '--to', 'single', '-o', str(root / 'single')]) == 0
                sys.argv = ['main.py', 'convert', str(source), '--to', 'single', '-o', str(root / 'legacy')]
                try:
                    runpy.run_path('main.py', run_name='__main__')
                except SystemExit as result:
                    assert result.code == 0, result.code
                assert (root / 'single' / 'FrameData.xml').is_file()
                assert (root / 'legacy' / 'FrameData.xml').is_file()
        ''')
        result = subprocess.run([sys.executable, '-c', probe],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_grid_order_blank_cells_and_pixels_survive_native_save(self):
        image = Image.new("RGBA", (12, 8))
        image.putpixel((0, 0), (10, 20, 30, 0))
        image.putpixel((11, 7), (100, 120, 140, 180))
        document = SpriteDocument.from_image(image, 4, 4)
        self.assertEqual(len(document.image_grid), 6)
        self.assertEqual(document.image_grid[5].getpixel((3, 3)), (100, 120, 140, 180))
        self.assertEqual([group.name for group in document.groups], ["Default"])
        self.assertEqual(len(document.groups[0].directions), 8)
        self.assertTrue(all(not sequence.frames for sequence in document.groups[0].directions))
        with tempfile.TemporaryDirectory() as folder:
            output = document.export(folder, SheetFormat.NATIVE)
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(set(data["animations"]), {"Default"})
            self.assertEqual(data["frames"], [])
            self.assertEqual(data["sequences"], [[]])
            loaded = SpriteDocument.load(output)
        self.assertEqual(loaded.sheet_image.tobytes(), image.tobytes())
        self.assertEqual(loaded.groups, document.groups)
        self.assertEqual((loaded.frame_width, loaded.frame_height), (4, 4))

    def test_padding_keeps_every_source_pixel_in_its_original_position(self):
        image = Image.new("RGBA", (7, 5), (50, 60, 70, 100))
        with self.assertRaises(FormatError):
            SpriteDocument.from_image(image, 4, 4)
        document = SpriteDocument.from_image(image, 4, 4, pad_edges=True)
        self.assertEqual(document.sheet_image.size, (8, 8))
        self.assertEqual(document.sheet_image.crop((0, 0, 7, 5)).tobytes(), image.tobytes())
        self.assertEqual(document.sheet_image.getpixel((7, 7)), (0, 0, 0, 0))
        self.assertEqual(image.size, (7, 5))

    def test_invalid_cell_sizes_are_rejected(self):
        image = Image.new("RGBA", (4, 4))
        for width, height in ((0, 4), (4, -1), (True, 4), (4, 1.5)):
            with self.subTest(width=width, height=height), self.assertRaises(FormatError):
                SpriteDocument.from_image(image, width, height)


if __name__ == "__main__":
    unittest.main(verbosity=2)
