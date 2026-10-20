from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt
from PIL import Image

from data import VERSION


class EditorButton(QtWidgets.QPushButton):
    def _buttonSize(self, preferred):
        metrics = self.fontMetrics()
        padding = self.style().pixelMetric(QtWidgets.QStyle.PixelMetric.PM_ButtonMargin,
                                          None, self)
        height = round(metrics.height() * (2 if preferred else 1.6)) + padding
        height = max(height, super().sizeHint().height())
        width = max(height, metrics.horizontalAdvance(self.text()) + 2 * padding)
        return QtCore.QSize(width, height)

    def sizeHint(self):
        return self._buttonSize(True)

    def minimumSizeHint(self):
        return self._buttonSize(False)


class IconButton(EditorButton):
    """Palette-aware action icons that do not depend on installed fonts."""

    def __init__(self, symbol, parent=None):
        super().__init__(parent)
        self.symbol = symbol

    def _iconPath(self):
        path = QtGui.QPainterPath()
        if self.symbol == 'add':
            path.moveTo(-7, 0)
            path.lineTo(7, 0)
            path.moveTo(0, -7)
            path.lineTo(0, 7)
        elif self.symbol in ('duplicate', 'copy'):
            path.moveTo(-5, 5)
            path.lineTo(-8, 5)
            path.lineTo(-8, -9)
            path.lineTo(4, -9)
            path.lineTo(4, -7)
            path.addRect(-3, -4, 12, 14)
            if self.symbol == 'copy':
                for y in (0, 4, 7):
                    path.moveTo(0, y)
                    path.lineTo(6, y)
        elif self.symbol == 'delete':
            path.moveTo(-6, -5)
            path.lineTo(-5, 9)
            path.lineTo(5, 9)
            path.lineTo(6, -5)
            path.moveTo(-8, -7)
            path.lineTo(8, -7)
            path.addRect(-3, -10, 6, 3)
            for x in (-2, 2):
                path.moveTo(x, -3)
                path.lineTo(x, 6)
        elif self.symbol == 'paste':
            path.moveTo(-3, -7)
            path.lineTo(-7, -7)
            path.lineTo(-7, 10)
            path.lineTo(7, 10)
            path.lineTo(7, -7)
            path.lineTo(3, -7)
            path.addRect(-3, -10, 6, 5)
            for y in (0, 4, 7):
                path.moveTo(-3, y)
                path.lineTo(3, y)
        elif self.symbol in ('up', 'down'):
            path.moveTo(-6, -2)
            path.lineTo(0, -8)
            path.lineTo(6, -2)
            path.moveTo(0, -8)
            path.lineTo(0, 8)
        elif self.symbol == 'restore':
            path.moveTo(-8, -4)
            path.cubicTo(-1, -11, 10, -6, 8, 3)
            path.cubicTo(7, 9, -2, 12, -7, 6)
            path.moveTo(-8, -10)
            path.lineTo(-8, -4)
            path.lineTo(-2, -4)
        elif self.symbol == 'hit':
            path.addEllipse(QtCore.QRectF(-6, -6, 12, 12))
            path.addEllipse(QtCore.QRectF(-1, -1, 2, 2))
            for x, y in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                path.moveTo(x * 7, y * 7)
                path.lineTo(x * 10, y * 10)
        elif self.symbol == 'rush':
            path.moveTo(1, -10)
            for x, y in ((-7, 2), (-1, 2), (-2, 10), (7, -3), (1, -3)):
                path.lineTo(x, y)
            path.closeSubpath()
        elif self.symbol == 'return':
            path.moveTo(7, -9)
            path.lineTo(7, 0)
            path.quadTo(7, 6, 0, 6)
            path.lineTo(-8, 6)
            path.moveTo(-3, 1)
            path.lineTo(-8, 6)
            path.lineTo(-3, 11)
        return path

    def paintEvent(self, event):
        super().paintEvent(event)
        option = QtWidgets.QStyleOptionButton()
        self.initStyleOption(option)
        contents = QtCore.QRectF(self.style().subElementRect(
            QtWidgets.QStyle.SubElement.SE_PushButtonContents, option, self))
        center = contents.center()
        if option.state & (QtWidgets.QStyle.StateFlag.State_Sunken | QtWidgets.QStyle.StateFlag.State_On):
            center += QtCore.QPointF(
                self.style().pixelMetric(QtWidgets.QStyle.PixelMetric.PM_ButtonShiftHorizontal, option, self),
                self.style().pixelMetric(QtWidgets.QStyle.PixelMetric.PM_ButtonShiftVertical, option, self))
        scale = min(contents.width(), contents.height()) * 0.7 / 24
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.translate(center)
        painter.scale(scale, scale)
        if self.symbol == 'down':
            painter.rotate(180)
        painter.setPen(QtGui.QPen(option.palette.buttonText().color(), 1.8,
                                 Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawPath(self._iconPath())


class DirectionButton(EditorButton):
    """A native button with a vector arrow, independent of installed fonts."""

    def __init__(self, angle, parent=None):
        super().__init__(parent)
        self._angle = angle

    def paintEvent(self, event):
        super().paintEvent(event)
        option = QtWidgets.QStyleOptionButton()
        self.initStyleOption(option)
        contents = QtCore.QRectF(self.style().subElementRect(
            QtWidgets.QStyle.SubElement.SE_PushButtonContents, option, self))
        center = contents.center()
        if option.state & (QtWidgets.QStyle.StateFlag.State_Sunken |
                           QtWidgets.QStyle.StateFlag.State_On):
            center += QtCore.QPointF(
                self.style().pixelMetric(QtWidgets.QStyle.PixelMetric.PM_ButtonShiftHorizontal, option, self),
                self.style().pixelMetric(QtWidgets.QStyle.PixelMetric.PM_ButtonShiftVertical, option, self))
        extent = min(contents.width(), contents.height())
        radius = extent * 0.3
        arrow = QtGui.QPainterPath()
        arrow.moveTo(0, radius)
        arrow.lineTo(0, -radius)
        arrow.moveTo(-radius * 0.65, -radius * 0.35)
        arrow.lineTo(0, -radius)
        arrow.lineTo(radius * 0.65, -radius * 0.35)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.translate(center)
        painter.rotate(self._angle)
        painter.setPen(QtGui.QPen(option.palette.buttonText().color(), max(1.25, extent * 0.065),
                                 Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                 Qt.PenJoinStyle.RoundJoin))
        painter.drawPath(arrow)


class PlaybackButton(EditorButton):
    """Large play/pause shapes drawn with Qt, without platform font glyphs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._playing = False
        self.setPlaying(False)

    def setPlaying(self, playing):
        self._playing = bool(playing)
        description = 'Pause animation' if self._playing else 'Play animation'
        self.setAccessibleName(description)
        self.update()

    def sizeHint(self):
        extent = max(super().sizeHint().height(), round(self.fontMetrics().height() * 2.6))
        return QtCore.QSize(extent, extent)

    def minimumSizeHint(self):
        extent = max(super().minimumSizeHint().height(), round(self.fontMetrics().height() * 2.2))
        return QtCore.QSize(extent, extent)

    def paintEvent(self, event):
        super().paintEvent(event)
        option = QtWidgets.QStyleOptionButton()
        self.initStyleOption(option)
        contents = QtCore.QRectF(self.style().subElementRect(
            QtWidgets.QStyle.SubElement.SE_PushButtonContents, option, self))
        center = contents.center()
        if option.state & (QtWidgets.QStyle.StateFlag.State_Sunken | QtWidgets.QStyle.StateFlag.State_On):
            center += QtCore.QPointF(
                self.style().pixelMetric(QtWidgets.QStyle.PixelMetric.PM_ButtonShiftHorizontal, option, self),
                self.style().pixelMetric(QtWidgets.QStyle.PixelMetric.PM_ButtonShiftVertical, option, self))
        radius = min(contents.width(), contents.height()) * 0.35
        symbol = QtGui.QPainterPath()
        if self._playing:
            symbol.addRect(-radius * 0.8, -radius, radius * 0.55, radius * 2)
            symbol.addRect(radius * 0.25, -radius, radius * 0.55, radius * 2)
        else:
            symbol.moveTo(-radius * 0.8, -radius)
            symbol.lineTo(radius, 0)
            symbol.lineTo(-radius * 0.8, radius)
            symbol.closeSubpath()
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.translate(center)
        painter.fillPath(symbol, option.palette.buttonText())


class ClickableSlider(QtWidgets.QSlider):
    """Used so Slider Tick positions can be manually clicked on."""

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.orientation() == Qt.Orientation.Horizontal:
                newValue = self.minimum() + ((self.maximum() - self.minimum()) * event.position().x() / self.width())
            else:
                newValue = self.minimum() + ((self.maximum() - self.minimum()) * (self.height() - event.position().y()) / self.height())

            self.setValue(int(newValue))
            event.accept()
        super().mousePressEvent(event)


class SliderTickLabels(QtWidgets.QWidget):
    """Place labels at Qt's actual slider handle positions, including DPI scaling."""

    def __init__(self, slider, labels, parent=None):
        super().__init__(parent)
        self.slider = slider
        self.labels = labels

    def sizeHint(self):
        metrics = self.fontMetrics()
        return QtCore.QSize(sum(metrics.horizontalAdvance(label) for label in self.labels)
                            + metrics.height() * (len(self.labels) - 1), metrics.height())

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        option = QtWidgets.QStyleOptionSlider()
        self.slider.initStyleOption(option)
        painter = QtGui.QPainter(self)
        painter.setPen(self.palette().windowText().color())
        offset = self.slider.x() - self.x()
        for value, label in enumerate(self.labels, self.slider.minimum()):
            option.sliderPosition = value
            handle = self.slider.style().subControlRect(
                QtWidgets.QStyle.ComplexControl.CC_Slider, option,
                QtWidgets.QStyle.SubControl.SC_SliderHandle, self.slider)
            width = self.fontMetrics().horizontalAdvance(label)
            left = max(0, min(offset + handle.center().x() - width // 2, self.width() - width))
            painter.drawText(QtCore.QRect(left, 0, width, self.height()),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, label)


class ResponsiveEditorPanel(QtWidgets.QWidget):
    resized = QtCore.Signal()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QtCore.QEvent.Type.FontChange, QtCore.QEvent.Type.StyleChange):
            self.resized.emit()


class TextWidthListWidget(QtWidgets.QListWidget):
    """Reserve a useful text width without expanding with the window."""

    def __init__(self, parent=None, characters=None, sampleText=None):
        super().__init__(parent)
        self.characters = characters
        self.sampleText = sampleText
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Expanding)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)

    def sizeHint(self):
        metrics = self.fontMetrics()
        textWidth = (metrics.horizontalAdvance(self.sampleText) if self.sampleText is not None
                     else metrics.averageCharWidth() * self.characters)
        scrollWidth = self.style().pixelMetric(QtWidgets.QStyle.PixelMetric.PM_ScrollBarExtent)
        return QtCore.QSize(textWidth + scrollWidth + 2 * self.frameWidth() + 12, 100)

    def minimumSizeHint(self):
        return QtCore.QSize(self.sizeHint().width(), 48)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QtCore.QEvent.Type.FontChange, QtCore.QEvent.Type.StyleChange):
            self.updateGeometry()


