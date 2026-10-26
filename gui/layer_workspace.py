"""Integrated preview tabs over real resource documents; no copied layer editor."""
import json
from pathlib import Path

from conversion import SpriteDocument
from gui import ScreenFitDialog
from utils import FormatError

from PySide6 import QtCore, QtGui, QtWidgets
from gui.editor import ElidedLabel

ADD_LAYER_TAB = "__add_layer__"

class CurrentPageStack(QtWidgets.QStackedWidget):
    """Size the document page from its visible controls."""

    def sizeHint(self):
        return self.currentWidget().sizeHint() if self.currentWidget() else super().sizeHint()

    def minimumSizeHint(self):
        return self.currentWidget().minimumSizeHint() if self.currentWidget() else super().minimumSizeHint()

class LayerTabBar(QtWidgets.QTabBar):
    interaction_finished = QtCore.Signal()
    add_requested = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self.pressed = False
        self.add_pressed = False
        self.main_identity = None

    def set_main_sprite(self, identity):
        self.main_identity = identity
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.main_identity is None:
            return
        index = next((i for i in range(self.count()) if self.tabData(i) == self.main_identity), -1)
        if index < 0:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor("#e7ad45"), 2))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QtCore.QRectF(self.tabRect(index).adjusted(1, 1, -2, -2)), 3, 3)

    def mousePressEvent(self, event):
        index = self.tabAt(event.position().toPoint())
        if index >= 0 and self.tabData(index) == ADD_LAYER_TAB:
            self.add_pressed = event.button() == QtCore.Qt.MouseButton.LeftButton and self.isTabEnabled(index)
            event.accept()
            return
        self.pressed = event.button() == QtCore.Qt.MouseButton.LeftButton
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.add_pressed:
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.add_pressed:
            self.add_pressed = False
            index = self.tabAt(event.position().toPoint())
            if index >= 0 and self.tabData(index) == ADD_LAYER_TAB and self.isTabEnabled(index):
                self.add_requested.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)
        if self.pressed:
            self.pressed = False
            self.interaction_finished.emit()

