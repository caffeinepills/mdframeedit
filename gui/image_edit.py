"""Image authoring dialogs: new atlases and resizing existing frame cells."""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

from PIL import Image
from gui import ScreenFitDialog

from PySide6 import QtCore, QtGui, QtWidgets

from conversion import SpriteDocument
from utils import FormatError

if TYPE_CHECKING:
    from main import AnimationEditor


IMAGE_SUFFIXES = {".png", ".bmp", ".jpg", ".jpeg", ".webp"}
IMAGE_FILTER = "Images (*.png *.bmp *.jpg *.jpeg *.webp)"


class AtlasGridPreview(QtWidgets.QWidget):
    def __init__(self, image: Image.Image, parent=None):
        super().__init__(parent)
        rgba = image.convert("RGBA")
        self.image = QtGui.QImage(rgba.tobytes(), rgba.width, rgba.height,
                                 QtGui.QImage.Format.Format_RGBA8888).copy()
        self.cell_width = self.cell_height = 32
        self.pad_edges = False
        self.zoom = 1.0
        checker = QtGui.QPixmap(16, 16)
        checker.fill(QtGui.QColor("#dddddd"))
        painter = QtGui.QPainter(checker)
        painter.fillRect(0, 0, 8, 8, QtGui.QColor("#bbbbbb"))
        painter.fillRect(8, 8, 8, 8, QtGui.QColor("#bbbbbb"))
        painter.end()
        self.checker = QtGui.QBrush(checker)

    def update_grid(self, width, height, pad_edges, zoom):
        self.cell_width, self.cell_height = width, height
        self.pad_edges, self.zoom = pad_edges, zoom
        image_width, image_height = self.image.width(), self.image.height()
        if pad_edges:
            image_width = math.ceil(image_width / width) * width
            image_height = math.ceil(image_height / height) * height
        self.setFixedSize(math.ceil(image_width * zoom) + 1, math.ceil(image_height * zoom) + 1)
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(event.rect(), self.checker)
        painter.scale(self.zoom, self.zoom)
        painter.drawImage(0, 0, self.image)
        pen = QtGui.QPen(QtGui.QColor("#008b99"))
        pen.setCosmetic(True)
        painter.setPen(pen)
        width, height = self.cell_width, self.cell_height
        columns = math.ceil(self.image.width() / width)
        rows = math.ceil(self.image.height() / height)
        # Only draw the visible portion, even for large sheets at high zoom.
        visible = event.rect()
        first_col = max(0, int(visible.left() / self.zoom / width))
        last_col = min(columns, math.ceil(visible.right() / self.zoom / width))
        first_row = max(0, int(visible.top() / self.zoom / height))
        last_row = min(rows, math.ceil(visible.bottom() / self.zoom / height))
        for col in range(first_col, last_col + 1):
            painter.drawLine(col * width, 0, col * width, rows * height)
        for row in range(first_row, last_row + 1):
            painter.drawLine(0, row * height, columns * width, row * height)
        if not self.pad_edges:
            shade = QtGui.QColor(220, 50, 50, 100)
            full_width = self.image.width() // width * width
            full_height = self.image.height() // height * height
            painter.fillRect(full_width, 0, self.image.width() - full_width, self.image.height(), shade)
            painter.fillRect(0, full_height, full_width, self.image.height() - full_height, shade)
        painter.resetTransform()
        if width * self.zoom >= 36 and height * self.zoom >= 24:
            for row in range(first_row, last_row):
                for col in range(first_col, last_col):
                    label = str(row * columns + col)
                    x, y = int(col * width * self.zoom) + 3, int(row * height * self.zoom) + 3
                    rect = QtCore.QRect(x, y, painter.fontMetrics().horizontalAdvance(label) + 6, painter.fontMetrics().height())
                    painter.fillRect(rect, QtGui.QColor(255, 255, 255, 210))
                    painter.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, label)


