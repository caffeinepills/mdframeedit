from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets

from utils import FormatError

if TYPE_CHECKING:
    from main import AnimationEditor


class CellResizePreview(QtWidgets.QWidget):
    def __init__(self, image: QtGui.QImage, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.image = image
        self.cellSize = image.size()
        self.referenceSize = image.size()
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
        painter.drawImage(QtCore.QRectF(
            cell.left() + dx * scale, cell.top() + dy * scale,
            self.image.width() * scale, self.image.height() * scale,
        ), self.image)
        painter.restore()
        painter.setPen(self.palette().text().color())
        painter.drawRect(cell)


class ResizeFramesDialog(QtWidgets.QDialog):
    def __init__(self, editor: AnimationEditor) -> None:
        super().__init__(editor.window)
        self.editor = editor
        self.setWindowTitle("Resize Frame Cells")
        layout = QtWidgets.QVBoxLayout(self)
        description = QtWidgets.QLabel(
            "Resize every cell in the sprite sheet. Artwork stays at its original "
            "pixel size and keeps its position during playback.", self,
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
        trimLayout = QtWidgets.QHBoxLayout()
        self.trimButton = QtWidgets.QPushButton("Trim Extra Space", self)
        self.trimButton.setToolTip(
            "Fit all frames and action points while keeping the current frame origin"
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
        previewImage = QtGui.QImage(
            pixels, image.width, image.height, QtGui.QImage.Format.Format_RGBA8888
        ).copy()
        previews = QtWidgets.QHBoxLayout()
        for title in ("Before", "After"):
            group = QtWidgets.QGroupBox(f"{title} - Frame {index}", self)
            groupLayout = QtWidgets.QVBoxLayout(group)
            preview = CellResizePreview(previewImage, group)
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
        self.updatePreview()
        self.resize(self.fontMetrics().horizontalAdvance("0") * 65, 440)

    @QtCore.Slot()
    def trimExtraSpace(self) -> None:
        width, height = self.editor._sprite_document().trimmed_frame_size()
        self.widthSpinBox.setValue(width)
        self.heightSpinBox.setValue(height)

    @QtCore.Slot()
    def updatePreview(self) -> None:
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
            self.afterPreview.cellSize != QtCore.QSize(
                self.editor.frame_width, self.editor.frame_height
            )
        )

    @QtCore.Slot()
    def applyResize(self) -> None:
        try:
            self.editor.resizeFrameCells(
                self.widthSpinBox.value(), self.heightSpinBox.value()
            )
        except (FormatError, ValueError, MemoryError) as error:
            self.errorLabel.setText(str(error) or "Not enough memory for that sheet size.")
            self.errorLabel.show()
            return
        self.accept()
