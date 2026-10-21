"""Built-in adapters for native authoring and Mystery Dungeon XML bundles."""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from data import SheetFormat

from .base import DocumentT, SpriteFormat
from .exported import detect_format, validate_source
from .exported import validate_export as validate_xml_export

if TYPE_CHECKING:
    from conversion import SpriteDocument


class NativeFormat(SpriteFormat):
    """Keep the optimized native serializer and reader directly accessible."""

    format_id = "native"
    default_filename = "Sprite.json"

    @property
    def document_format(self) -> SheetFormat:
        return SheetFormat.NATIVE

    def read(self, path: Path, document_class: type[DocumentT]) -> DocumentT:
        from .native import load_document

        return load_document(path, document_class)

    def validate_export(self, document: SpriteDocument) -> None:
        from .native import document_data

        document_data(document, "Sprite.png")

    def write(self, document: SpriteDocument, directory: Path, filename: str) -> None:
        from .native import write_document

        # write_document already validates while building the pooled data. Do
        # not build or expand another representation through a generic layer.
        write_document(document, directory / filename)


class _MysteryDungeonFormat(SpriteFormat):
    sheet_format: ClassVar[SheetFormat]

    @property
    def document_format(self) -> SheetFormat:
        return self.sheet_format

    def read(self, path: Path, document_class: type[DocumentT]) -> DocumentT:
        tree = validate_source(path, self.sheet_format)
        root = tree.getroot()
        if self.sheet_format == SheetFormat.SINGLE:
            document = document_class._load_single(path.parent, root)
        else:
            document = document_class._load_multi(path.parent, root)
        document._resolve_copies()
        document.loaded_action_points = copy.deepcopy(document.action_points)
        document.source_tree = tree
        document.sheet_format = self.sheet_format
        return document

    def validate_export(self, document: SpriteDocument) -> None:
        validate_xml_export(document, self.sheet_format)

    def write(self, document: SpriteDocument, directory: Path, filename: str) -> None:
        self.validate_export(document)
        if self.sheet_format == SheetFormat.SINGLE:
            document._write_single_sheet(directory)
        else:
            document._write_multiple_sheets(directory)
        if filename != self.default_filename:
            (directory / self.default_filename).rename(directory / filename)

    def validate_written(self, directory: Path, filename: str, document_class: type[DocumentT]) -> None:
        validate_source(directory / filename, self.sheet_format)


class SingleSheetFormat(_MysteryDungeonFormat):
    """Mystery Dungeon FrameData.xml and its shared image sheets."""

    format_id = "single"
    default_filename = "FrameData.xml"
    sheet_format = SheetFormat.SINGLE

    def obsolete_files(self, directory: Path) -> tuple[str, ...]:
        return () if (directory / "Offsets.png").exists() else ("Offsets.png",)


class MultiSheetFormat(_MysteryDungeonFormat):
    """Mystery Dungeon AnimData.xml and per-animation image triplets."""

    format_id = "multi"
    default_filename = "AnimData.xml"
    sheet_format = SheetFormat.MULTI


_BUILTINS: dict[SheetFormat, SpriteFormat] = {
    SheetFormat.NATIVE: NativeFormat(),
    SheetFormat.SINGLE: SingleSheetFormat(),
    SheetFormat.MULTI: MultiSheetFormat(),
}


def get_adapter(format_name: SheetFormat | SpriteFormat) -> SpriteFormat:
    """Accept a built-in selection or an explicitly supplied custom adapter."""
    if isinstance(format_name, SpriteFormat):
        return format_name
    return _BUILTINS[format_name]


def detect_adapter(path: Path) -> SpriteFormat:
    """Retain built-in automatic detection; custom formats are explicit."""
    if path.suffix.lower() == ".json":
        return _BUILTINS[SheetFormat.NATIVE]
    return _BUILTINS[detect_format(ET.parse(path).getroot())]
