# -*- coding: utf-8 -*-
import copy
import math
import os
import sys
import traceback
import xml.etree.ElementTree as ElementTree
from collections import defaultdict
from functools import partial
from typing import Optional, Tuple, Set
import pyglet
from PIL import Image, ImageDraw

pyglet.options["com_mta"] = False
import warnings

warnings.simplefilter("ignore", UserWarning)
sys.coinit_flags = 2

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import QSettings, QFileInfo
from PyQt5.QtGui import QWheelEvent, QPixmap, QImage, QKeyEvent
from PyQt5.QtWidgets import QFileDialog, QListWidgetItem, QInputDialog
from pyglet.gl import *
from pyglet.math import clamp

from data import *
from gui.batchadd import Ui_BatchCreateAction
from gui.editor import Ui_MainWindow
from utils import (TopLeftGrid, Camera, checkDuplicateImages, roundUpToMult, centerAndApplyOffset,
                   getActionPointsFromImage, getActionPointsFromPILImage, getShadowLocationFromPILImage,
                   createPlusImage, overlapColors)

pyglet.image.Texture.default_min_filter = GL_NEAREST
pyglet.image.Texture.default_mag_filter = GL_NEAREST


class LoadedSheetFrame(QListWidgetItem):
    def __init__(self, text, idx, image, label, editor: 'AnimationEditor'):
        super().__init__(text)
        self.image: pyglet.image.ImageData = image
        self.label = label
        self.editor = editor
        self.idx = idx

        data = self.image.get_image_data().get_data('RGBA', -self.image.width * 4)
        self.qim = QImage(data, image.width, image.height, QImage.Format.Format_RGBA8888).scaled(self.label.width(),
                                                                                                 self.label.height(),
                                                                                                 QtCore.Qt.KeepAspectRatio)
        self.pix = QPixmap.fromImage(self.qim)

    # Define the double-click event handler
    def mouseDoubleClickEvent(self, event):
        self.editor.addNewAnimationFrame(self.idx)

    def mouseClickEvent(self, event):
        self.label.setPixmap(self.pix)


class AnimGroupItem(QListWidgetItem):
    def __init__(self, animGroup: 'AnimGroup', editor: 'AnimationEditor'):
        self.animGroup = animGroup
        text = self._getAnimText()
        super().__init__(text)
        self.editor = editor

    def updateText(self):
        self.setText(self._getAnimText())

    def _getAnimText(self):
        text = f"{self.animGroup.idx if self.animGroup.idx >= 0 else '?'}. {self.animGroup.name}"
        if self.animGroup.copyName:
            text += f" [{self.animGroup.copyName}]"

        if self.animGroup.modified:
            text += " *"

        return text

    def mouseClickEvent(self, event):
        self.editor.currentAnimGroup = self.animGroup

        self.editor.currentSequence = self.animGroup.directions[self.editor.currentDirection]

        self.editor.clearAnimFrame()

        if self.editor.currentSequence.frames:
            self.editor.currentAnimFrame = self.editor.currentSequence.frames[0]

            self.editor.setSequenceList()

            self.editor.ui.animationFrameList.setCurrentRow(0)

            self.editor.setAnimFrameValues(self.editor.currentAnimFrame)

            self.editor.ui.statusBar.showMessage(f"Active action: {self.animGroup.name}.")



class AnimFrameItem(QListWidgetItem):
    def __init__(self, animFrame: AnimFrame, editor: 'AnimationEditor'):
        self.animFrame = animFrame
        self.editor = editor
        text = self._getText()
        super().__init__(text)

    def updateText(self):
        self.setText(self._getText())

    def _getText(self):
        text = f"#{self.animFrame.frameIndex}"

        if self.animFrame.idx == self.editor.currentAnimGroup.hitFrame:
            text += " (HF)"
        if self.animFrame.idx == self.editor.currentAnimGroup.rushFrame:
            text += " (RF)"
        if self.animFrame.idx == self.editor.currentAnimGroup.returnFrame:
            text += " (RTF)"

        return text

    def mouseClickEvent(self, event):
        self.editor.setAnimFrameValues(self.animFrame)

        if not self.editor.animating:
            if self.animFrame == self.editor.currentAnimFrame:
                return

            self.editor.currentAnimFrame = self.animFrame
            self.editor.ui.frameSlider.setValue(self.animFrame.idx)
            self.editor.setAnimation()


class ProxyStyle(QtWidgets.QProxyStyle):
    """Used so Slider Tick positions can be manually clicked on."""

    def styleHint(self, hint, opt=None, widget=None, returnData=None):
        res = super().styleHint(hint, opt, widget, returnData)
        if hint == self.SH_Slider_AbsoluteSetButtons:
            res |= QtCore.Qt.LeftButton
        return res


