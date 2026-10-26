"""Keep editor dialogs inside the work area of their owner's monitor."""
from PySide6 import QtCore, QtWidgets


class ScreenFitDialog(QtWidgets.QDialog):
    def available_geometry(self):
        owner = self.parentWidget()
        screen = owner.screen() if owner is not None else self.screen()
        return screen.availableGeometry()

    def showEvent(self, event):
        super().showEvent(event)
        self.fit_to_screen()
        # Native borders and per-monitor DPI settle after the initial show.
        QtCore.QTimer.singleShot(0, self.fit_to_screen)

    def fit_to_screen(self):
        if not self.isVisible():
            return
        available = self.available_geometry().adjusted(8, 8, -8, -8)
        margins = self.windowHandle().frameMargins()
        client = available.adjusted(margins.left(), margins.top(),
                                    -margins.right(), -margins.bottom())
        layout = self.layout()
        if layout is not None:
            layout.activate()
            if (self.minimumSizeHint().height() > client.height()
                    or self.minimumSizeHint().width() > client.width()):
                # Reduce padding on small desktops before reducing useful content.
                for child_layout in (layout, *self.findChildren(QtWidgets.QLayout)):
                    child_layout.setContentsMargins(4, 4, 4, 4)
                    child_layout.setSpacing(3)
                    child_layout.invalidate()
                layout.activate()
        self.resize(min(self.width(), client.width()), min(self.height(), client.height()))
        owner = self.parentWidget()
        center = owner.frameGeometry().center() if owner is not None else available.center()
        width = self.width() + margins.left() + margins.right()
        height = self.height() + margins.top() + margins.bottom()
        x = max(available.left(), min(center.x() - width // 2, available.right() - width + 1))
        y = max(available.top(), min(center.y() - height // 2, available.bottom() - height + 1))
        self.move(x, y)
