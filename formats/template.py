"""Copy this module to implement an explicit custom backend format adapter."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .base import DocumentT, SpriteFormat

if TYPE_CHECKING:
    from conversion import SpriteDocument


class CustomFormat(SpriteFormat):
    """Starter only: replace each stub with your own format's implementation."""

    format_id = "custom"
    default_filename = "Sprite.custom"

    def read(self, path: Path, document_class: type[DocumentT]) -> DocumentT:
        # Validate your schema and companion images, then construct
        # document_class(image, frame_width, frame_height, groups=..., ...).
        # Retain shared-direction identity, all offsets and optional event state.
        # Do not apply game rules while importing an authored document.
        raise NotImplementedError("Implement custom sprite loading.")

    def validate_export(self, document: SpriteDocument) -> None:
        # Raise FormatError if anything would be clipped, omitted or changed.
        # Define your own restrictions; do not inherit XML marker constraints.
        raise NotImplementedError("Implement custom export compatibility checks.")

    def write(self, document: SpriteDocument, directory: Path, filename: str) -> None:
        self.validate_export(document)
        # Write metadata as directory / filename and companion images here.
        # The caller validates and commits the staged bundle transactionally.
        raise NotImplementedError("Implement custom sprite serialization.")
