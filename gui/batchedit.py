from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtWidgets

from data import FD_STR, BatchFrameEdit, Offset

if TYPE_CHECKING:
    from main import AnimationEditor


class BatchEditDialog(QtWidgets.QDialog):
    """Choose which animation frames and properties to change together."""

    def __init__(self, editor: AnimationEditor) -> None:
        super().__init__(editor.window)
        self.editor: AnimationEditor | None = None
        self.editorSignals: list[QtCore.SignalInstance] = []
        self.setWindowTitle("Batch Edit Frames")
        self.setModal(False)
        self.setWindowFlag(QtCore.Qt.WindowType.Tool, True)
        layout = QtWidgets.QVBoxLayout(self)
        self.targetLabel = QtWidgets.QLabel(self)
        self.targetLabel.setWordWrap(True)
        layout.addWidget(self.targetLabel)
        scopeLayout = QtWidgets.QFormLayout()
        self.scopeComboBox = QtWidgets.QComboBox(self)
        for label, scope in (
            ("Selected frames", "selected"),
            ("Current direction", "direction"),
            ("Entire action (all directions)", "action"),
        ):
            self.scopeComboBox.addItem(label, scope)
        self.scopeComboBox.setCurrentIndex(
            0 if len(editor.ui.animationFrameList.selectedItems()) > 1 else 1
        )
        scopeLayout.addRow("Apply to:", self.scopeComboBox)
        layout.addLayout(scopeLayout)
        self.scopeLabel = QtWidgets.QLabel(self)
        layout.addWidget(self.scopeLabel)
        selectionHint = QtWidgets.QLabel(
            "Select multiple frames with Ctrl/Cmd or Shift in the animation sequence.\n"
            "Shared directional frames change together.",
            self,
        )
        selectionHint.setWordWrap(True)
        layout.addWidget(selectionHint)

        self.durationGroupBox = QtWidgets.QGroupBox("Set Duration", self)
        self.durationGroupBox.setCheckable(True)
        self.durationGroupBox.setChecked(False)
        durationLayout = QtWidgets.QFormLayout(self.durationGroupBox)
        self.durationSpinBox = QtWidgets.QSpinBox(self.durationGroupBox)
        self.durationSpinBox.setRange(1, 32767)
        self.durationSpinBox.setSuffix(" ticks")
        if editor.current_anim_frame is not None:
            self.durationSpinBox.setValue(editor.current_anim_frame.duration)
        durationLayout.addRow("Duration:", self.durationSpinBox)
        layout.addWidget(self.durationGroupBox)

        self.offsetGroups: dict[
            str,
            tuple[
                QtWidgets.QGroupBox,
                QtWidgets.QComboBox,
                QtWidgets.QSpinBox,
                QtWidgets.QSpinBox,
            ],
        ] = {}
        for name, title in (("sprite", "Sprite Offsets"), ("shadow", "Shadow Offsets")):
            group = QtWidgets.QGroupBox(title, self)
            group.setCheckable(True)
            group.setChecked(False)
            form = QtWidgets.QFormLayout(group)
            modeComboBox = QtWidgets.QComboBox(group)
            modeComboBox.addItems(["Shift by", "Set to"])
            form.addRow("Mode:", modeComboBox)
            axisLayout = QtWidgets.QHBoxLayout()
            xSpinBox, ySpinBox = QtWidgets.QSpinBox(group), QtWidgets.QSpinBox(group)
            for axis, spinBox in (("X:", xSpinBox), ("Y:", ySpinBox)):
                spinBox.setRange(-32768, 32767)
                axisLayout.addWidget(QtWidgets.QLabel(axis, group))
                axisLayout.addWidget(spinBox)
            form.addRow(axisLayout)
            layout.addWidget(group)
            self.offsetGroups[name] = (group, modeComboBox, xSpinBox, ySpinBox)
            group.toggled.connect(self.updateApplyButton)

        self.errorLabel = QtWidgets.QLabel(self)
        self.errorLabel.setWordWrap(True)
        self.errorLabel.hide()
        layout.addWidget(self.errorLabel)
        self.buttonBox = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Apply
            | QtWidgets.QDialogButtonBox.StandardButton.Close,
            self,
        )
        self.applyButton = self.buttonBox.button(
            QtWidgets.QDialogButtonBox.StandardButton.Apply
        )
        self.applyButton.clicked.connect(self.applyChanges)
        self.buttonBox.rejected.connect(self.reject)
        layout.addWidget(self.buttonBox)
        self.durationGroupBox.toggled.connect(self.updateApplyButton)
        self.scopeComboBox.currentIndexChanged.connect(self.updateApplyButton)
        self.setEditor(editor)

    def setEditor(self, editor: AnimationEditor | None) -> None:
        """Follow the active document and disconnect the previous document."""
        if editor is not self.editor:
            for signal in self.editorSignals:
                signal.disconnect(self.updateApplyButton)
            self.editorSignals.clear()
            self.editor = editor
            self.errorLabel.hide()
            if editor is not None:
                listing = editor.ui.animationFrameList
                self.editorSignals = [
                    listing.itemSelectionChanged,
                    listing.model().rowsInserted,
                    listing.model().rowsRemoved,
                    listing.model().modelReset,
                    editor.undoStack.indexChanged,
                    editor.undoStack.cleanChanged,
                ]
                for signal in self.editorSignals:
                    signal.connect(self.updateApplyButton)
        self.updateApplyButton()

    @QtCore.Slot()
    def updateApplyButton(self) -> None:
        editor = self.editor
        group = editor.current_anim_group if editor is not None else None
        if group is None:
            self.targetLabel.setText("No animation selected.")
        else:
            sprite = Path(editor.file_name).parent.name or "Untitled"
            direction = (
                "All directions"
                if self.scopeComboBox.currentData() == "action"
                else FD_STR[editor.current_direction].replace("_", " ").title()
            )
            self.targetLabel.setText(f"{sprite} / {group.name} / {direction}")
        count = (
            len(editor.batchEditFrames(self.scopeComboBox.currentData()))
            if editor is not None
            else 0
        )
        noun = "frame" if count == 1 else "frames"
        self.scopeLabel.setText(f"{count} animation {noun} will be edited.")
        hasChanges = self.durationGroupBox.isChecked() or any(
            controls[0].isChecked() for controls in self.offsetGroups.values()
        )
        self.applyButton.setEnabled(count > 0 and hasChanges)

    def accept(self) -> None:
        self.applyChanges()

    def done(self, result: int) -> None:
        self.setEditor(None)
        super().done(result)

    def applyChanges(self) -> None:
        if self.editor is None:
            return
        edit = BatchFrameEdit(
            duration=self.durationSpinBox.value()
            if self.durationGroupBox.isChecked()
            else None
        )
        for name, (
            group,
            modeComboBox,
            xSpinBox,
            ySpinBox,
        ) in self.offsetGroups.items():
            if group.isChecked():
                setattr(
                    edit, name + "_offset", Offset(xSpinBox.value(), ySpinBox.value())
                )
                setattr(edit, "shift_" + name, modeComboBox.currentIndex() == 0)
        if self.editor.applyBatchEdit(self.scopeComboBox.currentData(), edit):
            self.errorLabel.hide()
            self.updateApplyButton()
        else:
            self.errorLabel.setText(self.editor.ui.statusBar.currentMessage())
            self.errorLabel.show()