class PygletWidget(QtWidgets.QOpenGLWidget):
    _default_vertex_source = """#version 150 core
        in vec4 position;

        uniform WindowBlock
        {
            mat4 projection;
            mat4 view;
        } window;

        void main()
        {
            gl_Position = window.projection * window.view * position;
        }
    """
    _default_fragment_source = """#version 150 core
        out vec4 color;

        void main()
        {
            color = vec4(1.0, 0.0, 0.0, 1.0);
        }
    """

    def __init__(self, width, height, parent, mainWindow, editor: 'AnimationEditor'):
        super().__init__(parent)
        self.mainWindow = mainWindow
        self.setMinimumSize(width, height)
        self.editor = editor

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self._pyglet_update)
        self.timer.setInterval(0)
        self.timer.start()

        self.focusPoint = self.width() // 2, self.height() // 3

        self.camera = Camera(self, self.focusPoint)

        self.elapsed = 0

        self.setFocusPolicy(QtCore.Qt.StrongFocus)

    def keyPressEvent(self, event: QKeyEvent):
        super().keyPressEvent(event)

        if event.key() == QtCore.Qt.Key_O:
            for sprite in self.editor.apSprites:
                sprite.visible = not sprite.visible
        elif event.key() == QtCore.Qt.Key_S:
            if self.editor.shadow:
                self.editor.shadow.visible = not self.editor.shadow.visible

    def wheelEvent(self, event: QWheelEvent):
        super().wheelEvent(event)

        if event.modifiers() & QtCore.Qt.ControlModifier:
            if self.editor.sprite:
                if event.angleDelta().y() > 0:
                    self.editor.sprite.opacity = clamp(self.editor.sprite.opacity - 50, 0, 255)
                else:
                    self.editor.sprite.opacity = clamp(self.editor.sprite.opacity + 50, 0, 255)
        else:
            if event.angleDelta().y() > 0:
                self.camera.zoom *= 2.0
            else:
                self.camera.zoom /= 2.0

        self.view = pyglet.math.Mat4()
        event.accept()

    def _pyglet_update(self):
        # Tick the pyglet clock, so scheduled events can work.
        pyglet.clock.tick()

        # Force widget to update, otherwise paintGL will not be called.
        self.update()

    def paintGL(self):
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)

        with self.camera:
            self.batch.draw()

    def resizeGL(self, width, height):
        self.projection = pyglet.math.Mat4.orthogonal_projection(0, width, 0, height, -255, 255)

        self.viewport = 0, 0, width, height

        self.focusPoint = width // 2, height // 3
        self.camera.x, self.camera.y = self.focusPoint

        self.lines = [
            pyglet.shapes.Line(0, self.height() // 3, self.width(), self.height() // 3, batch=self.batch),
            pyglet.shapes.Line(self.width() // 2, 0, self.width() // 2, self.height(), batch=self.batch)
        ]

        if self.editor.sprite:
            self.editor.sprite.position = self.editor.getSpritePosition()
            self.editor.shadow.position = self.editor.getShadowPosition()

    def initializeGL(self):
        """Call anything that needs a context to be created."""

        self._projection_matrix = pyglet.math.Mat4()
        self._view_matrix = pyglet.math.Mat4()

        self.batch = pyglet.graphics.Batch()

        try:
            self._default_program = pyglet.graphics.shader.ShaderProgram(
                pyglet.graphics.shader.Shader(self._default_vertex_source, 'vertex'),
                pyglet.graphics.shader.Shader(self._default_fragment_source, 'fragment'))
        except:
            self.error_dialog = QtWidgets.QMessageBox()
            self.error_dialog.setWindowTitle("Error")
            self.error_dialog.setIcon(QtWidgets.QMessageBox.Critical)
            self.error_dialog.setText("Could not compile shader. Requires OpenGL 3.3 capability.")
            self.error_dialog.exec()
            sys.exit(app.exec_())

        self.ubo = self._default_program.uniform_blocks['WindowBlock'].create_ubo()

        glClearColor(0.5, 0.5, 0.5, 1.0)

        self.view = pyglet.math.Mat4()
        self.projection = pyglet.math.Mat4.orthogonal_projection(0, self.width(), 0, self.height(), -255, 255)
        self.viewport = 0, 0, self.width(), self.height()

    @property
    def viewport(self):
        return self._viewport

    @viewport.setter
    def viewport(self, values):
        self._viewport = values
        pr = 1.0
        x, y, w, h = values
        pyglet.gl.glViewport(int(x * pr), int(y * pr), int(w * pr), int(h * pr))

    @property
    def projection(self):
        return self._projection_matrix

    @projection.setter
    def projection(self, matrix: pyglet.math.Mat4):
        with self.ubo as window_block:
            window_block.projection[:] = matrix

        self._projection_matrix = matrix

    @property
    def view(self):
        return self._view_matrix

    @view.setter
    def view(self, matrix: pyglet.math.Mat4):

        with self.ubo as window_block:
            window_block.view[:] = matrix

        self._view_matrix = matrix


class BatchAddImplementation:
    def __init__(self, window: QtWidgets.QWidget, editor: 'AnimationEditor', ui: 'Ui_BatchCreateAction'):
        self.window = window
        self.ui = ui
        self.editor = editor

        self.ui.openDirectory.clicked.connect(lambda: self.openDirectory())

        self.loadedFrameData()

        self.ui.finalizeButtonBox.clicked.connect(self.finalizeClick)

    def apply(self):

        if self.ui.copyComboBox.currentText() == self.ui.actionComboBox.currentText():

            self.error_dialog = QtWidgets.QMessageBox()
            self.error_dialog.setWindowTitle("Warning")
            self.error_dialog.setIcon(QtWidgets.QMessageBox.Warning)
            self.error_dialog.setText("Both boxes cannot be the same name.")
            self.error_dialog.exec()

        else:
            if not self.ui.directoryLineEdit.text():
                return

            useAction: AnimGroup = self.ui.actionComboBox.currentData()
            copyAction: AnimGroup = self.ui.copyComboBox.currentData()

            overwrite = self.ui.overwriteCheckbox.isChecked()
            fulldata = self.ui.fullDataCheckbox.isChecked()

            _useIdx = self.ui.indexSpinBox.value()
            groupIdx = _useIdx if _useIdx >= 0 else useAction.idx
            collapse = self.editor.ui.actionCollapse_Singles.isChecked()

            ct = 0
            for dirpath, dirnames, filenames in os.walk(self.ui.directoryLineEdit.text()):
                for filename in filenames:
                    if filename == "FrameData.xml":
                        fileName = os.path.join(dirpath, filename)
                        data = ElementTree.parse(fileName)

                        root = data.getroot()

                        animsEl = root.find("Anims")

                        exists = False
                        write = False
                        for actionAnim in animsEl:
                            for animEl in actionAnim:
                                if animEl.tag == "Name":
                                    # Found one...
                                    if animEl.text.lower() == useAction.name.lower():
                                        exists = True

                                        if overwrite:
                                            # Clear existing anim.
                                            actionAnim.clear()
                                            if self.editor.createBaseAnimGroupXML(actionAnim, useAction.name, groupIdx,
                                                                               copyAction,
                                                                               trim=not fulldata,
                                                                               copyName=copyAction.name):
                                                self.editor.createSingleSheetFrameData(actionAnim, copyAction, collapse)

                                            write = True
                                        else:
                                            break

                        if not exists:
                            animEl = ElementTree.SubElement(animsEl, "Anim")
                            if self.editor.createBaseAnimGroupXML(animEl, useAction.name, groupIdx, copyAction,
                                                               trim=not fulldata,
                                                               copyName=copyAction.name):
                                self.editor.createSingleSheetFrameData(animEl, copyAction, collapse)
                            write = True

                        if write:
                            ElementTree.indent(root)

                            tree = ElementTree.ElementTree(root)
                            tree.write(fileName, encoding='utf-8', xml_declaration=True)

                            ct += 1

            error_dialog = QtWidgets.QMessageBox()
            error_dialog.setWindowTitle("Complete")
            error_dialog.setIcon(QtWidgets.QMessageBox.Information)
            error_dialog.setText(f"Operation completed with {ct} changes.")
            error_dialog.exec()

    def finalizeClick(self, button):
        role = self.ui.finalizeButtonBox.buttonRole(button)
        if role == QtWidgets.QDialogButtonBox.ApplyRole:
            self.apply()
        elif role == QtWidgets.QDialogButtonBox.RejectRole:
            self.window.hide()

    def loadedFrameData(self):
        self.ui.copyComboBox.clear()
        self.ui.actionComboBox.clear()

        self.fillActionList()
        self.fillCopyList()

    def fillActionList(self):
        for a in self.editor.groups:
            self.ui.actionComboBox.addItem(a.name, userData=a)

    def fillCopyList(self):
        for a in self.editor.groups:
            self.ui.copyComboBox.addItem(a.name, userData=a)

    def openDirectory(self):
        directory = QFileDialog.getExistingDirectory(self.window, "Select Directory")

        self.ui.directoryLineEdit.setText(directory)

REDUCE_RUSH_FRAMES = False

class AnimationEditor:
    shadowImage: Optional[pyglet.image.AbstractImage]
    sprite: Optional[pyglet.sprite.Sprite]
    animationSpeedSliderValues = (0.1, 0.25, 0.5, 1, 2)
    maxRecent = 5

    def __init__(self, app: QtWidgets.QApplication, window: QtWidgets.QMainWindow, ui: Ui_MainWindow):
        self.app = app
        self.window = window
        self.ui = ui
        self.groups: List[AnimGroup] = []
        self.fileName = ''
        self.loadedTree: Optional[ElementTree] = None
        self.batchAddImplem: Optional[BatchAddImplementation] = None

        self.settings = QSettings('MDFrameEditor', 'Frame Editor')
        self.recentFiles = self.settings.value('recent', [])

        self.enableTrim = self.settings.value('trim', True, bool)
        self.enableCollapse = self.settings.value('collapse', True, bool)

        self.ui.actionCollapse_Singles.setChecked(self.enableCollapse)
        self.ui.actionTrim_Copies.setChecked(self.enableTrim)

        self.ui.actionCollapse_Singles.changed.connect(lambda: self.saveCollapse())
        self.ui.actionTrim_Copies.changed.connect(lambda: self.saveTrim())

        self.ui.actionExit.triggered.connect(lambda: self.exitApplication())

        self.recentFileActions = []

        for i in range(self.maxRecent):
            action = QtWidgets.QAction(self.window, visible=False, triggered=self.loadRecentFile)
            self.recentFileActions.append(action)
            self.ui.menuRecent.addAction(action)

        self._updateRecentActions()

        self.scale = 2.0  # default sprite scaling.

        self.animSpeed = 1 / 60
        self.sheetImage: Optional[pyglet.image.ImageData] = None
        self.imageGrid: Optional[TopLeftGrid] = None

        self.actionPtImage: Optional[pyglet.image.ImageData] = None
        self.actionGrid: Optional[TopLeftGrid] = None
        self.actionPoints: dict[int, ActionPoints] = {}

        self.shadowImage = pyglet.image.load("shadow.png")
        self.shadowImage.anchor_x = self.shadowImage.width // 2
        self.shadowImage.anchor_y = self.shadowImage.height // 2

        self.sprite = None
        self.shadow: Optional[pyglet.sprite.Sprite] = None

        self.markerSize = 5
        self.actionPointMarker = createPlusImage(self.markerSize, (255, 255, 255, 255))
        self.actionPointMarker.anchor_x = self.markerSize // 2
        self.actionPointMarker.anchor_y = self.markerSize // 2
        self.leftHand: Optional[pyglet.sprite.Sprite] = None
        self.center: Optional[pyglet.sprite.Sprite] = None
        self.rightHand: Optional[pyglet.sprite.Sprite] = None
        self.head: Optional[pyglet.sprite.Sprite] = None

        self.apSprites = []

        self.currentDirection = 0
        self.animating = False
        self.newWindow = None

        self.singleLoaded = None

        # Set when opened for non-XML access.
        self.frameWidth = 0
        self.frameHeight = 0
        self.shadowSize = 0

        self.copiedSequence: Optional[AnimationSequence] = None

        # self.window.setFixedSize(670, 836)

        self.currentSequence: Optional[AnimationSequence] = None
        self.currentAnimFrame: Optional[AnimFrame] = None
        self.currentAnimGroup: Optional[AnimGroup] = None

        self.ui.actionLoad.triggered.connect(lambda: self._loadAnimationDialog())

        self.ui.playButton.clicked.connect(lambda: self.playAnimation())

        self.ui.frameDownReorderButton.clicked.connect(lambda: self.moveFrameDown())
        self.ui.frameUpReorderButton.clicked.connect(lambda: self.moveFrameUp())

        self.ui.animationFrameList.itemClicked.connect(lambda item: item.mouseClickEvent(None))

        self.directionButtons = [self.ui.buttonDown, self.ui.buttonDownLeft, self.ui.buttonLeft, self.ui.buttonUpLeft,
                                 self.ui.buttonUp, self.ui.buttonUpRight, self.ui.buttonRight, self.ui.buttonDownRight]

        for dirIdx, button in enumerate(self.directionButtons):
            button.clicked.connect(partial(self.setDirection, self.directionButtons.index(button)))
            button.setCheckable(True)

        self.ui.buttonDown.toggle()

        # self.openGLWidget = self.ui.openGLWidget = PygletWidget(301, 321, self.ui.centralwidget, self.window)
        # self.openGLWidget.setGeometry(QtCore.QRect(10, 340, 301, 321))
        # self.openGLWidget.setMinimumSize(QtCore.QSize(300, 0))
        # self.openGLWidget.setObjectName("openGLWidget")

        self.openGLWidget = self.ui.openGLWidget = PygletWidget(301, 321, self.ui.verticalFrame_3, self.window, self)
        self.openGLWidget.setMinimumSize(QtCore.QSize(0, 321))
        self.openGLWidget.setMaximumSize(QtCore.QSize(16777215, 16777215))
        self.openGLWidget.setObjectName("openGLWidget")
        self.ui.gridLayout.addWidget(self.openGLWidget, 0, 0, 1, 1)

        self.ui.frameSlider.setMaximum(0)

        self.ui.frameIndexSpinBox.valueChanged.connect(lambda: self.frameIndexChanged())

        self.ui.copySequenceButton.clicked.connect(lambda: self.copySequence())
        self.ui.pasteSequenceButton.clicked.connect(lambda: self.pasteSequence())

        self.ui.xSpinBox.valueChanged.connect(lambda: self.spriteOffsetChanged())
        self.ui.ySpinBox.valueChanged.connect(lambda: self.spriteOffsetChanged())

        self.ui.xShadowSpinbox.valueChanged.connect(lambda: self.shadowOffsetChanged())
        self.ui.yShadowSpinBox.valueChanged.connect(lambda: self.shadowOffsetChanged())

        self.ui.durationSpinBox.valueChanged.connect(lambda: self.durationChanged())
        self.ui.mirroredCheckbox.clicked.connect(lambda: self.flipChanged())

        self.ui.frameSlider.valueChanged.connect(lambda: self.sliderChange())
        self.ui.frameDuplicateButton.clicked.connect(lambda: self.duplicateFrame())
        self.ui.frameDeleteButton.clicked.connect(lambda: self.deleteSelectedFrames())

        self.ui.loadedSheetFrameList.itemDoubleClicked.connect(lambda item: item.mouseDoubleClickEvent(None))
        self.ui.loadedSheetFrameList.itemClicked.connect(lambda item: item.mouseClickEvent(None))

        self.ui.actionListWidget.itemActivated.connect(lambda item: item.mouseClickEvent(None))

        self.ui.animationSpeedSlider.valueChanged.connect(lambda: self.changeAnimationSpeed())
        self.ui.animationSpeedSlider.setStyle(ProxyStyle())

        self.ui.actionAddButton.clicked.connect(lambda: self.openAddAction())
        self.ui.actionDuplicateButton.clicked.connect(lambda: self.duplicateAction())
        self.ui.actionDeleteButton.clicked.connect(lambda: self.deleteAction())

        self.ui.actionSave.triggered.connect(lambda: self.saveActionTrigger())
        self.ui.actionSave_As.triggered.connect(lambda: self.saveAsActionTrigger())

        self.ui.defaultFrameButton.clicked.connect(lambda: self.defaultFrameClick())

        self.ui.menuBatch.triggered.connect(lambda: self.openBatchAdd())

        self.ui.returnPointButton.clicked.connect(lambda: self.setReturnPoint())
        self.ui.hitPointButton.clicked.connect(lambda: self.setHitPoint())
        self.ui.rushPointButton.clicked.connect(lambda: self.setRushPoint())

        self.ui.actionExportAll_Animations.triggered.connect(lambda: self.exportMultipleSheets())
        self.ui.actionExportSingle_Animation.triggered.connect(lambda: self.exportSingleSheet())

        self.ui.frameSlider.setStyle(ProxyStyle())

    def setReturnPoint(self):
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item and self.currentAnimGroup:
            idx = self.ui.animationFrameList.row(item)
            if self.currentAnimGroup.returnFrame != idx:
                self.currentAnimGroup.returnFrame = idx
            elif self.currentAnimGroup.returnFrame == idx:
                self.currentAnimGroup.returnFrame = -1

            for frameItem in self._getFrameListItems():
                frameItem.updateText()

    def setHitPoint(self):
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item and self.currentAnimGroup:
            idx = self.ui.animationFrameList.row(item)
            if self.currentAnimGroup.hitFrame != idx:
                self.currentAnimGroup.hitFrame = idx
            elif self.currentAnimGroup.hitFrame == idx:
                self.currentAnimGroup.hitFrame = -1

            for frameItem in self._getFrameListItems():
                frameItem.updateText()

    def setRushPoint(self):
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item and self.currentAnimGroup:
            idx = self.ui.animationFrameList.row(item)
            if self.currentAnimGroup.rushFrame != idx:
                self.currentAnimGroup.rushFrame = idx
            elif self.currentAnimGroup.rushFrame == idx:
                self.currentAnimGroup.rushFrame = -1

            for frameItem in self._getFrameListItems():
                frameItem.updateText()

    def openBatchAdd(self):
        if not self.newWindow:
            self.newWindow = QtWidgets.QWidget()
            ba = Ui_BatchCreateAction()
            ba.setupUi(self.newWindow)
            self.batchAddImplem = BatchAddImplementation(self.newWindow, self, ba)
            self.newWindow.setWindowTitle("Batch Add Action")
        self.newWindow.show()

    def saveTrim(self):
        self.settings.setValue('trim', self.ui.actionTrim_Copies.isChecked())

    def saveCollapse(self):
        self.settings.setValue('collapse', self.ui.actionCollapse_Singles.isChecked())

    def defaultFrameClick(self):
        if self.currentSequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                selectedAnimFrame: AnimFrame = item.animFrame
                defaultData = selectedAnimFrame.defaultCopy

                selectedAnimFrame.frameIndex = defaultData.frameIndex
                selectedAnimFrame.flip = defaultData.flip
                selectedAnimFrame.duration = defaultData.duration
                selectedAnimFrame.shadowOffset = Offset(defaultData.shadowOffset.x, defaultData.shadowOffset.y)
                selectedAnimFrame.spriteOffset = Offset(defaultData.spriteOffset.x, defaultData.spriteOffset.y)

                item.updateText()

                if not self.animating:
                    self.setAnimFrameValues(selectedAnimFrame)

                    self.setAnimation()

    def saveActionTrigger(self):
        if self.loadedTree:
            self._saveFrameData()

    def saveAsActionTrigger(self):
        if self.loadedTree:
            fileName, _ = QFileDialog.getSaveFileName(self.window, "Save Animation File", "",
                                                      "Animation File (FrameData.xml, *.xml)")

            self._saveFrameData(fileName)

    def _saveFrameData(self, fileName=None):
        collapse = self.ui.actionCollapse_Singles.isChecked()
        existingRoot = self.loadedTree.getroot()

        root = ElementTree.Element("AnimData")

        ElementTree.SubElement(root, "FrameWidth").text = str(self.frameWidth)
        ElementTree.SubElement(root, "FrameHeight").text = str(self.frameHeight)
        ElementTree.SubElement(root, "ShadowSize").text = str(self.shadowSize)

        animsEl = ElementTree.SubElement(root, "Anims")

        trim = self.ui.actionTrim_Copies.isChecked()

        for groupAnim in self.groups:
            animEl = ElementTree.SubElement(animsEl, "Anim")
            if self.createBaseAnimGroupXML(animEl, groupAnim.name, groupAnim.idx, groupAnim, trim=trim,
                                        copyName=groupAnim.copyName):
                self.createSingleSheetFrameData(animEl, groupAnim, collapse)

        ElementTree.indent(root)

        if not fileName:
            # Use loaded file name
            fileName = self.fileName

            # Reset saves.
            for item in self._getActionListItems():
                item.animGroup.modified = False
                item.updateText()

        tree = ElementTree.ElementTree(root)
        tree.write(fileName, encoding='utf-8', xml_declaration=True)

    def isSequenceCollapsable(self, animGroup: AnimGroup):
        ct = 0
        first = animGroup.directions[0].frames
        for direction in animGroup.directions:
            if direction.frames == first:
                ct += 1

        if ct == 8:
            # All 8 frames are the same.
            return True

        return False

    def createBaseAnimGroupXML(self, animEl: ElementTree.Element, name: str, index: int, group: AnimGroup,
                               trim=False, copyName="", size=None) -> bool:
        ElementTree.SubElement(animEl, "Name").text = name

        if index != -1:
            ElementTree.SubElement(animEl, "Index").text = str(index)

        if trim:
            if copyName:
                ElementTree.SubElement(animEl, "CopyOf").text = str(copyName)
                return False

        if size:
            ElementTree.SubElement(animEl, "FrameWidth").text = str(size[0])
            ElementTree.SubElement(animEl, "FrameHeight").text = str(size[1])

        if group.rushFrame != -1:
            ElementTree.SubElement(animEl, "RushFrame").text = str(group.rushFrame)

        if group.hitFrame != -1:
            ElementTree.SubElement(animEl, "HitFrame").text = str(group.hitFrame)

        if group.returnFrame != -1:
            ElementTree.SubElement(animEl, "ReturnFrame").text = str(group.returnFrame)

        return True

    def createSingleSheetFrameData(self, animEl: ElementTree.Element, group: AnimGroup, collapse):
        sequencesEle = ElementTree.SubElement(animEl, "Sequences")

        isCollapsable = False
        if collapse:
            isCollapsable = self.isSequenceCollapsable(group)

        for sequence in group.directions:
            seqEle = ElementTree.SubElement(sequencesEle, "AnimSequence")

            for frame in sequence.frames:
                frameEle = ElementTree.SubElement(seqEle, "AnimFrame")

                ElementTree.SubElement(frameEle, "FrameIndex").text = str(frame.frameIndex)
                ElementTree.SubElement(frameEle, "Duration").text = str(frame.duration)
                ElementTree.SubElement(frameEle, "HFlip").text = str(int(frame.flip))
                spriteOff = ElementTree.SubElement(frameEle, "Sprite")
                ElementTree.SubElement(spriteOff, "XOffset").text = str(frame.spriteOffset.x)
                ElementTree.SubElement(spriteOff, "YOffset").text = str(frame.spriteOffset.y)
                shadowOff = ElementTree.SubElement(frameEle, "Shadow")
                ElementTree.SubElement(shadowOff, "XOffset").text = str(frame.shadowOffset.x)
                ElementTree.SubElement(shadowOff, "YOffset").text = str(frame.shadowOffset.y)

            # Stop after writing a frame if we are collapsing it.
            if isCollapsable:
                break

        return True

    def createErrorPopup(self, text: str):
        return QtWidgets.QMessageBox.critical(self.window, 'Error',text, QtWidgets.QMessageBox.Ok)

    def createMultiSheetFrameData(self, animEl: ElementTree.Element, group: AnimGroup):
        """This essentially just includes the durations of each frame. Limited in that durations are set for the whole
        animation regardless of direction."""
        uniformDurations: Set[Tuple] = set()
        for sequence in group.directions:
            durations = tuple([frame.duration for frame in sequence.frames])
            if not durations:  # Ignore empty sequences?
                continue

            uniformDurations.add(durations)

            if len(uniformDurations) > 1:
                # Check if frame count differs between directions.
                firstLength = len(next(iter(uniformDurations), ()))
                for t in uniformDurations:
                    if len(t) != firstLength:
                        return self.createErrorPopup(f"Could not save AnimData.xml. All directions for animation {group.name} must all be the same number of frames.")

                # If frame counts are fine, then the durations are messed up.
                return self.createErrorPopup(f"Could not save AnimData.xml. Duration values for {group.name} must match for all directions for each frame.")

        durationEle = ElementTree.SubElement(animEl, "Durations")

        for durations in uniformDurations:
            for value in durations:
                ElementTree.SubElement(durationEle, "Duration").text = str(value)


        return True

    def frameIndexChanged(self):
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item:
            idx = self.ui.frameIndexSpinBox.value()
            clampedIdx = clamp(idx, 0, len(self.imageGrid) - 1)

            if item.animFrame.frameIndex != clampedIdx:
                item.animFrame.frameIndex = clampedIdx

                item.updateText()

                self._notifyChanges()

                if not self.animating:
                    self.openGLWidget.makeCurrent()

                    self._setAnimFrameDisplay(item.animFrame)

    def _moveFrame(self, rowDir: int):
        currentPos = self.ui.animationFrameList.currentRow()
        if rowDir < 0 and currentPos == 0:
            return

        if rowDir > 0 and currentPos == self.ui.animationFrameList.count() - 1:
            return

        newPos = currentPos + rowDir

        item = self.ui.animationFrameList.takeItem(currentPos)
        self.ui.animationFrameList.insertItem(newPos, item)

        swapItem = self.ui.animationFrameList.item(currentPos)
        newItem = self.ui.animationFrameList.item(newPos)

        # Swap position and indexes.
        self.currentSequence.frames[currentPos], self.currentSequence.frames[newPos] = self.currentSequence.frames[
            newPos], self.currentSequence.frames[currentPos]
        newItem.animFrame.idx, swapItem.animFrame.idx = swapItem.animFrame.idx, newItem.animFrame.idx

        self.clearCopyGroup()

        self.ui.frameSlider.setValue(item.animFrame.idx)
        self.ui.animationFrameList.setCurrentRow(newPos)

    def clearCopyGroup(self):
        self.currentAnimGroup.copyName = ""

    def copySequence(self):
        if self.currentSequence:
            self.copiedSequence = copy.deepcopy(self.currentSequence)

    def pasteSequence(self):
        if self.currentSequence:
            if self.copiedSequence:
                self.currentSequence = self.copiedSequence
                self.currentAnimGroup.directions[self.currentDirection] = self.copiedSequence
                self.currentAnimFrame = self.copiedSequence.frames[0]
                self.copiedSequence = None
                self.setSequenceList()
                self.ui.animationFrameList.setCurrentRow(0)
                self.setAnimFrameValues(self.currentAnimFrame)

    def moveFrameUp(self):
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item:
            self._moveFrame(-1)

    def moveFrameDown(self):
        item: AnimFrameItem = self.ui.animationFrameList.currentItem()
        if item:
            self._moveFrame(1)

    def addRecentList(self, path):
        # Push to top.
        try:
            self.recentFiles.remove(path)
        except ValueError:
            pass

        self.recentFiles.insert(0, path)

        self.recentFiles = self.recentFiles[:self.maxRecent]

        self.settings.setValue('recent', self.recentFiles)

        self._updateRecentActions()

    def _updateRecentActions(self):
        recentCount = min(len(self.recentFiles), self.maxRecent)

        for i in range(recentCount):
            self.recentFileActions[i].setText(f"{QFileInfo(self.recentFiles[i]).filePath()}")
            self.recentFileActions[i].setVisible(True)
            self.recentFileActions[i].setData(self.recentFiles[i])

        for m in range(recentCount, self.maxRecent):
            self.recentFileActions[m].setVisible(False)

    def deleteAction(self):
        current: AnimGroupItem = self.ui.actionListWidget.currentItem()

        if current:

            ret = QtWidgets.QMessageBox.question(self.window, '',
                                                 f"Are you sure you want to delete {current.animGroup.name}?",
                                                 QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
            if ret == QtWidgets.QMessageBox.Yes:
                row = self.ui.actionListWidget.row(current)
                taken = self.ui.actionListWidget.takeItem(row)
                del taken

                pyglet.clock.unschedule(self._playingAnimation)
                self.animating = False
                self.currentAnimFrame = None
                self.currentAnimGroup = None
                self.currentSequence = None

                self.clearAnimFrame()

                if self.sprite:
                    self.sprite.delete()
                    self.shadow.delete()
                    self.sprite = None
                    self.shadow = None

                self.ui.animationFrameList.clear()

    def duplicateAction(self):
        current: AnimGroupItem = self.ui.actionListWidget.currentItem()

        if not current:
            self.ui.statusBar.showMessage("Select an action to duplicate.", 5000)
            return

        name, ok = QInputDialog.getText(self.window, 'Duplicate Action', 'Enter an action name to replace or add.')
        if not ok:
            return

        indexId, ok = QInputDialog.getText(self.window, 'Duplicate Action',
                                           'Specify an available index ID. Leave blank to generate one.')

        if not ok:
            return

        try:
            indexId = int(indexId)
        except ValueError:
            indexId = None

        existingGroup: Optional[AnimGroup] = None
        if self.groups and ok and name:
            selectedGroup = current.animGroup

            for group in self.groups:
                if group.name.lower() == name.lower():
                    existingGroup = group
                    break

            # Replace existing group.
            if existingGroup:
                existingGroup.rushFrame = selectedGroup.rushFrame
                existingGroup.hitFrame = selectedGroup.hitFrame
                existingGroup.returnFrame = selectedGroup.returnFrame
                existingGroup.directions = copy.deepcopy(selectedGroup.directions)

                for item in self._getActionListItems():
                    if item.animGroup.name.lower() == name.lower():
                        item.updateText()

                self.ui.statusBar.showMessage(f"Action: {name} already existed. It has been replaced.", 5000)

            else:
                if not indexId:
                    indexId = self.groups[-1].idx + 1

                group = AnimGroup(indexId, name, selectedGroup.rushFrame, selectedGroup.hitFrame,
                                  selectedGroup.returnFrame, copy.deepcopy(selectedGroup.directions))
                group.copyName = selectedGroup.name
                self.groups.append(group)
                item = AnimGroupItem(group, self)
                self.ui.actionListWidget.addItem(item)

                self.ui.statusBar.showMessage(f"Action: {name} created.", 5000)

    def openAddAction(self):
        name, ok = QInputDialog.getText(self.window, 'Create New Action', 'Enter an action name.')
        if not ok:
            return

        indexId, ok = QInputDialog.getText(self.window, 'Create New Action',
                                           'Specify an available index ID. Leave blank to generate one.')

        if not ok:
            return

        try:
            indexId = int(indexId)
        except ValueError:
            indexId = None

        if self.groups and ok and name:

            for group in self.groups:
                if group.name == name:
                    self.ui.statusBar.showMessage(f"Action: {name} already exists.", 5000)
                    return

                if group.idx == indexId:
                    self.ui.statusBar.showMessage(f"Index ID {indexId} already exists.", 5000)
                    return

            if indexId is None:
                indexId = self.groups[-1].idx + 1

            group = AnimGroup(indexId, name)
            self.groups.append(group)
            item = AnimGroupItem(group, self)
            self.ui.actionListWidget.addItem(item)

    def _notifyChanges(self):
        """Set action as changed if an action has been modified."""
        if self.currentAnimGroup:
            for sequences in self.currentAnimGroup.directions:
                for frame in sequences.frames:
                    if frame.changed:
                        for item in self._getActionListItems():
                            if item.animGroup == self.currentAnimGroup:
                                if self.currentAnimGroup.modified is False:
                                    self.currentAnimGroup.modified = True
                                    item.updateText()
                                return

    def changeAnimationSpeed(self):
        newSpeed = self.animationSpeedSliderValues[self.ui.animationSpeedSlider.value()]

        self.animSpeed = (1 / 60.0) / newSpeed

        self.ui.animationSpeedLabel.setText(f"{newSpeed}x")

    def durationChanged(self):
        if self.currentSequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                animFrame = item.animFrame
                value = self.ui.durationSpinBox.value()
                animFrame.duration = value

                self.clearCopyGroup()

                self._notifyChanges()

    def flipChanged(self):
        if self.currentSequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                animFrame = item.animFrame
                checked = self.ui.mirroredCheckbox.isChecked()
                if int(checked) != animFrame.flip:
                    animFrame.flip = int(checked)

                    self.clearCopyGroup()

                    if not self.animating:
                        self.setAnimation()

                    self._notifyChanges()

    def spriteOffsetChanged(self):
        if self.currentSequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                animFrame = item.animFrame
                offset = animFrame.spriteOffset
                xValue = self.ui.xSpinBox.value()
                yValue = self.ui.ySpinBox.value()

                if offset.x != xValue or offset.y != yValue:
                    offset.x = xValue
                    offset.y = yValue

                    self.clearCopyGroup()

                    if not self.animating:
                        self.setAnimation()

                    self._notifyChanges()

    def shadowOffsetChanged(self):
        if self.currentSequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                animFrame = item.animFrame
                offset = animFrame.shadowOffset
                xValue = self.ui.xShadowSpinbox.value()
                yValue = self.ui.yShadowSpinBox.value()

                if offset.x != xValue or offset.y != yValue:
                    offset.x = xValue
                    offset.y = yValue

                    self.clearCopyGroup()

                    if not self.animating:
                        self.setAnimation()

                    self._notifyChanges()

    def duplicateFrame(self):
        """Duplicate the selected frame in the Animation Sequence List"""
        if self.currentSequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                selectedAnimFrame: AnimFrame = item.animFrame
                animIdx = len(self.currentSequence.frames)
                animFrame = AnimFrame(animIdx, selectedAnimFrame.frameIndex, selectedAnimFrame.flip,
                                      selectedAnimFrame.duration,
                                      Offset(selectedAnimFrame.shadowOffset.x, selectedAnimFrame.shadowOffset.y),
                                      Offset(selectedAnimFrame.spriteOffset.x, selectedAnimFrame.spriteOffset.y))

                self.clearCopyGroup()

                self._notifyChanges()

                self._addAnimFrame(animFrame)

    def deleteSelectedFrames(self):
        """Delete the selected frame in the Animation Sequence List"""
        if self.currentSequence:
            item: AnimFrameItem = self.ui.animationFrameList.currentItem()
            if item:
                for frame in list(self.currentSequence.frames):
                    if frame == item.animFrame:
                        self.currentSequence.frames.remove(frame)

                self.ui.animationFrameList.takeItem(self.ui.animationFrameList.row(item))
                del item

                self.clearCopyGroup()

                # Just use last in list as selection.
                lastIdx = self.ui.animationFrameList.count() - 1
                lastItem = self.ui.animationFrameList.item(lastIdx)
                self.ui.animationFrameList.setCurrentItem(lastItem)
                self._updateAnimFrameWidgets()

    def sliderChange(self):
        if not self.animating:
            if self.currentSequence:
                if self.currentSequence.frames:
                    self.ui.animationFrameList.setCurrentRow(self.ui.frameSlider.value())
                    self.currentAnimFrame = self.currentSequence.frames[self.ui.frameSlider.value()]
                    self.setAnimFrameValues(self.currentAnimFrame)
                    self.setAnimation()

    def setDirection(self, direction):
        if direction == self.currentDirection:
            self.directionButtons[direction].toggle()
            return

        for button in self.directionButtons:
            if button != self.directionButtons[direction]:
                button.setChecked(False)

        self.currentDirection = direction

        self.ui.animationFrameList.clear()

        if self.currentAnimGroup:
            if self.currentAnimGroup.directions:
                self.currentSequence = self.currentAnimGroup.directions[self.currentDirection]

                if self.currentSequence.frames:
                    self.currentAnimFrame = self.currentSequence.frames[0]

                if not self.animating:
                    self.setAnimFrameValues(self.currentAnimFrame)

                if self.currentSequence:
                    self.setSequenceList()

                self.ui.animationFrameList.setCurrentRow(0)

    def clearAnimFrame(self):
        """Clears information boxes of the Frame Data."""
        self.currentAnimFrame = None

        self.ui.animationFrameList.clear()

        self.ui.frameIndexSpinBox.blockSignals(True)
        self.ui.frameIndexSpinBox.setValue(0)
        self.ui.frameIndexSpinBox.blockSignals(False)

        self.ui.durationSpinBox.blockSignals(True)
        self.ui.durationSpinBox.setValue(0)
        self.ui.durationSpinBox.blockSignals(False)

        self.ui.mirroredCheckbox.blockSignals(True)
        self.ui.mirroredCheckbox.setChecked(False)
        self.ui.mirroredCheckbox.blockSignals(False)

        self.ui.xSpinBox.blockSignals(True)
        self.ui.ySpinBox.blockSignals(True)
        self.ui.xSpinBox.setValue(0)
        self.ui.ySpinBox.setValue(0)
        self.ui.xSpinBox.blockSignals(False)
        self.ui.ySpinBox.blockSignals(False)

        self.ui.xShadowSpinbox.blockSignals(True)
        self.ui.yShadowSpinBox.blockSignals(True)
        self.ui.xShadowSpinbox.setValue(0)
        self.ui.yShadowSpinBox.setValue(0)
        self.ui.xShadowSpinbox.blockSignals(False)
        self.ui.yShadowSpinBox.blockSignals(False)

    def setSequenceList(self):
        self.ui.animationFrameList.clear()

        duration = 0
        for animFrame in self.currentSequence.frames:
            duration += animFrame.duration
            item = AnimFrameItem(animFrame, self)
            self.ui.animationFrameList.addItem(item)

        self._updateAnimFrameWidgets()

    def _updateAnimFrameWidgets(self):
        self.ui.frameSlider.setMaximum(max(0, len(self.currentSequence.frames) - 1))
        self.ui.frameSlider.setValue(0)

        self.setAnimation()

    def setAnimFrameValues(self, animFrame):
        self.ui.frameIndexSpinBox.setValue(animFrame.frameIndex)

        self.ui.durationSpinBox.setValue(animFrame.duration)

        self.ui.mirroredCheckbox.setChecked(bool(animFrame.flip))

        self.setOffsetData(animFrame)

    def setOffsetData(self, animFrame):
        if animFrame:
            self.ui.xSpinBox.blockSignals(True)
            self.ui.ySpinBox.blockSignals(True)
            self.ui.xShadowSpinbox.blockSignals(True)
            self.ui.yShadowSpinBox.blockSignals(True)

            self.ui.xSpinBox.setValue(animFrame.spriteOffset.x)
            self.ui.ySpinBox.setValue(animFrame.spriteOffset.y)

            self.ui.xShadowSpinbox.setValue(animFrame.shadowOffset.x)
            self.ui.yShadowSpinBox.setValue(animFrame.shadowOffset.y)

            self.ui.xSpinBox.blockSignals(False)
            self.ui.ySpinBox.blockSignals(False)
            self.ui.xShadowSpinbox.blockSignals(False)
            self.ui.yShadowSpinBox.blockSignals(False)

    def loadRecentFile(self):
        action = self.window.sender()
        if action:
            path: str = action.data()

            # Just hard code this.
            if "AnimData.xml" in path:
                self.importMultipleSheets(path)
            else:
                self.loadSheet(path)

    def _loadAnimationDialog(self):
        fileName, _ = QFileDialog.getOpenFileName(self.window, "Select Animation File", "",
                                                  "Animation File (FrameData.xml, AnimData.xml, *.xml)")

        if fileName:
            if 'AnimData.xml' in fileName:
                self.importMultipleSheets(fileName)
            else:
                self.loadSheet(fileName)

    def loadSheet(self, fileName):
        dirName = os.path.dirname(fileName)

        try:
            sheetImage = pyglet.image.load(f"{dirName}/Anim.png")
        except FileNotFoundError:
            self.ui.statusBar.showMessage(f"Failed to find Anim.png.", 5000)
            return

        try:
            actionPtImage = pyglet.image.load(f"{dirName}/Offsets.png")
        except FileNotFoundError:
            actionPtImage = None
            self.ui.statusBar.showMessage(f"Failed to find Offsets file... skipping.", 5000)

        self.clear()

        self.singleLoaded = True

        self.sheetImage = sheetImage  # Do this after clear. Try block above so we don't clear loaded if fail loading.
        self.actionPtImage = actionPtImage

        self._parse(fileName)

        if self.batchAddImplem:
            self.batchAddImplem.loadedFrameData()

    def addNewAnimationFrame(self, frameIdx: int):
        """Create new animation frame in the sequence. Adds to the end."""
        if self.currentSequence:
            animIdx = len(self.currentSequence.frames)
            animFrame = AnimFrame(animIdx, frameIdx)

            self._addAnimFrame(animFrame)

            self.clearCopyGroup()

            self._notifyChanges()

    def _addAnimFrame(self, animFrame: AnimFrame):
        self.currentSequence.frames.append(animFrame)

        item = AnimFrameItem(animFrame, self)
        self.ui.animationFrameList.addItem(item)

        self._updateAnimFrameWidgets()

    @staticmethod
    def adjustOffset(rushFrame: int, frameNum: int, rushOffset: Offset, frameOffset: Offset):
        """Calculation to truncate rush frames."""
        if frameNum > rushFrame:
            diff = frameOffset - rushOffset

            final = rushOffset + (diff // 3)
            return final

    def _addFramesFromGrid(self):
        self.ui.frameIndexSpinBox.setMaximum(len(self.imageGrid) - 1)

        for idx, image in enumerate(self.imageGrid):
            image: pyglet.image.ImageDataRegion
            image.anchor_x = image.width // 2
            image.anchor_y = image.height // 2
            if self.actionPoints:
                if idx not in self.actionPoints:
                    continue
            item = LoadedSheetFrame(f"Frame {idx}", idx, image, self.ui.sheetFramePicture, self)
            self.ui.loadedSheetFrameList.addItem(item)

    def _parse(self, fileName):
        try:
            self.loadedTree = ElementTree.parse(fileName)
        except ElementTree.ParseError:
            self.ui.statusBar.showMessage("Failed to parse animations XML data.", 5000)
            return

        root = self.loadedTree.getroot()

        try:
            width = int(root.find("FrameWidth").text)
            height = int(root.find("FrameHeight").text)
            shadowSize = int(root.find("ShadowSize").text)
        except AttributeError:
            self.ui.statusBar.showMessage("Unable to determine dimensions of XML data.", 5000)
            return

        self.frameWidth = width
        self.frameHeight = height
        self.shadowSize = shadowSize


        anims = root.find('Anims')
        if not anims:
            self.ui.statusBar.showMessage(f"Unable to find any Animation XML data.", 5000)
            return

        self.imageGrid = TopLeftGrid(self.sheetImage,
                                     rows=self.sheetImage.height // height,
                                     columns=self.sheetImage.width // width)

        if self.actionPtImage:
            self.actionGrid = TopLeftGrid(self.actionPtImage,
                                     rows=self.sheetImage.height // height,
                                     columns=self.sheetImage.width // width)

            actionCenter = self.actionGrid[0].width // 2, self.actionGrid[0].height // 2
            for idx, actImg in enumerate(self.actionGrid):
                actImg: pyglet.image.ImageDataRegion
                actionPointLoc = getActionPointsFromImage(actImg)

                if actionPointLoc[0] and actionPointLoc[1] and actionPointLoc[2]:
                    center = actionPointLoc[1]
                    head = center
                    if actionPointLoc[3]:
                        head = actionPointLoc[3]

                    leftHand = actionPointLoc[0]
                    rightHand = actionPointLoc[2]

                    self.actionPoints[idx] = ActionPoints(leftHand, center, rightHand, head)

                    # Position relative to 0, 0.
                    self.actionPoints[idx].add(Offset(-actionCenter[0], -actionCenter[1]))


                else:
                    self.actionGrid = None
                    break

        self._addFramesFromGrid()

        self.groups = []
        copies = []
        copyGroups = []
        for actionAnim in anims:
            actionIdx = -1
            rushFrame = -1
            hitFrame = -1
            returnFrame = -1
            sequences = []
            copyName = ''
            for actionElement in actionAnim:
                if actionElement.tag == "Name":
                    name = actionElement.text
                elif actionElement.tag == "Index":
                    actionIdx = int(actionElement.text)
                elif actionElement.tag == "CopyOf":
                    copyName = actionElement.text
                elif actionElement.tag == "RushFrame":
                    rushFrame = int(actionElement.text)
                elif actionElement.tag == "HitFrame":
                    hitFrame = int(actionElement.text)
                elif actionElement.tag == "ReturnFrame":
                    returnFrame = int(actionElement.text)
                elif actionElement.tag == "Sequences":
                    sequences = []
                    for sequenceElement in actionElement:
                        frameSeqIdx = 0
                        frames = []
                        for animSequences in sequenceElement:
                            for frame in animSequences:
                                if frame.tag == "FrameIndex":
                                    frameIndex = int(frame.text)

                                elif frame.tag == "Sprite":
                                    spriteOffset = Offset(*[int(offset.text) for offset in frame])

                                elif frame.tag == "Shadow":
                                    shadowOffset = Offset(*[int(offset.text) for offset in frame])

                                elif frame.tag == "HFlip":
                                    try:
                                        hflip = int(frame.text)
                                    except ValueError:
                                        hflip = int(bool(frame.text))

                                elif frame.tag == "Duration":
                                    duration = int(frame.text)

                            frames.append(
                                AnimFrame(frameSeqIdx, frameIndex, hflip, duration, shadowOffset, spriteOffset))
                            frameSeqIdx += 1

                            if REDUCE_RUSH_FRAMES:
                                if rushFrame > -1:
                                    frame2 = frameSeqIdx -1
                                    #print(frame2, rushFrame, name, frames)
                                    if frame2 > rushFrame:
                                        #print("NAME", name)
                                        #print("FRAME!", name, self.adjustOffset(rushFrame, frame2, frames[rushFrame].spriteOffset, spriteOffset))

                                        frames[frame2].spriteOffset = self.adjustOffset(rushFrame, frame2, frames[rushFrame].spriteOffset, spriteOffset)
                                        frames[frame2].shadowOffset = self.adjustOffset(rushFrame, frame2,
                                                                                        frames[rushFrame].shadowOffset,
                                                                                        shadowOffset)

                        sequence = AnimationSequence(frames)
                        sequences.append(sequence)

            if copyName:
                group = AnimGroup(actionIdx, name, copyName=copyName)
                copyGroups.append(group)
            else:
                if len(sequences) == 1:
                    print(f"Warning: {name} only has 1 sequence. Duplicating for all directions.")
                    for i in range(7):
                        newSequence = copy.deepcopy(sequences[0])
                        sequences.append(newSequence)

                elif len(sequences) == 0:
                    print(f"Warning: {name} no sequences found. Generating empty sequences.")
                    for i in range(8):
                        sequences.append(AnimationSequence())

                group = AnimGroup(actionIdx, name, rushFrame, hitFrame, returnFrame, sequences)
            group.width = width
            group.height = height
            self.groups.append(group)
            item = AnimGroupItem(group, self)
            self.ui.actionListWidget.addItem(item)

        # Unfortunately copy actions can come before the action they need to copy? Check after we have parsed all actions.
        # Some copy actions don't even have action indexes... Indexes currently have no use.
        for groups in copyGroups:
            name, copyName = groups.name, groups.copyName
            # Find copy group
            found = False
            for currentGroup in self.groups:
                if currentGroup.name == copyName:
                    found = currentGroup
                    break

            if found:
                # Find destination group.
                for currentGroup in self.groups:
                    if currentGroup.name == name:
                        group = copy.deepcopy(found)
                        currentGroup.rushFrame = group.rushFrame
                        currentGroup.hitFrame = group.hitFrame
                        currentGroup.returnFrame = group.returnFrame
                        currentGroup.directions = group.directions

            else:
                print(f"Copy {name} not found")
                continue

        self.ui.statusBar.showMessage("Frame data and images loaded successfully.", 3000)

        self.fileName = fileName

        self.addRecentList(fileName)

    def _getActionListItems(self) -> List[AnimGroupItem]:
        return [self.ui.actionListWidget.item(x) for x in range(self.ui.actionListWidget.count())]

    def _getFrameListItems(self) -> List[AnimFrameItem]:
        return [self.ui.animationFrameList.item(x) for x in range(self.ui.animationFrameList.count())]

    def clear(self):
        """Clear everything so we can load a new sprite."""
        self.singleLoaded = None
        self.sheetImage = None
        self.actionPtImage = None
        self.imageGrid: Optional[TopLeftGrid] = None
        self.actionGrid: Optional[TopLeftGrid] = None
        self.actionPoints.clear()
        self.groups.clear()

        pyglet.clock.unschedule(self._playingAnimation)
        self.animating = False
        self.currentAnimFrame = None
        self.currentAnimGroup = None
        self.currentSequence = None

        self.clearAnimFrame()

        if self.sprite:
            self.sprite.delete()
            self.shadow.delete()
            self.sprite = None
            self.shadow = None

        self.ui.loadedSheetFrameList.clear()
        self.ui.animationFrameList.clear()
        self.ui.actionListWidget.clear()

        self.ui.sheetFramePicture.clear()

    def getSpritePosition(self):
        return ((self.openGLWidget.width() // 2) + (self.currentAnimFrame.spriteOffset.x * self.scale),
                (self.openGLWidget.height() // 3) + (-self.currentAnimFrame.spriteOffset.y * self.scale), 0)

    def getShadowPosition(self):
        return ((self.openGLWidget.width() // 2) + (self.currentAnimFrame.shadowOffset.x * self.scale),
                (self.openGLWidget.height() // 3) + (-self.currentAnimFrame.shadowOffset.y * self.scale), 0)

    def getActionPointPositions(self):
        ap = self.actionPoints[self.currentAnimFrame.frameIndex]

        if not self.currentAnimFrame.flip:
            lhPos, cPos, rhPos, headPos = ap.allPos()
        else:
            lhPos, cPos, rhPos, headPos = ap.allFlipPos()

        lh = ((self.openGLWidget.width() // 2) + ((self.currentAnimFrame.spriteOffset.x + lhPos.x) * self.scale),
                (self.openGLWidget.height() // 3) + ((-self.currentAnimFrame.spriteOffset.y - lhPos.y) * self.scale), 0)

        cent = ((self.openGLWidget.width() // 2) + ((self.currentAnimFrame.spriteOffset.x + cPos.x) * self.scale),
                (self.openGLWidget.height() // 3) + ((-self.currentAnimFrame.spriteOffset.y - cPos.y) * self.scale), 0)

        rh = ((self.openGLWidget.width() // 2) + ((self.currentAnimFrame.spriteOffset.x + rhPos.x) * self.scale),
                (self.openGLWidget.height() // 3) + ((-self.currentAnimFrame.spriteOffset.y - rhPos.y) * self.scale), 0)

        head = ((self.openGLWidget.width() // 2) + ((self.currentAnimFrame.spriteOffset.x + headPos.x) * self.scale),
                (self.openGLWidget.height() // 3) + ((-self.currentAnimFrame.spriteOffset.y - headPos.y) * self.scale), 0)
        return lh, cent, rh, head

    def _playingAnimation(self, dt):
        self.openGLWidget.makeCurrent()

        self._setAnimFrameDisplay(self.currentAnimFrame)

        self.ui.frameSlider.setValue(self.currentAnimFrame.idx)

        existingFrame = self.currentAnimFrame

        nextFrame = self.currentAnimFrame.idx + 1
        if nextFrame > len(self.currentSequence.frames) - 1:
            nextFrame = 0

        self.currentAnimFrame = self.currentSequence.frames[nextFrame]

        pyglet.clock.schedule_once(self._playingAnimation, self.animSpeed * existingFrame.duration)

    def playAnimation(self):
        if self.currentAnimFrame:
            self.animating = not self.animating
            pyglet.clock.unschedule(self._playingAnimation)

            if self.animating:
                pyglet.clock.schedule_once(self._playingAnimation, self.animSpeed * self.currentAnimFrame.duration)
            else:
                self.sliderChange()

    def setAnimation(self):
        self.openGLWidget.makeCurrent()

        if self.currentAnimGroup:
            if self.currentAnimGroup.directions[self.currentDirection].frames:
                if not self.sprite:
                    spritePos = self.getSpritePosition()
                    shadowPos = self.getShadowPosition()

                    firstIdx = self.currentAnimGroup.directions[self.currentDirection].frames[0].frameIndex

                    self.shadow = pyglet.sprite.Sprite(self.shadowImage, x=shadowPos[0], y=shadowPos[1],
                                                       batch=self.openGLWidget.batch, group=pyglet.graphics.Group(0))
                    self.shadow.scale = self.scale

                    self.sprite = pyglet.sprite.Sprite(self.imageGrid[firstIdx], x=spritePos[0], y=spritePos[1],
                                                       batch=self.openGLWidget.batch, group=pyglet.graphics.Group(1))
                    self.sprite.scale = self.scale


                if not self.leftHand:
                    if self.actionPoints:
                        lhPos, centPos, rhPos, headPos = self.getActionPointPositions()

                        self.leftHand = pyglet.sprite.Sprite(self.actionPointMarker, x=lhPos[0], y=lhPos[1],
                                                           batch=self.openGLWidget.batch, group=pyglet.graphics.Group(2))
                        self.leftHand.color = (255, 0, 0)
                        self.leftHand.scale = self.scale

                        self.center = pyglet.sprite.Sprite(self.actionPointMarker, x=centPos[0], y=centPos[1],
                                                           batch=self.openGLWidget.batch, group=pyglet.graphics.Group(3))
                        self.center.color = (0, 255, 0)
                        self.center.scale = self.scale

                        self.rightHand =pyglet.sprite.Sprite(self.actionPointMarker, x=rhPos[0], y=rhPos[1],
                                                           batch=self.openGLWidget.batch, group=pyglet.graphics.Group(2))
                        self.rightHand.color = (0, 0, 255)
                        self.rightHand.scale = self.scale

                        self.head = pyglet.sprite.Sprite(self.actionPointMarker, x=headPos[0], y=headPos[1],
                                                           batch=self.openGLWidget.batch, group=pyglet.graphics.Group(2))
                        self.head.color = (0, 0, 0)
                        self.head.scale = self.scale

                        self.apSprites = [self.leftHand, self.center, self.rightHand, self.head]
                        for sprite in self.apSprites: # Start them off as invisible.
                            sprite.visible = False

                if self.sprite:
                    animFrame = self.currentAnimFrame if self.currentAnimFrame is not None else \
                        self.currentAnimGroup.directions[self.currentDirection].frames[0]

                    self._setAnimFrameDisplay(animFrame)

    def _setAnimFrameDisplay(self, animFrame: AnimFrame):
        if self.sprite:
            image = self.imageGrid[animFrame.frameIndex]
            if animFrame.flip:
                image = image.get_texture().get_transform(flip_x=True)
                image.anchor_x = image.width // 2
                image.anchor_y = image.height // 2

            self.sprite.image = image

            self.sprite.position = self.getSpritePosition()
            self.shadow.position = self.getShadowPosition()

            if self.leftHand:
                lhPos, centPos, rhPos, headPos = self.getActionPointPositions()
                self.leftHand.position = lhPos
                self.center.position = centPos
                self.rightHand.position = rhPos
                self.head.position = headPos

    def importMultipleSheets(self, fileName):
        dirName = os.path.dirname(fileName)

        try:
            self.loadedTree = ElementTree.parse(fileName)
        except ElementTree.ParseError:
            self.ui.statusBar.showMessage("Failed to parse animations XML data.", 5000)
            return

        root = self.loadedTree.getroot()

        anims = root.find('Anims')
        if not anims:
            self.ui.statusBar.showMessage(f"Unable to find any Animation XML data.", 5000)
            return

        try:
            shadowSize = int(root.find("ShadowSize").text)
        except AttributeError:
            self.ui.statusBar.showMessage("Unable to determine dimensions of XML data.", 5000)
            return

        self.clear()

        self.singleLoaded = False

        self.ui.statusBar.showMessage(f"Processing... this may take a moment.", 5000)

        self.app.processEvents()

        self.groups = []
        copyGroups = []
        collapsedAnims = []
        maxWidth = 0
        maxHeight = 0
        frames = []
        framesToSequence = []
        for actionAnim in anims:
            name = "Unknown"
            actionIdx = -1
            rushFrame = -1
            hitFrame = -1
            returnFrame = -1
            copyName = ''
            frameHeight = 0
            frameWidth = 0
            durations = []
            for actionElement in actionAnim:
                if actionElement.tag == "Name":
                    name = actionElement.text
                elif actionElement.tag == "Index":
                    actionIdx = int(actionElement.text)
                elif actionElement.tag == "CopyOf":
                    copyName = actionElement.text
                elif actionElement.tag == "RushFrame":
                    rushFrame = int(actionElement.text)
                elif actionElement.tag == "HitFrame":
                    hitFrame = int(actionElement.text)
                elif actionElement.tag == "ReturnFrame":
                    returnFrame = int(actionElement.text)
                elif actionElement.tag == "FrameWidth":
                    frameWidth = int(actionElement.text)
                elif actionElement.tag == "FrameHeight":
                    frameHeight = int(actionElement.text)
                elif actionElement.tag == "Durations":
                    durations = []
                    for durationElement in actionElement:
                        try:
                            durationValue = int(durationElement.text)
                        except ValueError:
                            return self.createErrorPopup(f"{name} animation has an invalid duration value. Cannot be {durationElement.text}")

                        if durationValue <= 0:
                            return self.createErrorPopup(f"{name} animation has invalid duration value. Cannot be {durationValue}")

                        durations.append(durationValue)

            # After all checks, lets create some data.
            if copyName:
                group = AnimGroup(actionIdx, name, copyName=copyName)
                copyGroups.append(group)
                self.groups.append(group)
                item = AnimGroupItem(group, self)
                self.ui.actionListWidget.addItem(item)
                continue

            animFile = f"{name}-Anim.png"
            animImagePath = os.path.join(dirName, animFile)
            if not os.path.exists(animImagePath):
                return self.createErrorPopup(f"{animFile} not found.")

            offsetFile = f"{name}-Offsets.png"
            offsetImagePath = os.path.join(dirName, offsetFile)
            if not os.path.exists(animImagePath):
                return self.createErrorPopup(f"{offsetFile} not found.")

            shadowFile = f"{name}-Shadow.png"
            shadowImagePath = os.path.join(dirName, shadowFile)
            if not os.path.exists(animImagePath):
                return self.createErrorPopup(f"{shadowFile} not found.")

            animImage = Image.open(animImagePath)
            offsetImage = Image.open(offsetImagePath)
            shadowImage = Image.open(shadowImagePath)

            if (shadowImage.width != animImage.width or shadowImage.height != animImage.height or
                animImage.width != offsetImage.width or animImage.height != offsetImage.height or
                offsetImage.width != animImage.width or offsetImage.height != animImage.height):
                return self.createErrorPopup(f"Dimensions of Anims, Shadows, and Offsets do not match for {name}.")

            if frameWidth == 0 or frameHeight == 0:
                return self.createErrorPopup(f"Could not find frame dimensions for {name}.")

            if animImage.width % frameWidth != 0 or animImage.height % frameHeight != 0:
                return self.createErrorPopup(f"Animation must be divisible by frame dimensions for {name}.")

            frameXCount = animImage.width // frameWidth
            sequenceCount = frameYCount = animImage.height // frameHeight

            if len(durations) != frameXCount:
                return self.createErrorPopup("Amount of frame duration does not match number of frames.")

            if sequenceCount != 1 and sequenceCount != 8:
                return self.createErrorPopup(f"Frame count is not 1 or 8 for {name}.")

            group = AnimGroup(actionIdx, name, rushFrame, hitFrame, returnFrame)
            self.groups.append(group)
            item = AnimGroupItem(group, self)
            self.ui.actionListWidget.addItem(item)

            for i in range(sequenceCount):
                sequenceIdx = (sequenceCount - i) % sequenceCount

                for frameIdx in range(frameXCount):
                    startX, startY = frameIdx % frameXCount, sequenceIdx
                    l, t = startX * frameWidth, startY * frameHeight
                    r, b = l + frameWidth, t + frameHeight

                    obounds = (l, t, r, b)

                    frameImg = animImage.crop(obounds)

                    oFrameBox = frameImg.getbbox()
                    if oFrameBox:
                        croppedFrame = TLRectangle.fromBounds(oFrameBox)
                    else:
                        # No bounds found, it's possible the frame is empty. For example, an animation may temporarily
                        # make a character disappear/reappear. Create a frame at the center that's 1x1.
                        croppedFrame = TLRectangle(frameWidth // 2, frameHeight // 2, 1, 1)
                        oFrameBox = croppedFrame.bounds()

                    maxWidth = max(maxWidth, croppedFrame.width)
                    maxHeight = max(maxHeight, croppedFrame.height)

                    actionPointFrame = offsetImage.crop(obounds)

                    actionPointLoc = getActionPointsFromPILImage(actionPointFrame)

                    boundsCenter = croppedFrame.center

                    actionPoints = ActionPoints(Offset(*boundsCenter),
                                                Offset(*boundsCenter),
                                                Offset(*boundsCenter),
                                                Offset(*boundsCenter))

                    if actionPointLoc[0] and actionPointLoc[1] and actionPointLoc[2]:
                        center = actionPointLoc[1]
                        head = center
                        if actionPointLoc[3]:
                            head = actionPointLoc[3]

                        leftHand = actionPointLoc[0]
                        rightHand = actionPointLoc[2]
                        actionPoints = ActionPoints(leftHand, center, rightHand, head)
                    elif actionPointLoc[0] or actionPointLoc[1] or actionPointLoc[2] or actionPointLoc[3]:
                        return self.createErrorPopup(f"Error decoding action points from offsets image. Frame Index: {frameIdx}")

                    # Position relative to 0, 0.
                    actionPoints.add(Offset(-boundsCenter[0], -boundsCenter[1]))

                    offsetRect = actionPoints.getRect()
                    cOffsetRect = centerBounds(offsetRect)

                    maxWidth = max(maxWidth, cOffsetRect.width)
                    maxHeight = max(maxHeight, cOffsetRect.height)

                    animFrame = AnimFrame(len(group.directions[sequenceIdx].frames))

                    offsetX = croppedFrame.x - ((frameWidth // 2) - (croppedFrame.width // 2) )
                    offsetY = croppedFrame.y - ((frameHeight // 2) - (croppedFrame.height // 2 ))

                    animFrame.spriteOffset = Offset(offsetX, offsetY)
                    animFrame.duration = durations[frameIdx]

                    if shadowImage:
                        simage = shadowImage.crop(obounds)

                        if shadowOffset := getShadowLocationFromPILImage(simage):
                            animFrame.shadowOffset.x = shadowOffset.x - frameWidth // 2
                            animFrame.shadowOffset.y = shadowOffset.y - frameHeight // 2
                    else:
                        animFrame.shadowOffset.x = 0
                        animFrame.shadowOffset.y = -(croppedFrame.y - frameHeight // 2) // 2

                    frames.append((frameImg.crop(oFrameBox), actionPoints))
                    framesToSequence.append((actionIdx, sequenceIdx, frameIdx))
                    group.directions[sequenceIdx].frames.append(animFrame)

            if sequenceCount == 1:
                collapsedAnims.append(group)


        maxWidth = roundUpToMult(maxWidth, 2)
        maxHeight = roundUpToMult(maxHeight, 2)

        uniqueImages, oldFrameToNewFrame, uniqueBodyPoints = checkDuplicateImages(frames, True)

        maxTexSize = int(math.ceil(math.sqrt(len(uniqueImages))))

        singleSheetSize = (maxWidth * maxTexSize, maxHeight * maxTexSize)

        # Create single sheet
        sheet = Image.new("RGBA", singleSheetSize, (0, 0, 0, 0))

        apSheet = Image.new("RGBA", singleSheetSize, (0, 0, 0, 0))

        apDraw = ImageDraw.Draw(apSheet)

        # Map the positions of the frames to their sheet positions.
        for frameIdx, uI in enumerate(uniqueImages):
            diffX = maxWidth // 2 - uI.width // 2
            diffY = maxHeight // 2 - uI.height // 2
            startX = maxWidth * (frameIdx % maxTexSize)
            startY = (maxHeight * (frameIdx // maxTexSize))

            sheet.paste(uI, (startX + diffX, startY + diffY))

            # Create an Offset sheet.
            bpStartX = startX + maxWidth // 2
            bpStartY = startY + maxHeight // 2

            startOffset = Offset(bpStartX, bpStartY)

            bp = uniqueBodyPoints[frameIdx]
            lh = (startOffset + bp.leftHand).data()
            center  = (startOffset + bp.center).data()
            rh = (startOffset + bp.rightHand).data()
            head = (startOffset + bp.head).data()

            positions = defaultdict(list)
            positions[lh].append((255, 0, 0, 255))
            positions[center].append((0, 255, 0, 255))
            positions[head].append((90, 0, 0, 255))
            positions[rh].append((0, 0, 255, 255))

            for pos, color in overlapColors(positions).items():
                apDraw.point(pos, fill=color)

            self.actionPoints[frameIdx] = uniqueBodyPoints[frameIdx]

        flippedFrames = set()
        # Now we need to go through and update the data with the correct frame indexes.
        for oldId, oldFrame in enumerate(frames):
            oldInfo = framesToSequence[oldId]
            groups = [group for group in self.groups if group.idx == oldInfo[0]]
            group = groups[0]
            newFrame = group.directions[oldInfo[1]].frames[oldInfo[2]]
            changedFrame = oldFrameToNewFrame[oldId]
            newFrame.frameIndex = changedFrame.frameIndex
            newFrame.flip = changedFrame.flip

            if newFrame.flip:
                flippedFrames.add(newFrame.frameIndex)
                if oldFrame[0].width % 2 == 1:
                    newFrame.spriteOffset.x += 1

        # Flip back clockwise.
        for group in self.groups:
            group.directions = [group.directions[0], *reversed(group.directions[1:])]

            for sequence in group.directions:
                for frame in sequence.frames:
                    frame.reset()

        # Now that we have proper frames, copy the collapsed ones to all directions.
        for collapsedAnim in collapsedAnims:
            for si in range(1, 8):
                newSequence = copy.deepcopy(collapsedAnim.directions[0])
                collapsedAnim.directions[si] = newSequence

        # Unfortunately copy actions can come before the action they need to copy? Check after we have parsed all actions.
        # Some copy actions don't even have action indexes... Indexes currently have no use according to SkyTemple.
        for groups in copyGroups:
            name, copyName = groups.name, groups.copyName
            # Find copy group
            found = False
            for currentGroup in self.groups:
                if currentGroup.name == copyName:
                    found = currentGroup
                    break

            if found:
                # Find destination group.
                for currentGroup in self.groups:
                    if currentGroup.name == name:
                        group = copy.deepcopy(found)
                        currentGroup.rushFrame = group.rushFrame
                        currentGroup.hitFrame = group.hitFrame
                        currentGroup.returnFrame = group.returnFrame
                        currentGroup.directions = group.directions

            else:
                print(f"Copy {name} not found")
                continue

        self.sheetImage = pyglet.image.ImageData(sheet.width, sheet.height, 'RGBA', sheet.tobytes(), pitch=-sheet.width * 4)

        self.frameWidth = maxWidth
        self.frameHeight = maxHeight
        self.shadowSize = shadowSize

        self.actionPtImage = pyglet.image.ImageData(apSheet.width, apSheet.height, 'RGBA', apSheet.tobytes(),
                                                 pitch=-apSheet.width * 4)

        self.imageGrid = TopLeftGrid(self.sheetImage,
                                     rows=maxTexSize,
                                     columns=maxTexSize)

        self.actionGrid = TopLeftGrid(self.actionPtImage,
                                     rows=maxTexSize,
                                     columns=maxTexSize)

        self._addFramesFromGrid()

        self.addRecentList(fileName)

        self.ui.statusBar.showMessage("Frame data and images loaded successfully.", 3000)

    def _saveExportFrameData(self, fileName, frameSizes: dict[str, Tuple[int, int]]):
        existingRoot = self.loadedTree.getroot()

        root = ElementTree.Element("AnimData")

        ElementTree.SubElement(root, "ShadowSize").text = existingRoot.find("ShadowSize").text

        animsEl = ElementTree.SubElement(root, "Anims")

        trim = self.ui.actionTrim_Copies.isChecked()

        for groupAnim in self.groups:
            animEl = ElementTree.SubElement(animsEl, "Anim")
            size = frameSizes[groupAnim.name] if groupAnim.name in frameSizes else None
            self.createBaseAnimGroupXML(animEl, groupAnim.name, groupAnim.idx, groupAnim, trim=trim,
                                        copyName=groupAnim.copyName, size=size)

            if not groupAnim.copyName:
                self.createMultiSheetFrameData(animEl, groupAnim)

        ElementTree.indent(root)

        if not fileName:
            # Use loaded file name
            fileName = self.fileName

            # Reset saves.
            for item in self._getActionListItems():
                item.animGroup.modified = False
                item.updateText()

        tree = ElementTree.ElementTree(root)
        tree.write(fileName, encoding='utf-8', xml_declaration=True)

    def exportSingleSheet(self):
        if self.loadedTree:
            options = QFileDialog.Options()
            options |= QFileDialog.ShowDirsOnly

            directory = QFileDialog.getExistingDirectory(self.window, "Select Directory for Single Sheet", "", options)

            if directory:
                self._exportSingleSheet(directory)

    def _exportSingleSheet(self, directory):
        self._saveFrameData(f"{directory}/FrameData.xml")

        self.sheetImage.save(f"{directory}/Anim.png")

        if self.actionPtImage:
            self.actionPtImage.save(f"{directory}/Offsets.png")

        self.ui.statusBar.showMessage("Single sheet was output successfully.", 3000)

    def exportMultipleSheets(self):
        if self.loadedTree:
            options = QFileDialog.Options()
            options |= QFileDialog.ShowDirsOnly

            directory = QFileDialog.getExistingDirectory(self.window, "Select Directory for Multi-Sheets", "", options)

            if directory:
                self._exportMultipleSheets(directory)

    def _exportMultipleSheets(self, filePath: str):
        if not self.loadedTree:
            return

        baseFrame = self.imageGrid[0]
        frameWidth, frameHeight = baseFrame.width, baseFrame.height

        croppedBounds = []
        croppedImages = []

        croppedActionPts = []

        actionRects = []
        frameRects = []

        # Use PIL For image operations as it's faster than directly accessing bytes.
        sheetPilImage = Image.frombytes('RGBA', (self.sheetImage.width, self.sheetImage.height),
                                   self.sheetImage.get_image_data().get_data('RGBA', self.sheetImage.width * 4))

        actionPilImage = Image.frombytes('RGBA', (self.actionPtImage.width, self.sheetImage.height),
                                   self.actionPtImage.get_image_data().get_data('RGBA', self.sheetImage.width * 4))

        shadowPilImage = Image.frombytes('RGBA', (self.shadowImage.width, self.shadowImage.height),
                                    self.shadowImage.get_image_data().get_data('RGBA'))

        # FLIP due to data upside down.
        sheetPilImage = sheetPilImage.transpose(Image.FLIP_TOP_BOTTOM)
        actionPilImage = actionPilImage.transpose(Image.FLIP_TOP_BOTTOM)
        shadowPilImage = shadowPilImage.transpose(Image.FLIP_TOP_BOTTOM)

        for i in range(len(self.imageGrid)):
            startX, startY = i % self.imageGrid.columns, i // self.imageGrid.columns
            l, t = startX * frameWidth, startY * frameHeight
            r, b = l + frameWidth, t + frameHeight

            obounds = (l, t, r, b)
            originalFrame = sheetPilImage.crop(obounds)
            oActionFrame = actionPilImage.crop(obounds)

            frameBox = originalFrame.getbbox()
            actionBox = oActionFrame.getbbox()

            # No bounds. Empty frame.
            if not frameBox:
                # If no action box, it shouldn't be output?
                if not actionBox:
                    continue
                else:
                    frameBox = (l, t, l+1, b+1)

            bounds = TLRectangle.fromBounds(frameBox)
            actionBounds = TLRectangle.fromBounds(actionBox)

            croppedBounds.append(bounds)

            frameBound = bounds + (-frameWidth // 2, -frameHeight // 2)
            #frameBound += actRects[i]

            frameRects.append(frameBound)
            actionRects.append(actionBounds + (-frameWidth // 2, -frameHeight // 2))

            croppedImages.append(originalFrame.crop(frameBox))
            croppedActionPts.append(oActionFrame.crop(actionBox))


        groupSizes: dict[str, Tuple[int, int]] = {}

        for groupId, animGroup in enumerate(self.groups):
            # Skip copies.
            if animGroup.copyName != "":
                continue

            # Max amount of sequences.
            maxSequence = 0

            # Frames go counter clockwise, but now you want to go clockwise...?
            directions = [animGroup.directions[0], *reversed(animGroup.directions[1:])]

            maxWidth = maxHeight = 0

            offsetRects = {}

            collapsed = False
            if self.ui.actionCollapse_Singles.isChecked():
                collapsed = self.isSequenceCollapsable(animGroup)

            # Search all frames in the animation for the bounds that will fit the separated sheet.
            for dirIdx, sequence in enumerate(directions):
                # Determine the maximum bounds for all frames in the sequence.
                maxSequence = max(maxSequence, len(sequence.frames))
                for frameIdx, frame in enumerate(sequence.frames):
                    croppedRect = croppedBounds[frame.frameIndex]

                    # Get the biggest frame size we need. Offsets are expanded by 2x to make it centerable.
                    adjusted_width = croppedRect.width + abs(frame.spriteOffset.x) * 2
                    adjusted_height = croppedRect.height + abs(frame.spriteOffset.y) * 2

                    offsetRects[(dirIdx, frameIdx)] = croppedRect + frame.spriteOffset

                    maxWidth = max(maxWidth, adjusted_width)
                    maxHeight = max(maxHeight, adjusted_height)

            # Round up the boxes to the nearest eighth.
            maxWidth = int(roundUpToMult(maxWidth, 8))
            maxHeight = int(roundUpToMult(maxHeight, 8))

            groupSizes[animGroup.name] = (maxWidth, maxHeight)

            if collapsed:
                directionCount = 1
            else:
                directionCount = 8

            # Now lets output the texture.
            newAnimImage = Image.new("RGBA", (maxWidth * maxSequence, maxHeight * directionCount), (0, 0, 0, 0))
            newActionPtImage = Image.new("RGBA", (maxWidth * maxSequence, maxHeight * directionCount), (0, 0, 0, 0))
            newShadowImage = Image.new("RGBA", (maxWidth * maxSequence, maxHeight * directionCount), (0, 0, 0, 0))

            # Go over all sequences and frames.
            for dirIdx, sequence in enumerate(directions):
                startY = dirIdx * maxHeight
                maxSequence = max(maxSequence, len(sequence.frames))

                for frameIdx, frame in enumerate(sequence.frames):
                    croppedImg = croppedImages[frame.frameIndex]
                    croppedOffset = croppedActionPts[frame.frameIndex]

                    frameBounds = croppedBounds[frame.frameIndex]
                    actionBound = actionRects[frame.frameIndex]

                    if frame.flip:
                        croppedImg = croppedImg.transpose(Image.FLIP_LEFT_RIGHT)
                        croppedOffset = croppedOffset.transpose(Image.FLIP_LEFT_RIGHT)

                        frameBounds = Rectangle(-frameBounds.right + frameWidth, frameBounds.y, frameBounds.width, frameBounds.height)
                        actionBound = actionBound.getFlip()

                    translatedRect = centerAndApplyOffset(maxWidth, maxHeight,
                                                          frameBounds, frame.flip,
                                                          frame.spriteOffset)

                    startX = (frameIdx * maxWidth)

                    newAnimImage.paste(croppedImg,
                                      (startX + int(translatedRect[0]), startY + int(translatedRect[1]))
                                      )

                    actPtX = (maxWidth // 2) + frame.spriteOffset.x + actionBound.x
                    actPtY = (maxHeight // 2) + frame.spriteOffset.y + actionBound.y

                    newActionPtImage.paste(croppedOffset,
                                           (startX + actPtX, startY + actPtY))

                    shadowPtX = -(self.shadowImage.width // 2) + (maxWidth // 2) + frame.shadowOffset.x
                    shadowPtY = -(self.shadowImage.height // 2) + (maxHeight // 2) + frame.shadowOffset.y

                    newShadowImage.paste(shadowPilImage,
                                         (startX + shadowPtX, startY + shadowPtY))

                if collapsed:
                    break

            newAnimImage.save(f"{filePath}/{animGroup.name}-Anim.png")
            newActionPtImage.save(f"{filePath}/{animGroup.name}-Offsets.png")
            newShadowImage.save(f"{filePath}/{animGroup.name}-Shadow.png")

        self._saveExportFrameData(f"{filePath}/AnimData.xml", groupSizes)

        self.ui.statusBar.showMessage("Multisheet frames were output successfully.", 3000)

    def getAttachmentPointsFromTexture(self, path):
        if os.path.join(path, 'Offsets.png'):
            pass

    def exitApplication(self):
        sys.exit(app.exec_())


def excepthook(exc_type, exc_value, exc_tb):
    tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(tb)


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    ui = Ui_MainWindow()
    sys.excepthook = excepthook
    mainWindow = QtWidgets.QMainWindow()
    ui.setupUi(mainWindow)
    implementation = AnimationEditor(app, mainWindow, ui)
    mainWindow.show()
    sys.exit(app.exec_())