class LayerWorkspace:
    def __init__(self, root, body_page, holder, editor_class):
        self.root = root
        self.body_page = body_page
        self.holder = holder
        self.widget = CurrentPageStack()
        self.widget.addWidget(body_page)
        self.body_visible = True
        self.reordering = False
        self.visible = False
        self.headers = []
        self._add_header(root)
        root.undoStack.indexChanged.connect(self.refresh)

    @property
    def active_editor(self):
        return self.root

    def _add_header(self, editor):
        header = QtWidgets.QWidget(editor.ui.editorPanel)
        layout = QtWidgets.QVBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        row = QtWidgets.QHBoxLayout()
        tabs = LayerTabBar()
        tabs.setObjectName("layerTabs")
        tabs.setExpanding(False)
        tabs.setUsesScrollButtons(True)
        tabs.setDrawBase(False)
        tabs.setMovable(True)
        tabs.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        tabs.customContextMenuRequested.connect(lambda point: self.show_tab_menu(tabs, point))
        tabs.tabMoved.connect(lambda source, destination: self.tab_moved(tabs))
        tabs.interaction_finished.connect(lambda: self.finish_tab_interaction(tabs))
        tabs.add_requested.connect(lambda: self.show_add_menu(tabs))
        row.addWidget(tabs, 1)
        layout.addLayout(row)
        editor.ui.pointPreviewControls = QtWidgets.QWidget(header)
        preview_row = QtWidgets.QHBoxLayout(editor.ui.pointPreviewControls)
        preview_row.setContentsMargins(0, 0, 0, 0)
        editor.ui.pointPreviewPlayButton = QtWidgets.QToolButton()
        editor.ui.pointPreviewPlayButton.setText("Play Together")
        editor.ui.pointPreviewPlayButton.clicked.connect(
            lambda: self.root.point_preview.play_together() if self.root.point_preview else None)
        editor.ui.pointPreviewRelationshipButton = QtWidgets.QToolButton()
        editor.ui.pointPreviewRelationshipButton.setText("Relationship…")
        editor.ui.pointPreviewRelationshipButton.setToolTip("Choose Action Points, synchronized artwork selection and mirroring for this sprite preview.")
        editor.ui.pointPreviewRelationshipButton.clicked.connect(
            lambda: self.configure_relationship(self.root.point_preview.member(editor)))
        editor.ui.pointPreviewLabel = ElidedLabel()
        editor.ui.pointPreviewLabel.setMinimumWidth(0)
        editor.ui.pointPreviewLabel.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        preview_row.addWidget(editor.ui.pointPreviewPlayButton)
        preview_row.addWidget(editor.ui.pointPreviewRelationshipButton)
        preview_row.addWidget(editor.ui.pointPreviewLabel, 1)
        layout.addWidget(editor.ui.pointPreviewControls)
        editor.ui.pointPreviewControls.hide()
        tabs.currentChanged.connect(lambda index: self.select_tab(tabs, index))
        editor.ui.editorLayout.removeWidget(editor.ui.previewSplitter)
        editor.ui.editorLayout.addWidget(header, 0, 0)
        editor.ui.editorLayout.addWidget(editor.ui.previewSplitter, 1, 0)
        editor.ui.editorLayout.setRowStretch(0, 0)
        editor.ui.editorLayout.setRowStretch(1, 1)
        editor.ui.layerTabs = tabs
        self.headers.append(header)
        header.setVisible(self.visible)

    def set_visible(self, visible):
        self.visible = visible
        # Populate before showing: an empty QTabBar has zero height, which Qt
        # can cache for the header while a new document page is still hidden.
        self._refresh_tabs()
        for header in self.headers:
            header.setVisible(visible)

    def _refresh_tabs(self):
        preview = self.root.point_preview
        if preview:
            preview.refresh_tabs(self.root, self.root.ui.layerTabs)
            return
        tabs = self.root.ui.layerTabs
        tabs.set_main_sprite(None)
        self.root.ui.pointPreviewDetailsLabel.clear()
        self.root.ui.pointPreviewDetailsLabel.setToolTip("")
        with QtCore.QSignalBlocker(tabs):
            while tabs.count():
                tabs.removeTab(0)
            tabs.addTab("Sprite")
            tabs.setTabData(0, None)
            tabs.addTab("+")
            tabs.setTabData(1, ADD_LAYER_TAB)
            tabs.setTabEnabled(1, self.root.image_grid is not None)
        self.root.ui.pointPreviewControls.hide()

    def select_tab(self, tabs, index):
        if index < 0 or self.reordering or tabs.pressed:
            return
        identity = tabs.tabData(index)
        if identity == ADD_LAYER_TAB:
            self._refresh_tabs()
            self.show_add_menu(tabs)
        elif self.root.point_preview:
            self.root.point_preview.select(identity)

    def show_add_menu(self, tabs):
        from gui.point_preview import add_sprite_preview
        menu = QtWidgets.QMenu(tabs)
        action = menu.addAction("Add Sprite Preview…", lambda: add_sprite_preview(self.root.point_preview.root if self.root.point_preview else self.root))
        action.setEnabled(self.root.image_grid is not None and (not self.root.point_preview or len(self.root.point_preview.members) < 11))
        def browse():
            dialog = SpriteLibraryDialog(self.root)
            try:
                if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted and dialog.selected_path:
                    add_sprite_preview(self.root.point_preview.root if self.root.point_preview else self.root, dialog.selected_path)
            finally:
                dialog.deleteLater()
        menu.addAction("Browse Sprite Folder…", browse)
        menu.addSeparator()
        save = menu.addAction("Save Preview…", lambda: self.root.point_preview.save_dialog())
        save.setEnabled(self.root.point_preview is not None)
        try:
            menu.exec(tabs.mapToGlobal(tabs.tabRect(tabs.count()-1).bottomLeft()))
        finally:
            menu.deleteLater()

    @staticmethod
    def tab_identities(tabs):
        return [tabs.tabData(i) for i in range(tabs.count()) if tabs.tabData(i) != ADD_LAYER_TAB]

    def tab_moved(self, tabs):
        if tabs.pressed or self.reordering:
            return
        self.finish_tab_interaction(tabs)

    def finish_tab_interaction(self, tabs):
        if self.root.point_preview:
            self.root.point_preview.reorder(self.tab_identities(tabs))
            self.select_tab(tabs, tabs.currentIndex())

    def tab_menu(self, tabs, index):
        if index < 0 or tabs.tabData(index) == ADD_LAYER_TAB:
            return None
        from gui.point_preview import duplicate_frozen_preview
        if not self.root.point_preview:
            menu = QtWidgets.QMenu(tabs)
            duplicate = menu.addAction("Create Snapshot", lambda: duplicate_frozen_preview(self.root))
            duplicate.setEnabled(self.root.image_grid is not None and not self.root.frozen_preview)
            return menu
        preview = self.root.point_preview
        member = next((m for m in preview.members if m.identity == tabs.tabData(index)), None)
        if member is None:
            return None
        menu = QtWidgets.QMenu(tabs)
        visible = menu.addAction("Visible")
        visible.setCheckable(True)
        visible.setChecked(member.visible)
        visible.toggled.connect(lambda value: (setattr(member, "visible", value), preview.refresh()))
        menu.addAction("Rename Preview Tab…", lambda: self.rename_member(preview, member))
        duplicate = menu.addAction("Create Snapshot", lambda: duplicate_frozen_preview(member.editor))
        duplicate.setEnabled(not member.editor.frozen_preview and len(preview.members) < 11)
        if member is not preview.members[0]:
            separate = menu.addAction("Separate Alignments")
            separate.setCheckable(True)
            separate.setChecked(member.separate_alignment)
            separate.setToolTip("Show this sprite at its own origin in a separate viewport column.")
            separate.toggled.connect(lambda value: preview.set_separate_alignment(member, value))
            menu.addAction("Action Point Relationship…", lambda: self.configure_relationship(member))
        menu.addSeparator()
        menu.addAction("End Preview" if member is preview.members[0] else "Remove from Preview", lambda: preview.remove(member.editor))
        return menu

    def configure_relationship(self, member):
        from gui.point_preview import RelationshipDialog
        dialog = RelationshipDialog(self.root.point_preview, member)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def rename_member(self, preview, member):
        value, ok = QtWidgets.QInputDialog.getText(self.root.window, "Preview Tab", "Name:", text=preview.label(member.editor))
        if ok and value.strip():
            member.display_name = value.strip()
            preview.refresh()

    def show_tab_menu(self, tabs, point):
        menu = self.tab_menu(tabs, tabs.tabAt(point))
        if menu:
            try:
                menu.exec(tabs.mapToGlobal(point))
            finally:
                menu.deleteLater()

    def select(self, index):
        self.widget.setCurrentWidget(self.body_page)
        self._refresh_tabs()

    def reset(self):
        self.select(0)

    def refresh(self, *args):
        self._refresh_tabs()

    def dispose(self):
        pass


