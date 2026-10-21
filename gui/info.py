from __future__ import annotations

from typing import TYPE_CHECKING

from PIL import Image
from gui import ScreenFitDialog

from PySide6 import QtCore, QtGui, QtWidgets

from data import ArtworkInfo, SheetFormat
from formats import validate_export
from utils import FormatError, analyze_artwork

if TYPE_CHECKING:
    from main import AnimationEditor


class PaletteModel(QtCore.QAbstractTableModel):
    """Display large palettes without creating a widget or item for every cell."""

    headers = ("Color", "Hex", "RGBA", "Pixels")

    def __init__(self, info: ArtworkInfo, parent: QtCore.QObject) -> None:
        super().__init__(parent)
        self.colors = list(info.palette)

    def rowCount(self, parent: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.colors)

    def columnCount(self, parent: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(
        self,
        section: int,
        orientation: QtCore.Qt.Orientation,
        role: int = QtCore.Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if (
            role == QtCore.Qt.ItemDataRole.DisplayRole
            and orientation == QtCore.Qt.Orientation.Horizontal
        ):
            return self.headers[section]
        return None

    def data(
        self, index: QtCore.QModelIndex, role: int = QtCore.Qt.ItemDataRole.DisplayRole
    ) -> object:
        if not index.isValid():
            return None
        color = self.colors[index.row()]
        column = index.column()
        if role == QtCore.Qt.ItemDataRole.BackgroundRole and column == 0:
            return QtGui.QColor(*color.rgba)
        if column == 2 and 0 < color.rgba[3] < 255:
            if role == QtCore.Qt.ItemDataRole.BackgroundRole:
                return QtGui.QColor("#7F1D1D")
            if role == QtCore.Qt.ItemDataRole.ForegroundRole:
                return QtGui.QColor("#FFFFFF")
        if role == QtCore.Qt.ItemDataRole.TextAlignmentRole and column == 3:
            return (
                QtCore.Qt.AlignmentFlag.AlignRight
                | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            return (
                "",
                "#" + "".join(f"{channel:02X}" for channel in color.rgba[:3]),
                ", ".join(map(str, color.rgba)),
                color.pixels,
            )[column]
        return None

    def sort(
        self,
        column: int,
        order: QtCore.Qt.SortOrder = QtCore.Qt.SortOrder.AscendingOrder,
    ) -> None:
        self.layoutAboutToBeChanged.emit()
        self.colors.sort(
            key=lambda color: (
                color.pixels if column == 3 else color.rgba,
                color.rgba,
            ),
            reverse=order == QtCore.Qt.SortOrder.DescendingOrder,
        )
        self.layoutChanged.emit()


class SpriteInfoDialog(ScreenFitDialog):
    """Read-only palette, opacity, and export information for the active sprite."""

    def __init__(self, editor: AnimationEditor) -> None:
        super().__init__(editor.window)
        self.setWindowTitle("Sprite Info")
        self.setModal(True)
        self.editor = editor
        self.captureArtwork()
        layout = QtWidgets.QVBoxLayout(self)
        summary = QtWidgets.QGroupBox("Artwork", self)
        summaryLayout = QtWidgets.QHBoxLayout(summary)
        sheetForm = QtWidgets.QFormLayout()
        pixelForm = QtWidgets.QFormLayout()
        summaryLayout.addLayout(sheetForm, 1)
        summaryLayout.addLayout(pixelForm, 1)
        self.summaryLabels: dict[str, QtWidgets.QLabel] = {}
        for title, value in self.artworkSummary():
            label = QtWidgets.QLabel(value, summary)
            self.summaryLabels[title] = label
            label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            label.setTextInteractionFlags(
                QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
            )
            form = (
                pixelForm
                if title
                in ("Sheet cells / referenced:", "Actions:", "Palette colors (RGBA):")
                else sheetForm
            )
            form.addRow(title, label)
        layout.addWidget(summary)

        self.checks: list[str] = []
        checksBox = QtWidgets.QGroupBox("Export Compatibility", self)
        checksLayout = QtWidgets.QVBoxLayout(checksBox)
        self.checksText = QtWidgets.QPlainTextEdit(checksBox)
        self.updateChecks()
        self.checksText.setReadOnly(True)
        self.checksText.setMaximumHeight(self.fontMetrics().lineSpacing() * 4)
        checksLayout.addWidget(self.checksText)
        layout.addWidget(checksBox)

        self.createPaletteControls(layout)

    def captureArtwork(self) -> None:
        editor = self.editor
        image = editor.sheet_image
        artwork = Image.frombytes(
            "RGBA",
            (image.width, image.height),
            image.get_image_data().get_data("RGBA", -image.width * 4),
        )
        self.info = analyze_artwork(artwork)
        self.artwork = artwork
        self.frameWidth = editor.frame_width
        self.frameHeight = editor.frame_height
        self.frameColumns = editor.image_grid.columns

    def artworkSummary(self) -> tuple[tuple[str, str], ...]:
        editor = self.editor
        image = editor.sheet_image
        referenced = {
            frame.frame_index
            for group in editor.groups
            for sequence in group.directions
            for frame in sequence.frames
        }
        return (
            (
                "Mode:",
                "Native JSON" if editor.sheet_format == SheetFormat.NATIVE else "Single sheet"
                if editor.sheet_format == SheetFormat.SINGLE
                else "Multi-sheet",
            ),
            ("Sheet dimensions:", f"{image.width} x {image.height} pixels"),
            (
                "Frame cell dimensions:",
                f"{editor.frame_width} x {editor.frame_height} pixels",
            ),
            (
                "Sheet cells / referenced:",
                f"{len(editor.image_grid):,} / {len(referenced):,}",
            ),
            ("Actions:", f"{len(editor.groups):,}"),
            ("Palette colors (RGBA):", f"{len(self.info.palette):,}"),
        )

    def updateChecks(self) -> None:
        self.checks.clear()
        for sheetFormat in (SheetFormat.SINGLE, SheetFormat.MULTI):
            try:
                validate_export(self.editor, sheetFormat)
                result = "Compatible"
            except FormatError as error:
                result = f"Not compatible: {error}"
            self.checks.append(f"{sheetFormat.value}: {result}")
        self.checksText.setPlainText("\n".join(self.checks))

    def createPaletteControls(self, layout: QtWidgets.QVBoxLayout) -> None:
        explanation = QtWidgets.QLabel(
            "Each row is an exact RGBA color. Translucent values are highlighted."
            "Counts cover the selected frame or all loaded artwork depending on dropdown.",
            self,
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        paletteLayout = QtWidgets.QHBoxLayout()
        self.frameComboBox = QtWidgets.QComboBox(self)
        self.frameComboBox.addItem("All Frames", None)
        for frameIndex in range(len(self.editor.image_grid)):
            self.frameComboBox.addItem(f"Frame {frameIndex}", frameIndex)
        frameLabel = QtWidgets.QLabel("Palette:", self)
        frameLabel.setBuddy(self.frameComboBox)
        self.paletteCountLabel = QtWidgets.QLabel(self)
        paletteLayout.addWidget(frameLabel)
        paletteLayout.addWidget(self.frameComboBox)
        paletteLayout.addWidget(self.paletteCountLabel)
        paletteLayout.addStretch()
        layout.addLayout(paletteLayout)
        self.paletteModel = PaletteModel(self.info, self)
        self.paletteTable = QtWidgets.QTableView(self)
        self.paletteTable.setModel(self.paletteModel)
        self.paletteTable.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.paletteTable.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.paletteTable.verticalHeader().hide()
        self.paletteTable.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.paletteTable.horizontalHeader().setStretchLastSection(True)
        self.paletteTable.setSortingEnabled(True)
        self.paletteTable.sortByColumn(3, QtCore.Qt.SortOrder.DescendingOrder)
        self.frameComboBox.currentIndexChanged.connect(self.updatePalette)
        self.updatePalette()
        layout.addWidget(self.paletteTable, 1)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Close, self
        )
        self.refreshButton = buttons.addButton(
            "Refresh", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole
        )
        self.refreshButton.setToolTip("Refresh information from the loaded artwork")
        self.refreshButton.clicked.connect(self.refreshInfo)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(
            self.fontMetrics().horizontalAdvance("0") * 95,
            min(
                self.fontMetrics().lineSpacing() * 34,
                round(self.screen().availableGeometry().height() * 0.85),
            ),
        )

    @QtCore.Slot()
    def refreshInfo(self) -> None:
        frameIndex = self.frameComboBox.currentData()
        self.captureArtwork()
        for title, value in self.artworkSummary():
            self.summaryLabels[title].setText(value)
        self.updateChecks()
        with QtCore.QSignalBlocker(self.frameComboBox):
            self.frameComboBox.clear()
            self.frameComboBox.addItem("All Frames", None)
            for index in range(len(self.editor.image_grid)):
                self.frameComboBox.addItem(f"Frame {index}", index)
            selected = self.frameComboBox.findData(frameIndex)
            self.frameComboBox.setCurrentIndex(max(0, selected))
        self.updatePalette()

    @QtCore.Slot()
    def updatePalette(self) -> None:
        frameIndex = self.frameComboBox.currentData()
        if frameIndex is None:
            info = self.info
        else:
            row, column = divmod(frameIndex, self.frameColumns)
            left = column * self.frameWidth
            top = row * self.frameHeight
            info = analyze_artwork(self.artwork.crop((
                left, top, left + self.frameWidth, top + self.frameHeight,
            )))
        self.paletteModel.beginResetModel()
        self.paletteModel.colors = list(info.palette)
        self.paletteModel.endResetModel()
        header = self.paletteTable.horizontalHeader()
        self.paletteModel.sort(header.sortIndicatorSection(), header.sortIndicatorOrder())
        self.paletteCountLabel.setText(f"{len(info.palette):,} colors")
