"""Batch action alias dialog, independent of the active sprite's frame data."""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from action_alias import add_action_aliases


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


class ActionAliasDialog(QtWidgets.QDialog):
    def __init__(self, source: str = ""):
        super().__init__()
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
            button = QtWidgets.QPushButton("Browse…")
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
        self.statusLabel.setText("Copying sprite files and adding missing aliases…")
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