class SheetFramePreview(QtWidgets.QLabel):
    """Pixel-aligned sheet artwork and attachment markers, redrawn on resize."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.frameImage = QtGui.QImage()
        self.artworkBounds = QtCore.QRectF()
        self.points = []
        self.pointsVisible = True
        self.pointsOpacity = 255
        self.includeWhitespace = False
        self.showBounds = False
        self.setMinimumSize(100, 80)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Ignored)

    def setFrame(self, image, points, visible, opacity):
        if image.cacheKey() != self.frameImage.cacheKey() and not image.isNull():
            rgba = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
            pixels = Image.frombytes('RGBA', (rgba.width(), rgba.height()), bytes(rgba.constBits()),
                                    'raw', 'RGBA', rgba.bytesPerLine(), 1)
            bounds = pixels.getchannel('A').getbbox()
            self.artworkBounds = (QtCore.QRectF(bounds[0], bounds[1], bounds[2] - bounds[0], bounds[3] - bounds[1])
                                  if bounds else QtCore.QRectF(image.rect()))
        self.frameImage = image
        self.points = points
        self.pointsVisible = visible
        self.pointsOpacity = opacity
        self.update()

    def setPixmap(self, pixmap):
        self.setFrame(pixmap.toImage(), [], self.pointsVisible, self.pointsOpacity)

    def clear(self):
        self.frameImage = QtGui.QImage()
        self.points = []
        self.update()

    def paintEvent(self, event):
        if self.frameImage.isNull():
            super().paintEvent(event)
            return
        cellBounds = QtCore.QRectF(self.frameImage.rect())
        bounds = QtCore.QRectF(self.artworkBounds)
        if self.pointsVisible:
            for x, y, color in self.points:
                markerBounds = QtCore.QRectF(x - 1, y - 1, 3, 3)
                bounds = bounds.united(markerBounds)
                cellBounds = cellBounds.united(markerBounds)
        available = QtCore.QRectF(self.contentsRect().adjusted(6, 6, -6, -6))
        cellScale = min(available.width() / cellBounds.width(), available.height() / cellBounds.height())
        artworkScale = min(available.width() / bounds.width(), available.height() / bounds.height()) * 0.9
        scale = cellScale if self.includeWhitespace else min(cellScale * 2, artworkScale)
        if self.includeWhitespace:
            bounds = cellBounds
        if scale >= 1:
            scale = int(scale)
        origin = available.center() - bounds.center() * scale
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.drawImage(QtCore.QRectF(origin.x(), origin.y(), self.frameImage.width() * scale,
                                       self.frameImage.height() * scale), self.frameImage)
        if self.pointsVisible:
            painter.setOpacity(self.pointsOpacity / 255)
            for x, y, color in self.points:
                pixel = QtCore.QRectF(origin.x() + x * scale, origin.y() + y * scale, scale, scale)
                painter.fillRect(pixel, QtGui.QColor(*color))
        if self.showBounds:
            painter.setOpacity(1)
            painter.setPen(QtGui.QPen(QtGui.QColor(255, 220, 0), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QtCore.QRectF(origin.x(), origin.y(), self.frameImage.width() * scale,
                                           self.frameImage.height() * scale))


class Ui_MainWindow(object):
    buttonActions = {
        'actionAddButton': ('add', 'Add action', 'Create a new animation action.'),
        'actionDuplicateButton': ('duplicate', 'Duplicate action',
                                  'Duplicate the selected action. Choosing an existing name replaces that action.'),
        'actionDeleteButton': ('delete', 'Delete action', 'Delete the selected action after confirmation.'),
        'frameUpReorderButton': ('up', 'Move frame up', 'Move the selected frame earlier in the sequence.'),
        'frameDownReorderButton': ('down', 'Move frame down', 'Move the selected frame later in the sequence.'),
        'frameDuplicateButton': ('duplicate', 'Duplicate frame', 'Append a copy of the selected frame to the sequence.'),
        'frameDeleteButton': ('delete', 'Delete frame', 'Remove the selected frame from the sequence.'),
        'defaultFrameButton': ('restore', 'Restore frame',
                               'Restore the selected frame\'s sheet index, duration, flip and offsets to their last saved values.'),
        'copySequenceButton': ('copy', 'Copy sequence', 'Copy the entire current frame sequence.'),
        'pasteSequenceButton': ('paste', 'Paste sequence',
                                'Replace the current sequence with the copied sequence. Single-direction actions update all directions.'),
        'hitPointButton': ('hit', 'Set hit frame', 'Set the selected frame as the hit frame. Click again to clear it.'),
        'rushPointButton': ('rush', 'Set rush frame', 'Set the selected frame as the rush frame. Click again to clear it.'),
        'returnPointButton': ('return', 'Set return frame', 'Set the selected frame as the return frame. Click again to clear it.'),
    }

    def setupUi(self, MainWindow):
        MainWindow.setObjectName("MainWindow")
        MainWindow.resize(1040, 760)
        self.centralwidget = QtWidgets.QWidget(MainWindow)
        self.centralwidget.setObjectName("centralwidget")
        self.verticalLayout_8 = QtWidgets.QVBoxLayout(self.centralwidget)

        def makeLayout(layoutType, parent=None):
            layout = layoutType(parent) if parent is not None else layoutType()
            if parent is None:
                layout.setContentsMargins(0, 0, 0, 0)
            return layout

        def makeButton(name, parent):
            button = IconButton(self.buttonActions[name][0], parent)
            button.setObjectName(name)
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                 QtWidgets.QSizePolicy.Policy.Maximum)
            setattr(self, name, button)
            return button

        def makeSpinBox(name, parent, minimum=0):
            spinBox = QtWidgets.QSpinBox(parent)
            spinBox.setObjectName(name)
            spinBox.setMinimum(minimum)
            setattr(self, name, spinBox)
            return spinBox

        def makeOpacityControls(name, parent):
            label = QtWidgets.QLabel('Opacity: 100%', parent)
            slider = ClickableSlider(Qt.Orientation.Horizontal, parent)
            slider.setRange(0, 255)
            slider.setValue(255)
            slider.setAccessibleName(name.capitalize() + ' preview opacity')
            label.setBuddy(slider)
            setattr(self, name + 'OpacityLabel', label)
            setattr(self, name + 'OpacitySlider', slider)
            return label, slider

        self.mainSplitter = QtWidgets.QSplitter(Qt.Orientation.Vertical, self.centralwidget)
        self.mainSplitter.setObjectName("mainSplitter")
        self.mainSplitter.setChildrenCollapsible(False)
        self.mainSplitter.setHandleWidth(6)
        self.verticalLayout_8.addWidget(self.mainSplitter)
        self.libraryPanel = QtWidgets.QWidget()
        self.libraryPanel.setObjectName("libraryPanel")
        self.horizontalLayout_8 = makeLayout(QtWidgets.QHBoxLayout, self.libraryPanel)
        self.horizontalLayout_8.setContentsMargins(0, 0, 0, 0)

        self.groupBox_4 = QtWidgets.QGroupBox(self.libraryPanel)
        self.groupBox_4.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Expanding)
        self.actionLibraryLayout = makeLayout(QtWidgets.QHBoxLayout, self.groupBox_4)
        self.actionListWidget = TextWidthListWidget(self.groupBox_4, characters=24)
        self.actionLibraryLayout.addWidget(self.actionListWidget)
        self.actionButtonsLayout = makeLayout(QtWidgets.QVBoxLayout)
        for name in ('actionDuplicateButton', 'actionDeleteButton', 'actionAddButton'):
            button = makeButton(name, self.groupBox_4)
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Maximum, QtWidgets.QSizePolicy.Policy.Maximum)
            self.actionButtonsLayout.addWidget(button)
        self.actionButtonsLayout.addStretch(1)
        self.actionLibraryLayout.addLayout(self.actionButtonsLayout)
        self.horizontalLayout_8.addWidget(self.groupBox_4)

        self.groupBox_5 = QtWidgets.QGroupBox(self.libraryPanel)
        self.horizontalLayout_10 = makeLayout(QtWidgets.QHBoxLayout, self.groupBox_5)
        self.sheetLibraryPanel = QtWidgets.QWidget(self.groupBox_5)
        self.sheetLibraryLayout = makeLayout(QtWidgets.QVBoxLayout, self.sheetLibraryPanel)
        self.sheetLibraryLayout.setContentsMargins(0, 0, 0, 0)
        self.sheetFramesGroupBox = QtWidgets.QGroupBox(self.sheetLibraryPanel)
        self.sheetFramesLayout = makeLayout(QtWidgets.QVBoxLayout, self.sheetFramesGroupBox)
        self.loadedSheetFrameList = TextWidthListWidget(self.sheetFramesGroupBox, sampleText='Frame 100')
        self.sheetFramesLayout.addWidget(self.loadedSheetFrameList)
        self.sheetLibraryLayout.addWidget(self.sheetFramesGroupBox, 1)
        self.horizontalLayout_10.addWidget(self.sheetLibraryPanel)
        self.sheetImagePanel = QtWidgets.QWidget(self.groupBox_5)
        self.sheetImageLayout = makeLayout(QtWidgets.QVBoxLayout, self.sheetImagePanel)
        self.sheetImageLayout.setContentsMargins(0, 0, 0, 0)
        self.frame = QtWidgets.QFrame(self.sheetImagePanel)
        self.frame.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self.horizontalLayout_12 = makeLayout(QtWidgets.QHBoxLayout, self.frame)
        self.horizontalLayout_12.setContentsMargins(0, 0, 0, 0)
        self.sheetFramePicture = SheetFramePreview(self.frame)
        self.horizontalLayout_12.addWidget(self.sheetFramePicture)
        self.sheetImageLayout.addWidget(self.frame, 1)
        self.horizontalLayout_10.addWidget(self.sheetImagePanel, 1)
        self.actionPointsGroupBox = QtWidgets.QGroupBox(self.groupBox_5)
        self.actionPointsLayout = makeLayout(QtWidgets.QVBoxLayout, self.actionPointsGroupBox)
        self.sheetFrameScopeLabel = QtWidgets.QLabel(self.actionPointsGroupBox)
        self.actionPointsLayout.addWidget(self.sheetFrameScopeLabel)
        self.actionPointsGrid = makeLayout(QtWidgets.QGridLayout)
        self.actionPointXLabel = QtWidgets.QLabel('X:', self.actionPointsGroupBox)
        self.actionPointYLabel = QtWidgets.QLabel('Y:', self.actionPointsGroupBox)
        self.actionPointsGrid.addWidget(self.actionPointXLabel, 0, 1)
        self.actionPointsGrid.addWidget(self.actionPointYLabel, 0, 2)
        self.actionPointSpinBoxes = {}
        for row, pointName, title in ((1, 'leftHand', 'Left hand'), (2, 'rightHand', 'Right hand'),
                                      (3, 'head', 'Head'), (4, 'center', 'Center')):
            self.actionPointsGrid.addWidget(QtWidgets.QLabel(title, self.actionPointsGroupBox), row, 0)
            spinBoxes = []
            for column, axis in ((1, 'X'), (2, 'Y')):
                spinBox = makeSpinBox(pointName + axis + 'SpinBox', self.actionPointsGroupBox, -32768)
                spinBox.setMaximum(32767)
                spinBox.setEnabled(False)
                spinBox.setAccessibleName(title + ' ' + axis + ' offset')
                self.actionPointsGrid.addWidget(spinBox, row, column)
                spinBoxes.append(spinBox)
            self.actionPointSpinBoxes[pointName] = tuple(spinBoxes)
        self.actionPointsGrid.setColumnStretch(1, 1)
        self.actionPointsGrid.setColumnStretch(2, 1)
        self.actionPointsLayout.addLayout(self.actionPointsGrid)
        self.actionPointsLayout.addStretch(1)
        self.horizontalLayout_10.addWidget(self.actionPointsGroupBox)
        self.horizontalLayout_8.addWidget(self.groupBox_5, 1)
        self.mainSplitter.addWidget(self.libraryPanel)

        self.editorPanel = ResponsiveEditorPanel()
        self.editorLayout = makeLayout(QtWidgets.QGridLayout, self.editorPanel)
        self.editorLayout.setContentsMargins(0, 0, 0, 0)
        self.editorLayout.setRowStretch(0, 1)
        self.editorLayout.setColumnStretch(0, 1)
        self.previewSplitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        self.previewSplitter.setChildrenCollapsible(False)
        self.previewSplitter.setHandleWidth(6)
        self.verticalFrame_3 = QtWidgets.QFrame()
        self.verticalFrame_3.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self.gridLayout = makeLayout(QtWidgets.QGridLayout, self.verticalFrame_3)
        self.gridLayout.setContentsMargins(6, 6, 6, 6)
        self.gridLayout.setRowStretch(0, 1)
        self.horizontalLayout_11 = makeLayout(QtWidgets.QHBoxLayout)
        self.playButton = PlaybackButton(self.verticalFrame_3)
        self.playButton.setSizePolicy(QtWidgets.QSizePolicy.Policy.Maximum, QtWidgets.QSizePolicy.Policy.Maximum)
        self.playButton.setFlat(True)
        self.horizontalLayout_11.addWidget(self.playButton)
        self.frameSlider = ClickableSlider(Qt.Orientation.Horizontal, self.verticalFrame_3)
        self.frameSlider.setMaximum(2)
        self.frameSlider.setPageStep(13)
        self.frameSlider.setTickPosition(QtWidgets.QSlider.TickPosition.TicksAbove)
        self.horizontalLayout_11.addWidget(self.frameSlider, 1)
        self.gridLayout.addLayout(self.horizontalLayout_11, 1, 0)
        self.viewControlsPanel = QtWidgets.QWidget(self.editorPanel)
        self.viewControlsLayout = makeLayout(QtWidgets.QGridLayout, self.viewControlsPanel)
        self.viewControlsLayout.setContentsMargins(0, 0, 0, 0)
        self.viewControlsLayout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)
        self.groupBox_6 = QtWidgets.QGroupBox(self.verticalFrame_3)
        self.verticalLayout_9 = makeLayout(QtWidgets.QGridLayout, self.groupBox_6)
        self.animationSpeedLabel = QtWidgets.QLabel(self.groupBox_6)
        self.verticalLayout_9.addWidget(self.animationSpeedLabel, 0, 0)
        self.animationSpeedSlider = ClickableSlider(Qt.Orientation.Horizontal, self.groupBox_6)
        self.animationSpeedSlider.setMaximum(4)
        self.animationSpeedSlider.setValue(3)
        self.animationSpeedSlider.setTickPosition(QtWidgets.QSlider.TickPosition.TicksBelow)
        self.verticalLayout_9.addWidget(self.animationSpeedSlider, 0, 1)
        self.verticalLayout_9.setColumnStretch(1, 1)
        self.speedTickLabels = SliderTickLabels(self.animationSpeedSlider,
                                               ('0.1x', '0.25x', '0.5x', '1x', '2x'), self.groupBox_6)
        self.verticalLayout_9.addWidget(self.speedTickLabels, 1, 1)
        self.gridLayout.addWidget(self.groupBox_6, 2, 0)
        self.previewGroupBox = QtWidgets.QGroupBox(self.viewControlsPanel)
        self.previewLayout = makeLayout(QtWidgets.QHBoxLayout, self.previewGroupBox)
        self.previewLayout.setContentsMargins(9, 0, 9, 6)
        self.previewLayout.setSpacing(4)
        for name, title in (('sprite', 'Sprite'), ('shadow', 'Shadow'), ('actionPoints', 'Action Points')):
            group = QtWidgets.QGroupBox(title, self.previewGroupBox)
            groupLayout = makeLayout(QtWidgets.QVBoxLayout, group)
            groupLayout.setContentsMargins(6, 0, 6, 6)
            label, slider = makeOpacityControls(name, group)
            label.setText('100%')
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            slider.setAccessibleName(title + ' preview opacity')
            opacityRow = makeLayout(QtWidgets.QHBoxLayout)
            opacityRow.addWidget(QtWidgets.QLabel('Opacity', group))
            opacityRow.addStretch(1)
            opacityRow.addWidget(label)
            groupLayout.addLayout(opacityRow)
            groupLayout.addWidget(slider)
            if name == 'shadow':
                self.previewShadowCheckBox = QtWidgets.QCheckBox('Preview', group)
                self.shadowPreviewOptionsLayout = makeLayout(QtWidgets.QHBoxLayout)
                self.shadowPreviewOptionsLayout.addWidget(self.previewShadowCheckBox)
                self.shadowSizeComboBox = QtWidgets.QComboBox(group)
                self.shadowSizeComboBox.addItems(['Small', 'Medium', 'Large'])
                self.shadowSizeComboBox.setAccessibleName('Shadow size')
                self.shadowPreviewOptionsLayout.addWidget(self.shadowSizeComboBox, 1)
                groupLayout.addLayout(self.shadowPreviewOptionsLayout)
            elif name == 'actionPoints':
                self.showActionPointsCheckBox = QtWidgets.QCheckBox(group)
                self.showActionPointsCheckBox.setChecked(True)
                self.showActionPointsCheckBox.setAccessibleName('Show action points')
                groupLayout.addWidget(self.showActionPointsCheckBox)
            groupLayout.addStretch(1)
            setattr(self, name + 'PreviewGroupBox', group)
            self.previewLayout.addWidget(group, 1)
        self.previewSplitter.addWidget(self.verticalFrame_3)

        self.inspectorPanel = QtWidgets.QGroupBox()
        self.inspectorPanel.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,
                                          QtWidgets.QSizePolicy.Policy.Maximum)
        self.gridLayout_3 = makeLayout(QtWidgets.QGridLayout, self.inspectorPanel)
        self.directionGroupBox = QtWidgets.QGroupBox(self.inspectorPanel)
        self.gridLayout_2 = makeLayout(QtWidgets.QGridLayout, self.directionGroupBox)
        for name, row, column, angle in (
                ('buttonUpLeft', 0, 0, -45), ('buttonUp', 0, 1, 0), ('buttonUpRight', 0, 2, 45),
                ('buttonLeft', 1, 0, 270), ('buttonRight', 1, 2, 90),
                ('buttonDownLeft', 2, 0, 225), ('buttonDown', 2, 1, 180), ('buttonDownRight', 2, 2, 135)):
            button = DirectionButton(angle, self.directionGroupBox)
            button.setObjectName(name)
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Maximum, QtWidgets.QSizePolicy.Policy.Maximum)
            setattr(self, name, button)
            self.gridLayout_2.addWidget(button, row, column)
        self.shadowOffsetScopeLabel = QtWidgets.QLabel(self.inspectorPanel)
        self.frameHeaderLayout = makeLayout(QtWidgets.QHBoxLayout)
        self.frameHeaderLayout.addStretch(1)
        self.frameHeaderLayout.addWidget(self.shadowOffsetScopeLabel)
        self.gridLayout_3.addLayout(self.frameHeaderLayout, 0, 0, 1, 2)
        self.gridLayout_3.addWidget(self.directionGroupBox, 1, 0, Qt.AlignmentFlag.AlignTop)
        self.groupBox_7 = QtWidgets.QGroupBox(self.inspectorPanel)
        self.verticalLayout_3 = makeLayout(QtWidgets.QVBoxLayout, self.groupBox_7)
        self.frameForm = makeLayout(QtWidgets.QFormLayout)
        self.frameForm.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.label_6 = QtWidgets.QLabel(self.groupBox_7)
        self.frameIndexSpinBox = makeSpinBox('frameIndexSpinBox', self.groupBox_7)
        self.frameForm.addRow(self.label_6, self.frameIndexSpinBox)
        self.label = QtWidgets.QLabel(self.groupBox_7)
        self.durationSpinBox = makeSpinBox('durationSpinBox', self.groupBox_7)
        self.frameForm.addRow(self.label, self.durationSpinBox)
        self.verticalLayout_3.addLayout(self.frameForm)
        self.mirroredCheckbox = QtWidgets.QCheckBox(self.groupBox_7)
        self.verticalLayout_3.addWidget(self.mirroredCheckbox)
        self.gridLayout_3.addWidget(self.groupBox_7, 1, 1, Qt.AlignmentFlag.AlignTop)
        self.frameDataPanel = QtWidgets.QWidget(self.inspectorPanel)
        self.frameDataLayout = makeLayout(QtWidgets.QHBoxLayout, self.frameDataPanel)
        self.frameDataLayout.setContentsMargins(0, 0, 0, 0)
        for name, title in (('sprite', 'Sprite Offset'), ('shadow', 'Shadow Offset')):
            group = QtWidgets.QGroupBox(title, self.frameDataPanel)
            offsetsGroup = group
            form = makeLayout(QtWidgets.QFormLayout, offsetsGroup)
            form.setContentsMargins(6, 0, 6, 6)
            form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            for axis, labelName, spinName in (
                    ('X:', 'label_3' if name == 'sprite' else 'label_2', 'xSpinBox' if name == 'sprite' else 'xShadowSpinbox'),
                    ('Y:', 'label_4' if name == 'sprite' else 'label_5', 'ySpinBox' if name == 'sprite' else 'yShadowSpinBox')):
                label = QtWidgets.QLabel(axis, offsetsGroup)
                setattr(self, labelName, label)
                spinBox = makeSpinBox(spinName, offsetsGroup, -32768)
                spinBox.setMaximum(32767)
                spinBox.setEnabled(False)
                form.addRow(label, spinBox)
            setattr(self, 'groupBox_2' if name == 'sprite' else 'groupBox_8', group)
            setattr(self, name + 'OffsetsGroupBox', offsetsGroup)
            setattr(self, 'offsetLayout' if name == 'sprite' else 'shadowOffsetLayout', form)
            self.frameDataLayout.addWidget(group, 1, Qt.AlignmentFlag.AlignTop)
        self.gridLayout_3.addWidget(self.frameDataPanel, 2, 0, 1, 2, Qt.AlignmentFlag.AlignTop)
        self.gridLayout_3.setColumnStretch(0, 1)
        self.gridLayout_3.setColumnStretch(1, 1)

        self.groupBox_3 = QtWidgets.QGroupBox(self.editorPanel)
        self.groupBox_3.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Expanding)
        self.sequenceLayout = makeLayout(QtWidgets.QVBoxLayout, self.groupBox_3)
        self.sequenceBody = makeLayout(QtWidgets.QHBoxLayout)
        self.animationFrameList = TextWidthListWidget(self.groupBox_3, characters=10)
        self.sequenceBody.addWidget(self.animationFrameList, 1)
        self.sequenceButtons = makeLayout(QtWidgets.QGridLayout)
        self.sequenceButtonWidgets = []
        for name in ('frameUpReorderButton', 'frameDownReorderButton', 'frameDuplicateButton',
                     'frameDeleteButton', 'defaultFrameButton'):
            button = makeButton(name, self.groupBox_3)
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Maximum, QtWidgets.QSizePolicy.Policy.Maximum)
            self.sequenceButtons.addWidget(button, len(self.sequenceButtonWidgets), 0)
            self.sequenceButtonWidgets.append(button)
        self.sequenceButtons.setRowStretch(5, 1)
        self.sequenceBody.addLayout(self.sequenceButtons)
        self.sequenceLayout.addLayout(self.sequenceBody, 1)
        self.sequenceClipboard = makeLayout(QtWidgets.QHBoxLayout)
        self.sequenceClipboard.addWidget(makeButton('copySequenceButton', self.groupBox_3))
        self.sequenceClipboard.addWidget(makeButton('pasteSequenceButton', self.groupBox_3))
        self.sequenceLayout.addLayout(self.sequenceClipboard)
        self.sequenceMarkers = makeLayout(QtWidgets.QHBoxLayout)
        for name in ('hitPointButton', 'returnPointButton', 'rushPointButton'):
            self.sequenceMarkers.addWidget(makeButton(name, self.groupBox_3))
        self.sequenceLayout.addLayout(self.sequenceMarkers)
        self.viewControlsLayout.addWidget(self.inspectorPanel, 0, 0)
        self.viewControlsLayout.addWidget(self.groupBox_3, 0, 1)
        self.viewControlsLayout.addWidget(self.previewGroupBox, 1, 0, 1, 2)
        self.viewControlsLayout.setRowStretch(0, 1)
        self.previewGroupBox.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,
                                          QtWidgets.QSizePolicy.Policy.Maximum)
        self.previewSplitter.addWidget(self.viewControlsPanel)
        self.previewSplitter.setStretchFactor(0, 1)
        self.previewSplitter.setStretchFactor(1, 0)
        self.editorLayout.addWidget(self.previewSplitter, 0, 0)
        self.mainSplitter.addWidget(self.editorPanel)
        self.mainSplitter.setStretchFactor(0, 0)
        self.mainSplitter.setStretchFactor(1, 1)
        self.mainSplitter.setSizes([220, 520])
        self.previewSplitter.setSizes([500, 350])
        self.sequenceStacked = False
        self.updatingEditorLayout = False
        self.editorPanel.resized.connect(self.updateEditorLayout)
        MainWindow.setCentralWidget(self.centralwidget)
        self.menubar = QtWidgets.QMenuBar(MainWindow)
        self.menubar.setGeometry(QtCore.QRect(0, 0, 623, 21))
        self.menubar.setObjectName("menubar")
        self.menuFile = QtWidgets.QMenu(self.menubar)
        self.menuFile.setObjectName("menuFile")
        self.menuRecent = QtWidgets.QMenu(self.menuFile)
        self.menuRecent.setObjectName("menuRecent")
        self.menuExport = QtWidgets.QMenu(self.menuFile)
        self.menuExport.setObjectName("menuExport")
        self.menuBatch = QtWidgets.QMenu(self.menubar)
        self.menuBatch.setObjectName("menuBatch")
        MainWindow.setMenuBar(self.menubar)
        self.statusBar = QtWidgets.QStatusBar(MainWindow)
        self.statusBar.setObjectName("statusBar")
        MainWindow.setStatusBar(self.statusBar)
        self.actionLoad = QtGui.QAction(MainWindow)
        self.actionLoad.setObjectName("actionLoad")
        self.actionSave = QtGui.QAction(MainWindow)
        self.actionSave.setObjectName("actionSave")
        self.actionExit = QtGui.QAction(MainWindow)
        self.actionExit.setObjectName("actionExit")
        self.actionNone = QtGui.QAction(MainWindow)
        self.actionNone.setEnabled(False)
        self.actionNone.setObjectName("actionNone")
        self.actionSave_As = QtGui.QAction(MainWindow)
        self.actionSave_As.setObjectName("actionSave_As")
        self.actionAdd_Action_Copy = QtGui.QAction(MainWindow)
        self.actionAdd_Action_Copy.setObjectName("actionAdd_Action_Copy")
        self.actionTrim_Copies = QtGui.QAction(MainWindow)
        self.actionTrim_Copies.setCheckable(True)
        self.actionTrim_Copies.setChecked(True)
        self.actionTrim_Copies.setObjectName("actionTrim_Copies")
        self.actionCollapse_Singles = QtGui.QAction(MainWindow)
        self.actionCollapse_Singles.setCheckable(True)
        self.actionCollapse_Singles.setChecked(True)
        self.actionCollapse_Singles.setObjectName("actionCollapse_Singles")
        self.actionExportAll_Animations = QtGui.QAction(MainWindow)
        self.actionExportAll_Animations.setObjectName("actionExportAll_Animations")
        self.actionExportSingle_Animation = QtGui.QAction(MainWindow)
        self.actionExportSingle_Animation.setObjectName("actionExportSingle_Animation")
        self.menuExport.addAction(self.actionExportAll_Animations)
        self.menuExport.addAction(self.actionExportSingle_Animation)
        self.menuFile.addAction(self.actionLoad)
        self.menuFile.addAction(self.menuRecent.menuAction())
        self.menuFile.addSeparator()
        self.menuFile.addAction(self.menuExport.menuAction())
        self.menuFile.addSeparator()
        self.menuFile.addAction(self.actionSave)
        self.menuFile.addAction(self.actionSave_As)
        self.menuFile.addAction(self.actionTrim_Copies)
        self.menuFile.addAction(self.actionCollapse_Singles)
        self.menuFile.addSeparator()
        self.menuFile.addAction(self.actionExit)
        self.menuFile.addSeparator()
        self.menuBatch.addAction(self.actionAdd_Action_Copy)
        self.menubar.addAction(self.menuFile.menuAction())
        self.menubar.addAction(self.menuBatch.menuAction())

        self.retranslateUi(MainWindow)
        for name, obj in vars(self).items():
            if isinstance(obj, QtCore.QObject) and not obj.objectName():
                obj.setObjectName(name)
        QtCore.QMetaObject.connectSlotsByName(MainWindow)

    def updateEditorLayout(self):
        """Use spare height for the sequence and spare width for the sprite canvas."""
        if self.updatingEditorLayout:
            return
        self.updatingEditorLayout = True
        try:
            fontHeight = self.viewControlsPanel.fontMetrics().height()
            buttonHeight = max(button.minimumSizeHint().height() for button in self.sequenceButtonWidgets)
            margins = self.sequenceLayout.contentsMargins()
            frameMargins = self.groupBox_3.contentsMargins()
            sequenceFooterHeight = (self.sequenceClipboard.minimumSize().height()
                                    + self.sequenceMarkers.minimumSize().height()
                                    + margins.top() + margins.bottom()
                                    + frameMargins.top() + frameMargins.bottom()
                                    + 2 * self.sequenceLayout.spacing())
            sequenceHeight = 2 * buttonHeight + self.sequenceButtons.verticalSpacing() + sequenceFooterHeight
            self.stackedSequenceMinimumHeight = (self.inspectorPanel.minimumSizeHint().height()
                                                + self.previewGroupBox.minimumSizeHint().height()
                                                + sequenceHeight + 2 * self.viewControlsLayout.spacing())
            threshold = self.stackedSequenceMinimumHeight + (0 if self.sequenceStacked else fontHeight // 4)
            stacked = self.editorPanel.height() >= threshold
            normalSequenceWidth = max(
                self.animationFrameList.minimumSizeHint().width()
                + max(button.minimumSizeHint().width() for button in self.sequenceButtonWidgets)
                + self.sequenceBody.spacing(), self.sequenceMarkers.minimumSize().width())
            normalSequenceWidth += margins.left() + margins.right() + frameMargins.left() + frameMargins.right()
            controlWidth = max(self.previewGroupBox.minimumSizeHint().width(),
                               self.inspectorPanel.minimumSizeHint().width()
                               + (0 if stacked else normalSequenceWidth + self.viewControlsLayout.spacing()))
            normalSequenceHeight = (5 * buttonHeight + 4 * self.sequenceButtons.verticalSpacing()
                                    + sequenceFooterHeight)
            controlHeight = (max(self.inspectorPanel.minimumSizeHint().height(), normalSequenceHeight)
                             + self.previewGroupBox.minimumSizeHint().height() + self.viewControlsLayout.spacing())
            # Keep the compact minimum even when stacked so Restore can shrink the window.
            self.viewControlsPanel.setMinimumSize(controlWidth, controlHeight)
            self.viewControlsPanel.setMaximumWidth(controlWidth + 2 * fontHeight)
            if stacked == self.sequenceStacked:
                return
            self.sequenceStacked = stacked
            for widget in (self.inspectorPanel, self.groupBox_3, self.previewGroupBox):
                self.viewControlsLayout.removeWidget(widget)
            for row in range(3):
                self.viewControlsLayout.setRowStretch(row, 0)
            if stacked:
                self.viewControlsLayout.addWidget(self.inspectorPanel, 0, 0, 1, 2)
                self.viewControlsLayout.addWidget(self.groupBox_3, 1, 0, 1, 2)
                self.viewControlsLayout.addWidget(self.previewGroupBox, 2, 0, 1, 2)
                self.viewControlsLayout.setRowStretch(1, 1)
            else:
                self.viewControlsLayout.addWidget(self.inspectorPanel, 0, 0)
                self.viewControlsLayout.addWidget(self.groupBox_3, 0, 1)
                self.viewControlsLayout.addWidget(self.previewGroupBox, 1, 0, 1, 2)
                self.viewControlsLayout.setRowStretch(0, 1)
            for row in range(6):
                self.sequenceButtons.setRowStretch(row, 0)
            for index, button in enumerate(self.sequenceButtonWidgets):
                self.sequenceButtons.removeWidget(button)
                self.sequenceButtons.addWidget(button, index // 3 if stacked else index,
                                                index % 3 if stacked else 0)
            self.sequenceButtons.setRowStretch(2 if stacked else 5, 1)
            self.groupBox_3.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred if stacked
                                          else QtWidgets.QSizePolicy.Policy.Fixed,
                                          QtWidgets.QSizePolicy.Policy.Expanding)
            self.animationFrameList.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding if stacked
                                                   else QtWidgets.QSizePolicy.Policy.Fixed,
                                                   QtWidgets.QSizePolicy.Policy.Expanding)
            self.groupBox_3.show()
        finally:
            self.updatingEditorLayout = False

    def retranslateUi(self, MainWindow):
        _translate = QtCore.QCoreApplication.translate
        MainWindow.setWindowTitle(_translate("MainWindow", f"MD Frame Editor ({VERSION})"))
        self.groupBox_4.setTitle(_translate("MainWindow", "Action Animations"))
        for name, (symbol, label, description) in self.buttonActions.items():
            button = getattr(self, name)
            button.setAccessibleName(_translate("MainWindow", label))
            button.setToolTip(_translate("MainWindow", description))
        self.groupBox_5.setTitle(_translate("MainWindow", "Sprite Sheet"))
        self.sheetFramesGroupBox.setTitle(_translate("MainWindow", "Frames"))
        self.inspectorPanel.setTitle(_translate("MainWindow", "Animation Frame"))
        self.previewGroupBox.setTitle(_translate("MainWindow", "Preview"))
        self.groupBox_6.setTitle(_translate("MainWindow", "Animation Speed"))
        self.animationSpeedLabel.setText(_translate("MainWindow", "1x"))
        self.directionGroupBox.setTitle(_translate("MainWindow", "Direction"))
        for button, name in (
                (self.buttonUpLeft, 'Up-left'), (self.buttonUp, 'Up'), (self.buttonUpRight, 'Up-right'),
                (self.buttonLeft, 'Left'), (self.buttonRight, 'Right'),
                (self.buttonDownLeft, 'Down-left'), (self.buttonDown, 'Down'),
                (self.buttonDownRight, 'Down-right')):
            description = _translate("MainWindow", name)
            button.setAccessibleName(description)
        self.groupBox_7.setTitle(_translate("MainWindow", "Frame Data"))
        self.label_6.setText(_translate("MainWindow", "Sheet frame"))
        self.label.setText(_translate("MainWindow", "Duration"))
        self.mirroredCheckbox.setText(_translate("MainWindow", "Horizontal Flip"))
        self.groupBox_2.setTitle(_translate("MainWindow", "Sprite Offset"))
        self.label_3.setText(_translate("MainWindow", "X:"))
        self.label_4.setText(_translate("MainWindow", "Y:"))
        self.groupBox_8.setTitle(_translate("MainWindow", "Shadow Offset"))
        self.actionPointsGroupBox.setTitle(_translate("MainWindow", "Action Points"))
        self.sheetFrameScopeLabel.setText(_translate("MainWindow", "Current Frame: -"))
        self.shadowOffsetScopeLabel.setText(_translate("MainWindow", "Frame 0 of 0"))
        self.showActionPointsCheckBox.setText(_translate("MainWindow", "Show"))
        self.label_2.setText(_translate("MainWindow", "X:"))
        self.label_5.setText(_translate("MainWindow", "Y:"))
        self.groupBox_3.setTitle(_translate("MainWindow", "Animation Sequence"))
        self.menuFile.setTitle(_translate("MainWindow", "File"))
        self.menuRecent.setTitle(_translate("MainWindow", "Recent"))
        self.menuExport.setTitle(_translate("MainWindow", "Export"))
        self.menuBatch.setTitle(_translate("MainWindow", "Batch"))
        self.actionLoad.setText(_translate("MainWindow", "Open"))
        self.actionLoad.setShortcut(_translate("MainWindow", "Ctrl+O"))
        self.actionSave.setText(_translate("MainWindow", "Save"))
        self.actionSave.setShortcut(_translate("MainWindow", "Ctrl+S"))
        self.actionExit.setText(_translate("MainWindow", "Exit"))
        self.actionNone.setText(_translate("MainWindow", "None"))
        self.actionSave_As.setText(_translate("MainWindow", "Save As"))
        self.actionAdd_Action_Copy.setText(_translate("MainWindow", "Add Action Copy"))
        self.actionTrim_Copies.setText(_translate("MainWindow", "Trim Copies"))
        self.actionCollapse_Singles.setText(_translate("MainWindow", "Collapse Singles"))
        self.actionExportAll_Animations.setText(_translate("MainWindow", "Multi-Animation Sheets"))
        self.actionExportSingle_Animation.setText(_translate("MainWindow", "Single Animation Sheet"))