class ImageAtlasDialog(ScreenFitDialog):
    def __init__(self, image: Image.Image, parent=None, *, cell_width=32, cell_height=32):
        super().__init__(parent)
        self.image = image
        self.document = None
        self.setWindowTitle("New Sprite from Image")
        self.resize(760, 600)
        layout = QtWidgets.QVBoxLayout(self)
        description = QtWidgets.QLabel(
            f"Image: {image.width} × {image.height} pixels. Choose the size of each frame cell. "
            "Cells are numbered left to right, then top to bottom."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        form = QtWidgets.QFormLayout()
        self.width_spin = QtWidgets.QSpinBox()
        self.height_spin = QtWidgets.QSpinBox()
        for spin, value, dimension in ((self.width_spin, cell_width, image.width),
                                       (self.height_spin, cell_height, image.height)):
            spin.setRange(1, 32767)
            spin.setSuffix(" px")
            spin.setValue(min(value, dimension))
        form.addRow("Cell width:", self.width_spin)
        form.addRow("Cell height:", self.height_spin)
        layout.addLayout(form)
        self.pad_checkbox = QtWidgets.QCheckBox("Pad incomplete edge cells with transparency")
        layout.addWidget(self.pad_checkbox)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.preview = AtlasGridPreview(image)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidget(self.preview)
        layout.addWidget(self.scroll, 1)
        zoom_row = QtWidgets.QHBoxLayout()
        zoom_row.addWidget(QtWidgets.QLabel("Zoom:"))
        self.zoom_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(10, 800)
        self.zoom_slider.setValue(max(10, int(min(1, 680 / image.width, 280 / image.height) * 100)))
        zoom_row.addWidget(self.zoom_slider, 1)
        self.zoom_label = QtWidgets.QLabel()
        zoom_row.addWidget(self.zoom_label)
        layout.addLayout(zoom_row)
        note = QtWidgets.QLabel("All cells, including blank cells, are kept. Start with an empty Default action and add animation frames in the editor. Save creates a JSON document and PNG atlas.")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Open | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.open_button = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Open)
        self.open_button.setText("Create Sprite")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for spin in (self.width_spin, self.height_spin):
            spin.valueChanged.connect(self.update_preview)
        self.pad_checkbox.toggled.connect(self.update_preview)
        self.zoom_slider.valueChanged.connect(self.update_preview)
        self.update_preview()

    def update_preview(self, *_):
        width, height = self.width_spin.value(), self.height_spin.value()
        columns, rows = math.ceil(self.image.width / width), math.ceil(self.image.height / height)
        partial = self.image.width % width or self.image.height % height
        padded = self.pad_checkbox.isChecked()
        valid = not partial or padded
        self.open_button.setEnabled(bool(valid))
        message = f"{columns} columns × {rows} rows = {columns * rows} cells."
        if partial:
            message += (f" Padded atlas: {columns * width} × {rows * height} pixels."
                        if padded else " Incomplete edges are highlighted. Adjust the cell size or enable padding.")
        self.summary.setText(message)
        zoom = self.zoom_slider.value() / 100
        self.zoom_label.setText(f"{self.zoom_slider.value()}%")
        self.preview.update_grid(width, height, padded, zoom)

    def accept(self):
        try:
            self.document = SpriteDocument.from_image(self.image, self.width_spin.value(), self.height_spin.value(),
                                                      pad_edges=self.pad_checkbox.isChecked())
        except FormatError as error:
            self.summary.setText(str(error))
            return
        super().accept()



class CellResizePreview(QtWidgets.QWidget):
    def __init__(self, image: QtGui.QImage, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.image = image
        self.cellSize = image.size()
        self.referenceSize = image.size()
        self.centerArtwork = False
        self.artworkBounds = None
        self.setMinimumSize(160, 160)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding
        )

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        area = QtCore.QRectF(self.contentsRect().adjusted(8, 8, -8, -8))
        width, height = self.cellSize.width(), self.cellSize.height()
        scale = min(
            area.width() / self.referenceSize.width(),
            area.height() / self.referenceSize.height(),
        )
        if scale >= 1:
            scale = int(scale)
        cell = QtCore.QRectF(0, 0, width * scale, height * scale)
        cell.moveCenter(area.center())
        painter.fillRect(cell, self.palette().base())
        painter.save()
        painter.setClipRect(cell)
        dx = width // 2 - self.image.width() // 2
        dy = height // 2 - self.image.height() // 2
        if self.centerArtwork and self.artworkBounds is not None:
            left, top, right, bottom = self.artworkBounds
            dx += self.image.width() // 2 - left - (right - left) // 2
            dy += self.image.height() // 2 - top - (bottom - top) // 2
        painter.drawImage(QtCore.QRectF(
            cell.left() + dx * scale, cell.top() + dy * scale,
            self.image.width() * scale, self.image.height() * scale,
        ), self.image)
        painter.restore()
        painter.setPen(self.palette().text().color())
        painter.drawRect(cell)