def sprite_files(folder, exclude=None):
    """Index metadata only; textures are loaded for the selected item only."""
    result = []
    for path in Path(folder).rglob("*.json"):
        if any(part.endswith(".editor") for part in path.parts) or (exclude and path.resolve() == Path(exclude).resolve()):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("format") == "mdframeedit" and not data.get("layers"):
                result.append(path.resolve())
        except (OSError, ValueError):
            continue
    return sorted(result, key=lambda path: path.relative_to(Path(folder).resolve()).as_posix().casefold())


class SpriteLibraryDialog(ScreenFitDialog):
    def __init__(self, editor, *, current=None):
        super().__init__(editor.window)
        self.editor = editor
        self.current = Path(current).resolve() if current else None
        self.selected_path = None
        self.setWindowTitle("Choose Sprite")
        self.resize(700, 470)
        layout = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel("Choose an independent sprite resource to inspect in the editor preview.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QtWidgets.QHBoxLayout()
        self.folder_label = QtWidgets.QLabel()
        self.folder_label.setWordWrap(True)
        row.addWidget(self.folder_label, 1)
        browse = QtWidgets.QPushButton("Choose Folder…")
        browse.clicked.connect(self.choose_folder)
        row.addWidget(browse)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(lambda: self.set_folder(self.folder))
        row.addWidget(refresh)
        layout.addLayout(row)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Find a sprite…")
        self.search.textChanged.connect(self.filter_items)
        layout.addWidget(self.search)
        content = QtWidgets.QHBoxLayout()
        self.list = QtWidgets.QListWidget()
        self.list.currentItemChanged.connect(self.show_sprite)
        self.list.itemDoubleClicked.connect(lambda _: self.accept() if self.selected_path else None)
        content.addWidget(self.list, 1)
        preview_column = QtWidgets.QVBoxLayout()
        self.preview = QtWidgets.QLabel()
        self.preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(180, 180)
        preview_column.addWidget(self.preview, 1)
        self.details = QtWidgets.QLabel()
        self.details.setWordWrap(True)
        preview_column.addWidget(self.details)
        content.addLayout(preview_column, 1)
        layout.addLayout(content, 1)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Use Sprite")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        saved = editor.settings.value("sharedSpriteFolder", "")
        self.set_folder(Path(saved) if saved and Path(saved).is_dir() else self.current.parent if self.current else Path.cwd())

    def choose_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Shared sprite folder", str(self.folder))
        if folder:
            self.set_folder(Path(folder))

    def set_folder(self, folder):
        self.folder = Path(folder).resolve()
        self.folder_label.setText(str(self.folder))
        self.editor.settings.setValue("sharedSpriteFolder", str(self.folder))
        self.list.clear()
        for path in sprite_files(self.folder, self.editor.file_name):
            item = QtWidgets.QListWidgetItem(path.relative_to(self.folder).as_posix())
            item.setData(QtCore.Qt.ItemDataRole.UserRole, path)
            item.setToolTip(str(path))
            self.list.addItem(item)
            if path == self.current:
                self.list.setCurrentItem(item)
        self.filter_items()
        if self.list.currentItem() is None:
            self.show_sprite(None)

    def filter_items(self, *_):
        query = self.search.text().casefold()
        for index in range(self.list.count()):
            item = self.list.item(index)
            item.setHidden(query not in item.text().casefold())
        current = self.list.currentItem()
        if current is None or current.isHidden():
            self.list.setCurrentItem(next((self.list.item(i) for i in range(self.list.count()) if not self.list.item(i).isHidden()), None))

    def show_sprite(self, item, *_):
        self.selected_path = None
        self.preview.clear()
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        if item is None:
            self.details.setText("No matching sprites. Choose a folder containing saved sprite JSON files.")
            return
        path = item.data(QtCore.Qt.ItemDataRole.UserRole)
        try:
            document = SpriteDocument.load(path)
            usable = [g for g in document.groups if any(s.frames for s in g.directions)]
            if not usable:
                raise FormatError("This sprite has no animation frames.")
            frame = next(f for s in usable[0].directions for f in s.frames)
            image = document.image_grid[frame.frame_index].convert("RGBA")
            qimage = QtGui.QImage(image.tobytes(), image.width, image.height, image.width*4, QtGui.QImage.Format.Format_RGBA8888).copy()
            self.preview.setPixmap(QtGui.QPixmap.fromImage(qimage).scaled(160, 160, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.FastTransformation))
            self.details.setText(f"{document.frame_width} × {document.frame_height} per cell\nAnimations: {', '.join(g.name for g in usable)}")
            self.selected_path = path
            self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        except (FormatError, OSError, ValueError) as error:
            self.details.setText(str(error))

    def accept(self):
        if self.selected_path is not None:
            super().accept()
