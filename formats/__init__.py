"""Native authoring and exported XML formats with shared validation entry points."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from data import SheetFormat
from utils import FormatError

from .exported import detect_format, validate_source
from .adapters import MultiSheetFormat, NativeFormat, SingleSheetFormat, get_adapter
from .base import SpriteFormat

if TYPE_CHECKING:
    from conversion import SpriteDocument
    from main import AnimationEditor

__all__ = [
    "FormatError", "MultiSheetFormat", "NativeFormat", "SingleSheetFormat",
    "SpriteFormat", "detect_format", "get_adapter", "validate_export", "validate_source",
]


def validate_export(
    editor: SpriteDocument | AnimationEditor,
    format_name: SheetFormat | SpriteFormat,
) -> None:
    """Route validation to the native or exported format implementation."""
    document = cast("SpriteDocument", editor._sprite_document() if hasattr(editor, "_sprite_document") else editor)
    get_adapter(format_name).validate_export(document)