class ResizeFramesDialog(ScreenFitDialog):
    def __init__(self, editor: AnimationEditor) -> None:
        super().__init__(editor.window)
        self.editor = editor
        self.setWindowTitle("Edit Frame Cells")
        layout = QtWidgets.QVBoxLayout(self)
        description = QtWidgets.QLabel(
            "Resize, trim, or center artwork in every sheet cell. Artwork stays "
            "at its original pixel size and keeps its position during playback.", self,
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        form = QtWidgets.QFormLayout()
        form.addRow("Current size:", QtWidgets.QLabel(
            f"{editor.frame_width} x {editor.frame_height} pixels", self
        ))
        self.widthSpinBox = QtWidgets.QSpinBox(self)
        self.heightSpinBox = QtWidgets.QSpinBox(self)
        for spinBox, value in (
            (self.widthSpinBox, editor.frame_width),
            (self.heightSpinBox, editor.frame_height),
        ):
            spinBox.setRange(1, 32767)
            spinBox.setSuffix(" px")
            spinBox.setValue(value)
        form.addRow("New width:", self.widthSpinBox)
        form.addRow("New height:", self.heightSpinBox)
        layout.addLayout(form)
        self.centerArtworkCheckBox = QtWidgets.QCheckBox("Center artwork in each cell", self)
        layout.addWidget(self.centerArtworkCheckBox)
        trimLayout = QtWidgets.QHBoxLayout()
        self.trimButton = QtWidgets.QPushButton("Trim Extra Space", self)
        self.trimButton.setToolTip(
            "Fit all frames and action points using the selected centering option"
        )
        self.trimButton.clicked.connect(self.trimExtraSpace)
        trimLayout.addWidget(self.trimButton)
        trimLayout.addStretch()
        layout.addLayout(trimLayout)
        index = editor.selected_sheet_frame
        if index is None:
            index = editor.current_anim_frame.frame_index if editor.current_anim_frame else 0
        image = editor.image_grid[index]
        pixels = image.get_image_data().get_data("RGBA", -image.width * 4)
        artworkBounds = Image.frombytes("RGBA", (image.width, image.height), pixels).getchannel("A").getbbox()
        previewImage = QtGui.QImage(
            pixels, image.width, image.height, QtGui.QImage.Format.Format_RGBA8888
        ).copy()
        previews = QtWidgets.QHBoxLayout()
        for title in ("Before", "After"):
            group = QtWidgets.QGroupBox(f"{title} - Frame {index}", self)
            groupLayout = QtWidgets.QVBoxLayout(group)
            preview = CellResizePreview(previewImage, group)
            preview.artworkBounds = artworkBounds
            groupLayout.addWidget(preview)
            previews.addWidget(group)
            if title == "After":
                self.afterPreview = preview
            else:
                self.beforePreview = preview
        layout.addLayout(previews, 1)
        self.errorLabel = QtWidgets.QLabel(self)
        self.errorLabel.setWordWrap(True)
        self.errorLabel.hide()
        layout.addWidget(self.errorLabel)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Apply
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel, self,
        )
        self.applyButton = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Apply)
        self.applyButton.clicked.connect(self.applyResize)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.widthSpinBox.valueChanged.connect(self.updatePreview)
        self.heightSpinBox.valueChanged.connect(self.updatePreview)
        self.centerArtworkCheckBox.toggled.connect(self.updatePreview)
        self.updatePreview()
        self.resize(self.fontMetrics().horizontalAdvance("0") * 65, 440)

    @QtCore.Slot()
    def trimExtraSpace(self) -> None:
        width, height = self.editor._sprite_document().trimmed_frame_size(
            center_artwork=self.centerArtworkCheckBox.isChecked()
        )
        self.widthSpinBox.setValue(width)
        self.heightSpinBox.setValue(height)

    @QtCore.Slot()
    def updatePreview(self) -> None:
        self.afterPreview.centerArtwork = self.centerArtworkCheckBox.isChecked()
        self.afterPreview.cellSize = QtCore.QSize(
            self.widthSpinBox.value(), self.heightSpinBox.value()
        )
        referenceSize = self.afterPreview.cellSize.expandedTo(self.beforePreview.cellSize)
        self.beforePreview.referenceSize = referenceSize
        self.afterPreview.referenceSize = referenceSize
        self.beforePreview.update()
        self.afterPreview.update()
        self.errorLabel.hide()
        self.applyButton.setEnabled(
            self.centerArtworkCheckBox.isChecked() or self.afterPreview.cellSize != QtCore.QSize(
                self.editor.frame_width, self.editor.frame_height
            )
        )

    @QtCore.Slot()
    def applyResize(self) -> None:
        try:
            self.editor.resizeFrameCells(
                self.widthSpinBox.value(), self.heightSpinBox.value(),
                centerArtwork=self.centerArtworkCheckBox.isChecked(),
            )
        except (FormatError, ValueError, MemoryError) as error:
            self.errorLabel.setText(str(error) or "Not enough memory for that sheet size.")
            self.errorLabel.show()
            return
        self.accept()
