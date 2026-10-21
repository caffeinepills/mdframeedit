"""Extension interface at the sprite file I/O boundary, outside runtime data."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, TypeVar

from utils import FormatError

if TYPE_CHECKING:
    from conversion import SpriteDocument
    from data import SheetFormat

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

    def obsolete_files(self, directory: Path) -> tuple[str, ...]:
        """Return optional companion filenames to remove transactionally."""
        return ()
