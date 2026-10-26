"""Batch editing tools: frame changes and folder-wide action aliases."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets

from action_alias import add_action_aliases
from data import FD_STR, BatchFrameEdit, Offset
from gui import ScreenFitDialog

if TYPE_CHECKING:
    from main import AnimationEditor

class AliasWorker(QtCore.QThread):
    completed = QtCore.Signal(str, object)
    failed = QtCore.Signal(str)

    def __init__(self, parent, source, destination, action, fallbacks, index):
        super().__init__(parent)
        self.arguments = source, destination, action, fallbacks, index

    def run(self):
        try:
            path, counts = add_action_aliases(*self.arguments)
        except Exception as error:
            self.failed.emit(str(error))
        else:
            self.completed.emit(str(path), counts)


class ActionAliasDialog(ScreenFitDialog):
    def __init__(self, source: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Action Alias")
        self.resize(560, 480)
        self.worker = None
        self.log_path = None
        layout = QtWidgets.QVBoxLayout(self)
        description = QtWidgets.QLabel(
            "Ensure an action exists in each sprite by adding a CopyOf alias. "
            "Existing actions are always kept. The complete source folder is copied "
            "to a new output folder; original files are never modified."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        self.controls = QtWidgets.QWidget(self)
        form = QtWidgets.QFormLayout(self.controls)
        self.sourceEdit = QtWidgets.QLineEdit(source)
        self.outputEdit = QtWidgets.QLineEdit()
        for label, edit, callback in (
            ("Source folder:", self.sourceEdit, self.browse_source),
            ("New output folder:", self.outputEdit, self.browse_output),
        ):
            row = QtWidgets.QHBoxLayout()
            row.addWidget(edit)
            button = QtWidgets.QPushButton("Browse...")
            button.clicked.connect(callback)
            row.addWidget(button)
            form.addRow(label, row)
        self.actionEdit = QtWidgets.QLineEdit()
        self.actionEdit.setPlaceholderText("e.g. Fly")
        form.addRow("Action name:", self.actionEdit)
        self.fallbackEdit = QtWidgets.QPlainTextEdit()
        self.fallbackEdit.setPlaceholderText("Hover\nWalk\nIdle")
        self.fallbackEdit.setToolTip("Enter one action per line, in preference order. Names are not case sensitive.")
        form.addRow("Fallbacks:\n(one per line,\nfirst choice at top)", self.fallbackEdit)
        self.indexSpinBox = QtWidgets.QSpinBox()
        self.indexSpinBox.setRange(-1, 2147483647)
        self.indexSpinBox.setValue(-1)
        self.indexSpinBox.setSpecialValueText("Automatic per sprite")
        self.indexSpinBox.setToolTip("Automatic uses the next unused index in each sprite. A specified index is used for all sprites. Check logs for conflicts.")
        form.addRow("Action index:", self.indexSpinBox)
        layout.addWidget(self.controls)
        hint = QtWidgets.QLabel(
            "Processes FrameData.xml and AnimData.xml in all subfolders. "
            "The output folder must not exist and must be outside the source folder. "
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.statusLabel = QtWidgets.QLabel()
        self.statusLabel.setWordWrap(True)
        self.statusLabel.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.statusLabel)
        self.buttonBox = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Apply
            | QtWidgets.QDialogButtonBox.StandardButton.Close
        )
        self.applyButton = self.buttonBox.button(QtWidgets.QDialogButtonBox.StandardButton.Apply)
        self.applyButton.setText("Create Output")
        self.applyButton.clicked.connect(self.apply)
        self.buttonBox.rejected.connect(self.reject)
        self.logButton = self.buttonBox.addButton("Open Log", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        self.logButton.setEnabled(False)
        self.logButton.clicked.connect(self.open_log)
        layout.addWidget(self.buttonBox)
        self.sourceEdit.editingFinished.connect(self.suggest_output)
        self.suggest_output()
        QtWidgets.QApplication.instance().aboutToQuit.connect(self.wait_for_worker)

    def suggest_output(self):
        if self.sourceEdit.text().strip() and not self.outputEdit.text():
            source = Path(self.sourceEdit.text().strip())
            if source.name:
                self.outputEdit.setText(str(source.with_name(source.name + "-aliases")))

    def browse_source(self):
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose Source Folder", self.sourceEdit.text())
        if selected:
            self.sourceEdit.setText(selected)
            self.outputEdit.clear()
            self.suggest_output()

    def browse_output(self):
        # Select an existing parent, then name a new child folder.
        parent = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose Output Parent Folder", str(Path(self.outputEdit.text()).parent))
        if parent:
            name, accepted = QtWidgets.QInputDialog.getText(self, "New Output Folder", "Folder name:", text="sprites-aliases")
            if accepted and name.strip():
                if Path(name).name != name or name in {".", ".."}:
                    self.statusLabel.setText("Enter a folder name, without a path.")
                else:
                    self.outputEdit.setText(str(Path(parent) / name))

    def apply(self):
        if self.worker is not None:
            return
        if not self.sourceEdit.text().strip() or not self.outputEdit.text().strip():
            self.statusLabel.setText("Choose a source folder and a new output folder.")
            return
        self.controls.setEnabled(False)
        self.applyButton.setEnabled(False)
        self.buttonBox.button(QtWidgets.QDialogButtonBox.StandardButton.Close).setEnabled(False)
        self.logButton.setEnabled(False)
        self.statusLabel.setText("Copying sprite files and adding missing aliases...")
        self.worker = AliasWorker(
            self, self.sourceEdit.text().strip(), self.outputEdit.text().strip(),
            self.actionEdit.text(), self.fallbackEdit.toPlainText().splitlines(),
            None if self.indexSpinBox.value() == -1 else self.indexSpinBox.value(),
        )
        self.worker.completed.connect(self.completed)
        self.worker.failed.connect(self.statusLabel.setText)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()

    def completed(self, path, counts):
        self.log_path = path
        summary = ", ".join(f"{key.lower().replace('_', ' ')}: {value}" for key, value in sorted(counts.items()))
        self.statusLabel.setText(f"Finished — {summary}.\nLog: {path}")
        self.logButton.setEnabled(True)

    def worker_finished(self):
        self.worker.deleteLater()
        self.worker = None
        self.controls.setEnabled(True)
        self.applyButton.setEnabled(True)
        self.buttonBox.button(QtWidgets.QDialogButtonBox.StandardButton.Close).setEnabled(True)

    def open_log(self):
        if self.log_path:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self.log_path))

    def wait_for_worker(self):
        if self.worker is not None:
            self.worker.wait()

    def reject(self):
        if self.worker is None:
            super().reject()

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
        else:
            super().closeEvent(event)


class BatchEditDialog(ScreenFitDialog):
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
