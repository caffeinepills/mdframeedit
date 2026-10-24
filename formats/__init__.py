"""Format contracts, built-in adapters and shared validation entry points."""
from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, TypeVar, cast

from data import SheetFormat
from utils import FormatError
from . import runtime
from .exported import detect_format, validate_source
from .exported import validate_export as validate_xml_export

if TYPE_CHECKING:
    from conversion import SpriteDocument
    from main import AnimationEditor

DocumentT = TypeVar("DocumentT", bound="SpriteDocument")


class SpriteFormat(ABC):
    """Read/write a flat sprite bundle through the shared authoring model.

    Adapters run only when opening, validating or exporting a document. They
    never wrap frame records or participate in an engine's JSON parsing.
    """

    format_id: ClassVar[str]
    default_filename: ClassVar[str]

    @property
    def document_format(self) -> SheetFormat | str:
        """Identify the source format without requiring a new enum member."""
        return self.format_id

    def check_filename(self, filename: str) -> None:
        """Require a local metadata filename with this format's extension."""
        suffix = Path(self.default_filename).suffix.lower()
        if not suffix or Path(filename).name != filename or Path(filename).suffix.lower() != suffix:
            msg = f"Animation data must be saved as a {suffix} file in the destination folder."
            raise FormatError(msg)

    @abstractmethod
    def read(self, path: Path, document_class: type[DocumentT]) -> DocumentT:
        """Validate source files and return a completely loaded document."""

    @abstractmethod
    def validate_export(self, document: SpriteDocument) -> None:
        """Reject any document values this format cannot represent losslessly."""

    @abstractmethod
    def write(self, document: SpriteDocument, directory: Path, filename: str) -> None:
        """Validate and write a complete bundle, only inside the staging directory.

        The metadata must use filename. Auxiliary files must have local names;
        nested directories are not supported by this initial adapter interface.
        """

    def validate_written(self, directory: Path, filename: str, document_class: type[DocumentT]) -> None:
        """Check staged output before any destination files are replaced."""
        self.read(directory / filename, document_class)

    def write_bundle(self, document: SpriteDocument, directory: Path, filename: str, destination: Path) -> None:
        """Write staged files, optionally accounting for their final location."""
        self.write(document, directory, filename)

    def validate_bundle(self, directory: Path, filename: str, document_class: type[DocumentT], destination: Path) -> None:
        self.validate_written(directory, filename, document_class)

    def obsolete_files(self, directory: Path) -> tuple[str, ...]:
        """Return optional companion filenames to remove transactionally."""
        return ()


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

    def write_bundle(self, document: SpriteDocument, directory: Path, filename: str, destination: Path) -> None:
        from .native import relative_sprite_path
        from utils import FormatError
        self.write(document, directory, filename)
        generated = {(destination / file.name).resolve() for file in directory.iterdir() if file.is_file()}
        for path in directory.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("format") != "mdframeedit":
                continue
            changed = False
            for field in ("rig_profile",):
                if field in data:
                    source = (directory / data[field]).resolve()
                    if source in generated:
                        raise FormatError(f"Saving would overwrite {field} {source}.")
                    data[field] = relative_sprite_path(source, destination)
                    changed = True
            if changed:
                path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")

    def validate_bundle(self, directory: Path, filename: str, document_class: type[DocumentT], destination: Path) -> None:
        from .native import load_document
        metadata = [json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("*.json")]
        if any("rig_profile" in data for data in metadata):
            load_document(directory / filename, document_class, _reference_directory=destination)
        else:
            self.validate_written(directory, filename, document_class)


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


class RuntimeFormat(SpriteFormat):
    """Export-only, packed game metadata and an independently loadable PNG."""

    format_id = "runtime"
    default_filename = "Sprite.mdruntime"

    @property
    def document_format(self) -> SheetFormat:
        return SheetFormat.RUNTIME

    def read(self, path: Path, document_class: type[DocumentT]) -> DocumentT:
        raise FormatError("Runtime containers are export-only; open the native JSON or XML source to edit.")

    def validate_export(self, document: SpriteDocument) -> None:
        runtime._encode(document, "Sprite.png")

    def write(self, document: SpriteDocument, directory: Path, filename: str) -> None:
        runtime.write_document(document, directory / filename)

    def validate_written(self, directory: Path, filename: str, document_class: type[DocumentT]) -> None:
        runtime.validate_written(directory / filename)


_BUILTINS: dict[SheetFormat, SpriteFormat] = {
    SheetFormat.NATIVE: NativeFormat(),
    SheetFormat.RUNTIME: RuntimeFormat(),
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
    if path.suffix.lower() == ".mdruntime":
        return _BUILTINS[SheetFormat.RUNTIME]
    return _BUILTINS[detect_format(ET.parse(path).getroot())]


def validate_export(
    editor: SpriteDocument | AnimationEditor,
    format_name: SheetFormat | SpriteFormat,
) -> None:
    """Route validation to the native or exported format implementation."""
    document = cast("SpriteDocument", editor._sprite_document() if hasattr(editor, "_sprite_document") else editor)
    get_adapter(format_name).validate_export(document)

__all__ = [
    "DocumentT", "FormatError", "MultiSheetFormat", "NativeFormat", "RuntimeFormat", "SingleSheetFormat",
    "SpriteFormat", "detect_adapter", "detect_format", "get_adapter", "validate_export", "validate_source",
]
