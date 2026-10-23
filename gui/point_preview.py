"""In-memory Action Point previews over independently owned editor documents."""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4

import pyglet
from PySide6 import QtCore, QtWidgets

from gui import ScreenFitDialog
from gui.preview import TopLeftGrid
from preview import (
    ActionPointRelationship,
    Presentation,
    ResolutionFailure,
    resolve_relationship,
    sample_sequence,
    sample_sprite,
    synchronized_time,
)
from project import available_point_names


@dataclass
class PreviewSprite:
    editor: object
    identity: str = field(default_factory=lambda: "point-preview:" + uuid4().hex)
    parent_point: str = ""
    child_point: str = ""
    display_name: str = ""
    follow_facing: bool = True
    synchronized: bool = False
    follow_action: bool = False
    inherit_mirroring: bool = False
    visible: bool = True
    playing: bool = False
    ticks: float = 0
    selection: object = None
    frame: object = None
    direction: int = 0
    revision: object = None
    warning: str = ""
    source_warning: str = ""
    shadow_visible: bool = True
    standalone_shadow_visible: bool = True


class ActionPointPreview:
    """Preview state only: sources retain their editors, undo stacks and saves.

    Each participating resource uses its existing document page and GL view.
    Layer tabs select those pages; they never create editable source copies.
    The initial preview is flat: every new child follows the root instance.
    """

    def __init__(self, root):
        if root.point_preview is not None:
            msg = "This resource already belongs to a point preview."
            raise ValueError(msg)
        self.root = root
        self.members = [PreviewSprite(root)]
        self.adopt_playback(self.members[0])
        self.members[0].shadow_visible = self.members[0].standalone_shadow_visible
        self.members[0].revision = self.source_stamp(root)
        self.order = [self.members[0].identity]
        self.views = {}
        self.last_time = time.monotonic()
        self.last_refresh = 0
        self.timer = QtCore.QTimer(root.window)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)
        root.point_preview = self
        self.timer.start()

    def member(self, editor):
        return next(m for m in self.members if m.editor is editor)

    @staticmethod
    def label(editor):
        if editor.point_preview is not None:
            member = next((m for m in editor.point_preview.members if m.editor is editor), None)
            if member and member.display_name:
                return member.display_name
        path = Path(editor.file_name) if editor.file_name else None
        return (path.parent.name if path.stem.lower() in ("sprite", "animdata", "framedata") else path.stem) if path else getattr(editor, "untitled_name", "Untitled")

    def add_sprite(self, editor, *, parent_point="", child_point=""):
        if editor.point_preview is not None:
            msg = "This sprite already belongs to a point preview. Remove it there first."
            raise ValueError(msg)
        if len(self.members) >= 11:
            msg = "The preview supports one root sprite plus 10 additional sprites."
            raise ValueError(msg)
        member = PreviewSprite(editor, parent_point=parent_point, child_point=child_point)
        self.adopt_playback(member)
        # Attachments do not each cast an extra copy of the trainer's shadow.
        member.shadow_visible = False
        member.revision = self.source_stamp(editor)
        self.members.append(member)
        self.order.append(member.identity)
        editor.point_preview = self
        self.refresh()
        return member

    @staticmethod
    def adopt_playback(member):
        editor = member.editor
        member.standalone_shadow_visible = editor.shadow.visible if editor.shadow is not None else True
        member.playing = editor.animating
        displayed = (editor.displayed_anim_frame or editor.current_anim_frame) if member.playing else editor.current_anim_frame
        pyglet.clock.unschedule(editor._playing_animation)
        editor.animating = False
        if displayed is not None:
            editor.current_anim_frame = displayed
            with (QtCore.QSignalBlocker(editor.ui.frameSlider),
                  QtCore.QSignalBlocker(editor.ui.animationFrameList)):
                editor.ui.frameSlider.setValue(displayed.idx)
                editor.ui.animationFrameList.setCurrentRow(displayed.idx)
            editor.set_anim_frame_values(displayed)

    def bind(self, member, parent_point, child_point, *, follow_facing=True,
             synchronized=False, follow_action=False, inherit_mirroring=False):
        member.parent_point, member.child_point = parent_point, child_point
        member.follow_facing = follow_facing
        member.synchronized = synchronized
        member.follow_action = follow_action
        member.inherit_mirroring = inherit_mirroring
        self.refresh()

    def select(self, identity):
        member = next((m for m in self.members if m.identity == identity), None)
        editor = member.editor if member else self.root
        host = self.root.document_host
        page = next((page for page, source in host._editors.items() if source is editor), None)
        if page is not None:
            host.documentTabs.setCurrentWidget(page)
            editor.workspace.select(0)

    def refresh(self):
        for member in self.members:
            member.editor.workspace._refresh_tabs()
            member.editor.openGLWidget.update()

    def refresh_tabs(self, editor, tabs):
        tabs.set_main_sprite(self.members[0].identity)
        active = self.root.document_host.current_editor
        source = next((m for m in self.members if m.editor is active), self.member(editor))
        selected = source.identity
        self.order = [key for key in self.order if any(m.identity == key for m in self.members)]
        with QtCore.QSignalBlocker(tabs):
            while tabs.count():
                tabs.removeTab(0)
            for identity in self.order:
                member = next((m for m in self.members if m.identity == identity), None)
                title = self.label(member.editor)
                index = tabs.addTab(title)
                tabs.setTabData(index, identity)
                role = "Main sprite" if member is self.members[0] else "Preview sprite"
                tabs.setTabToolTip(index, f"{role}: {member.editor.file_name or title} — resource edits use its own Save/Undo; preview binding is editor state")
                if identity == selected:
                    tabs.setCurrentIndex(index)
            index = tabs.addTab("+")
            tabs.setTabData(index, "__add_layer__")
            tabs.setTabEnabled(index, len(self.order) < 11)
        editor.ui.pointPreviewControls.show()
        editor.ui.pointPreviewRelationshipButton.setEnabled(editor is not self.root)
        self.update_label(editor)

    def update_label(self, editor):
        member = next((m for m in self.members if m.editor is editor), None)
        if member is None:
            return
        resource = editor.file_name or self.label(editor)
        short_resource = str(Path(*Path(editor.file_name).parts[-2:])) if editor.file_name else resource
        editor.ui.pointPreviewLabel.setText(f"Editing: {short_resource}")
        editor.ui.pointPreviewLabel.setToolTip(f"{resource}\nEdits, Save and Undo apply to this sprite resource.")
        timing = "Synced" if member.synchronized else "Independent"
        details = ["Main sprite", f"Timing: {timing}"]
        tooltip = [f"Main sprite: {self.label(self.root)}", f"Timeline: {timing.lower()}"]
        if member is not self.members[0]:
            details = [f"{member.parent_point or '?'} → {member.child_point or '?'}", f"Timing: {timing}"]
            tooltip[0] = f"{self.label(self.root)}.{member.parent_point or '?'} → {self.label(editor)}.{member.child_point or '?'}"
            follows = []
            if member.follow_facing:
                follows.append("Facing")
                tooltip.append("Facing follows the main sprite; direction controls turn the preview together.")
            if member.follow_action:
                follows.append("Action")
                tooltip.append("Selects the main sprite's action name; a missing action hides this sprite.")
            if member.inherit_mirroring:
                follows.append("Mirror")
                tooltip.append("Artwork mirroring follows the main sprite.")
            details.append("Follow: " + (", ".join(follows) if follows else "None"))
        warning = member.source_warning or member.warning
        if warning:
            details = details[:2] + [f"Warning: {warning}"]
            tooltip.append(warning)
        editor.ui.pointPreviewDetailsLabel.setText("\n".join(details))
        editor.ui.pointPreviewDetailsLabel.setToolTip("\n".join(tooltip))
        effective_playing = self.members[0].playing if member.synchronized else member.playing
        editor.ui.playButton.setPlaying(effective_playing)
        editor.ui.pointPreviewPlayButton.setText("Pause Together" if any(m.playing and not m.synchronized for m in self.members) else "Play Together")

    def reorder(self, identities):
        self.order = [key for key in identities if key in self.order]
        QtCore.QTimer.singleShot(0, self.refresh)

    def toggle(self, editor):
        member = self.member(editor)
        if member.synchronized:
            member = self.members[0]
        member.playing = not member.playing
        self.last_time = time.monotonic()
        self.update_label(editor)

    def pause_for_edit(self, editor):
        member = self.member(editor)
        member.playing = False
        if member.synchronized:
            self.members[0].playing = False
        self.update_label(editor)

    def play_together(self):
        playing = not any(m.playing and not m.synchronized for m in self.members)
        for member in self.members:
            member.playing = playing
        self.last_time = time.monotonic()
        self.refresh()

    def toggle_shadow(self, editor):
        member = self.member(editor)
        member.shadow_visible = not member.shadow_visible
        self.refresh()

    def advance(self, seconds):
        """Each instance consumes its own selected sequence and elapsed time."""
        for member in self.members:
            editor = member.editor
            group = editor.current_anim_group
            if editor is not self.root and member.follow_action:
                parent_group = self.root.current_anim_group
                target = next((g for g in editor.groups if parent_group and g.name == parent_group.name), None)
                if target is not None and target is not group:
                    elapsed = member.ticks
                    editor.openGLWidget.makeCurrent()
                    editor.openGLWidget._activate_context() if hasattr(editor.openGLWidget, "_activate_context") else None
                    row = editor.groups.index(target)
                    editor.ui.actionListWidget.setCurrentRow(row)
                    editor.ui.actionListWidget.item(row).mouseClickEvent(None)
                    member.ticks = elapsed
                    member.selection = (target.name, editor.current_anim_frame.idx if editor.current_anim_frame else None)
                group = target
            direction = self.root.current_direction if member.follow_facing else editor.current_direction
            facing_changed = editor is not self.root and member.follow_facing and editor.current_direction != direction
            if facing_changed:
                editor.openGLWidget.makeCurrent()
                editor.set_direction(direction)
                editor.directionButtons[direction].setChecked(True)
                selected = editor.current_anim_frame
                # Turning selects directional artwork, not a new playback clock.
                member.selection = (group.name if group else None, selected.idx if selected else None)
            member.direction = direction
            frames = group.directions[direction].frames if group else []
            selected = editor.current_anim_frame
            token = (group.name if group else None, selected.idx if selected else None)
            if member.selection != token:
                member.selection = token
                member.ticks = sum(f.duration for f in frames[:selected.idx]) if selected else 0
            if member.playing and not member.synchronized:
                member.ticks += seconds / editor.anim_speed
            if member.synchronized:
                root = self.members[0]
                parent_group = self.root.current_anim_group
                parent_frames = parent_group.directions[self.root.current_direction].frames if parent_group else []
                member.ticks = synchronized_time(parent_frames, frames, root.ticks)
            member.frame = None
            if frames:
                member.frame = sample_sequence(frames, member.ticks)
            if (member.playing or member.synchronized or facing_changed) and member.frame is not None and (
                member.frame is not editor.current_anim_frame or member.frame is not editor.displayed_anim_frame
            ):
                frame = member.frame
                editor.current_anim_frame = editor.displayed_anim_frame = frame
                with (QtCore.QSignalBlocker(editor.ui.frameSlider),
                      QtCore.QSignalBlocker(editor.ui.animationFrameList)):
                    editor.ui.frameSlider.setValue(frame.idx)
                    editor.ui.animationFrameList.setCurrentRow(frame.idx)
                editor.openGLWidget.makeCurrent()
                editor.set_anim_frame_values(frame)
                member.selection = (group.name, frame.idx)

    def tick(self):
        now = time.monotonic()
        self.advance(max(0, now - self.last_time))
        self.last_time = now
        if now - self.last_refresh > .5:
            self.last_refresh = now
            self.refresh_sources()
        for member in self.members:
            self.update_label(member.editor)
            member.editor.openGLWidget.update()

    def resolve(self, presentation=Presentation()):
        self.advance(0)
        results = {}
        parent = self.members[0]
        parent.warning = ""
        if parent.frame is None:
            parent.warning = f"{self.label(self.root)}: no artwork for the selected action/direction."
            for child in self.members[1:]:
                child.warning = f"{self.label(self.root)}: no artwork for the selected action/direction."
            return results
        root_sample = sample_sprite(parent.identity, self.root, parent.frame, facing=parent.direction, presentation=presentation)
        results[parent.identity] = (root_sample, presentation.point(root_sample.sprite_offset))
        for child in self.members[1:]:
            child.warning = ""
            if child.frame is None:
                child.warning = f"{self.label(child.editor)}: no artwork for the selected action/direction."
                if child.follow_action and self.root.current_anim_group and not any(g.name == self.root.current_anim_group.name for g in child.editor.groups):
                    child.warning = f"{self.label(child.editor)}: action {self.root.current_anim_group.name!r} is missing. Choose a child action or disable following the parent's action name."
                continue
            sample = sample_sprite(child.identity, child.editor, child.frame, facing=child.direction,
                                   presentation=Presentation(scale=presentation.scale))
            if child.inherit_mirroring:
                sample = replace(sample, mirrored=bool(sample.mirrored) ^ bool(root_sample.mirrored))
            if not child.parent_point or not child.child_point:
                child.warning = "Choose Action Point Relationship… on this preview tab."
                continue
            relation = ActionPointRelationship(parent.identity, child.parent_point, child.identity, child.child_point)
            placement = resolve_relationship(relation, root_sample, sample)
            if isinstance(placement, ResolutionFailure):
                child.warning = f"{self.label(self.root) if placement.instance_id == parent.identity else self.label(child.editor)}: {placement.message}"
                continue
            results[child.identity] = (replace(sample, presentation=placement.presentation), placement.artwork_origin)
        return results

    def refresh_sources(self):
        """Refresh clean native sources only; never overwrite unsaved authoring."""
        from conversion import SpriteDocument
        from utils import FormatError
        host = self.root.document_host
        for member in self.members:
            editor = member.editor
            if not editor.file_name or Path(editor.file_name).suffix.lower() != ".json":
                continue
            paths = [Path(editor.file_name), Path(editor.file_name).with_name(editor.texture_name)]
            try:
                revision = tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
                if revision == member.revision:
                    continue
                if member.revision is None:
                    member.revision = revision
                    continue
                page = next(page for page, source in host._editors.items() if source is editor)
                if host._saved_states[page] != host._document_state(editor) or editor.pointDragState is not None or editor.editDepth:
                    member.source_warning = "Source changed externally; save or discard resource edits before reloading."
                    continue
                # Validate before using the UI loader so invalid external writes
                # never cause repeated modal dialogs during playback.
                SpriteDocument.load(paths[0])
                action = editor.current_anim_group.name if editor.current_anim_group else None
                direction = editor.current_direction
                row = editor.current_anim_frame.idx if editor.current_anim_frame else 0
                if editor.load_animation(paths[0], add_to_recent=False):
                    item = next((item for item in editor._get_action_list_items() if item.anim_group.name == action), None)
                    if item:
                        editor.ui.actionListWidget.setCurrentItem(item)
                        item.mouseClickEvent(None)
                    editor.set_direction(direction)
                    editor.ui.frameSlider.setValue(row)
                    editor.undoStack.clear()
                    page = next(page for page, source in host._editors.items() if source is editor)
                    host._saved_states[page] = host._document_state(editor)
                    member.revision = revision
                    member.source_warning = ""
            except (OSError, ValueError, FormatError) as error:
                member.source_warning = f"Source refresh failed: {error}"

    @staticmethod
    def source_stamp(editor):
        if not editor.file_name:
            return None
        try:
            return tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in (
                Path(editor.file_name), Path(editor.file_name).with_name(editor.texture_name)))
        except OSError:
            return None

    def source_saved(self, editor):
        member = self.member(editor)
        member.revision = self.source_stamp(editor)
        member.source_warning = ""
        self.refresh()

    def save_dialog(self):
        from formats.native import relative_sprite_path
        from preview import FORMAT, write_workspace
        from utils import FormatError
        try:
            if any(not m.editor.file_name for m in self.members):
                msg = "Save each sprite resource before saving its preview references."
                raise FormatError(msg)
            default = str(Path(self.root.file_name).with_suffix(".preview.json"))
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self.root.window, "Save Editor Preview", default, "Editor Preview (*.preview.json)")
            if not path:
                return False
            if not path.lower().endswith(".preview.json"):
                path += ".preview.json"
            directory = Path(path).parent
            parts = []
            for member in self.members[1:]:
                part = {name: getattr(member, name) for name in ("parent_point", "child_point", "follow_facing", "follow_action", "synchronized", "inherit_mirroring", "visible")}
                part["sprite"] = relative_sprite_path(Path(member.editor.file_name), directory)
                part["action"] = member.editor.current_anim_group.name if member.editor.current_anim_group else ""
                part["name"] = member.display_name
                parts.append(part)
            write_workspace(path, {"format": FORMAT, "version": 1, "root": relative_sprite_path(Path(self.root.file_name), directory),
                                    "root_visible": self.members[0].visible, "root_name": self.members[0].display_name,
                                    "parts": parts, "order": [next(i for i,m in enumerate(self.members) if m.identity == key) for key in self.order]})
            return True
        except (FormatError, OSError, ValueError) as error:
            self.root.create_error_popup(str(error))
            return False

    def release_view(self, editor):
        cache = self.views.pop(editor, {})
        if cache:
            editor.openGLWidget.makeCurrent()
            editor.openGLWidget._activate_context()
            for _, sprite in cache.values():
                sprite.delete()

    def remove(self, editor):
        removing = list(self.members) if editor is self.root else [self.member(editor)]
        # Context-owned sprites must be released before any document GL teardown.
        for viewer in list(self.views):
            self.release_view(viewer)
        for member in removing:
            member.editor.point_preview = None
            member.editor.ui.pointPreviewControls.hide()
            self.members.remove(member)
            member.editor.workspace._refresh_tabs()
            member.editor.set_animation()
            self.restore_visibility(member.editor, member.standalone_shadow_visible)
            member.editor.openGLWidget.update()
        if not self.members or len(self.members) == 1:
            if self.members:
                remaining = self.members.pop()
                root = remaining.editor
                root.point_preview = None
                root.ui.pointPreviewControls.hide()
                root.workspace._refresh_tabs()
                root.set_animation()
                self.restore_visibility(root, remaining.standalone_shadow_visible)
            self.timer.stop()
            self.timer.deleteLater()
        else:
            self.refresh()

    @staticmethod
    def restore_visibility(editor, shadow_visible):
        if editor.sprite:
            editor.sprite.visible = editor.workspace.body_visible
        if editor.shadow:
            editor.shadow.visible = shadow_visible

    def render(self, viewer):
        """Draw in the viewer's GL context using resolver-derived positions."""
        gl = viewer.openGLWidget
        presentation = Presentation((gl.width() // 2, gl.height() // 3), (viewer.scale, -viewer.scale))
        try:
            results = self.resolve(presentation)
        finally:
            # Sampling can update other document controls and activate their GL
            # contexts. All cached sprites/VAOs below belong to this viewer,
            # and Qt's framebuffer must also be rebound before painting it.
            gl.makeCurrent()
        cache = self.views.setdefault(viewer, {})
        for marker in (*viewer.ap_sprites, *viewer.onionSprites, *viewer.onion_shadow_sprites):
            marker.visible = False
        if viewer.sprite:
            viewer.sprite.visible = False
        if viewer.shadow:
            viewer.shadow.visible = False
        for _, sprite in cache.values():
            sprite.visible = False

        def draw(key, image_source, rows, columns, frame, position, mirrored, order, stamp, opacity=255, color=(255, 255, 255)):
            entry = cache.get(key)
            if entry is None or entry[0][0] != stamp:
                if entry:
                    entry[1].delete()
                # Current artwork and onion ghosts share one atlas per context.
                grid = next((cached[0][1] for cached in cache.values() if cached[0][0] == stamp), None)
                if grid is None:
                    data = image_source.get_image_data() if hasattr(image_source, "get_image_data") else None
                    raw = data.get_data("RGBA", -image_source.width * 4) if data else image_source.tobytes()
                    image = pyglet.image.ImageData(image_source.width, image_source.height, "RGBA", raw, pitch=-image_source.width * 4)
                    grid = TopLeftGrid(image, rows=rows, columns=columns)
                sprite = pyglet.sprite.Sprite(grid[0], batch=gl.batch, group=pyglet.graphics.Group(20 + order))
                entry = cache[key] = ((stamp, grid), sprite)
            grid, sprite = entry[0][1], entry[1]
            image = grid[frame].get_texture()
            if mirrored:
                image = image.get_transform(flip_x=True)
            # Full logical cells retain the native center-relative origin.
            image.anchor_x, image.anchor_y = image.width // 2, image.height - image.height // 2
            sprite.image = image
            sprite.position = (*position, 0)
            sprite.scale = viewer.scale
            sprite.group = pyglet.graphics.Group(20 + order)
            sprite.opacity = opacity
            sprite.color = color
            sprite.visible = True
            return sprite

        member = self.member(viewer)
        if member.visible and member.identity in results:
            for index, (neighbor, distance) in enumerate(viewer.onionPreviewNeighbors(member.frame)):
                sample = sample_sprite(member.identity, viewer, neighbor, facing=member.direction,
                                       presentation=presentation if viewer is self.root else Presentation(scale=presentation.scale))
                if viewer is self.root:
                    origin = presentation.point(sample.sprite_offset)
                else:
                    parent = self.members[0]
                    parent_sample = results[parent.identity][0]
                    if member.inherit_mirroring:
                        sample = replace(sample, mirrored=bool(sample.mirrored) ^ bool(parent_sample.mirrored))
                    relation = ActionPointRelationship(parent.identity, member.parent_point, member.identity, member.child_point)
                    placement = resolve_relationship(relation, parent_sample, sample)
                    if isinstance(placement, ResolutionFailure):
                        continue
                    origin = placement.artwork_origin
                draw(("onion", member.identity, index), viewer.sheet_image, viewer.image_grid.rows,
                     viewer.image_grid.columns, sample.frame_index, origin, sample.mirrored, -19,
                     id(viewer.sheet_image), round(viewer.onionOpacity / distance), (190, 190, 190))
                if viewer.onion_shadows_enabled and member.shadow_visible:
                    shadow_artwork = viewer._display_shadow_image()
                    if viewer is self.root:
                        shadow_position = presentation.point(neighbor.shadow_offset.data())
                    else:
                        shadow_position = placement.presentation.point(neighbor.shadow_offset.data())
                    draw(("onion-shadow", member.identity, index), shadow_artwork, 1, 1, 0,
                         shadow_position, False, -21, (viewer.shadow_size, id(shadow_artwork)),
                         round(viewer.onionOpacity / distance))

        for order, identity in enumerate(self.order):
            member = next((m for m in self.members if m.identity == identity), None)
            if member is not None:
                if not member.visible or identity not in results:
                    continue
                sample, origin = results[identity]
                source = member.editor
                if member.shadow_visible:
                    shadow_artwork = source._display_shadow_image()
                    draw(("shadow", identity), shadow_artwork, 1, 1, 0,
                         sample.presentation.point(member.frame.shadow_offset.data()), False, -20,
                         (source.shadow_size, id(shadow_artwork)), source.ui.shadowOpacitySlider.value())
                sprite = draw(identity, source.sheet_image, source.image_grid.rows, source.image_grid.columns,
                              sample.frame_index, origin, sample.mirrored, order, id(source.sheet_image),
                              source.ui.spriteOpacitySlider.value())
                if source is viewer and viewer.sprite:
                    viewer.sprite.position = sprite.position
        member = next((m for m in self.members if m.editor is viewer), None)
        if member and member.identity in results and viewer.ui.showActionPointsCheckBox.isChecked():
            sample = results[member.identity][0]
            for index, name in enumerate(sample.action_points):
                position = sample.rendered_point(name)
                image = viewer.actionPointMarker(name)
                draw(("point", name), image, 1, 1, 0, position, False,
                     1000 + index, id(image), viewer.ui.actionPointsOpacitySlider.value())
        self.update_label(viewer)


class RelationshipDialog(ScreenFitDialog):
    def __init__(self, preview, member):
        super().__init__(preview.root.window)
        self.setWindowTitle("Action Point Relationship — Preview Only")
        self.preview, self.member = preview, member
        layout = QtWidgets.QFormLayout(self)
        layout.addRow("Parent sprite:", QtWidgets.QLabel(preview.label(preview.root)))
        self.parent_point = QtWidgets.QComboBox()
        self.child_point = QtWidgets.QComboBox()
        layout.addRow("Parent Action Point:", self.parent_point)
        layout.addRow("Child sprite:", QtWidgets.QLabel(preview.label(member.editor)))
        layout.addRow("Child Action Point:", self.child_point)
        preview.advance(0)
        for editor, state, combo, selected in (
            (preview.root, preview.members[0], self.parent_point, member.parent_point),
            (member.editor, member, self.child_point, member.child_point),
        ):
            editor.refreshRigProfile()
            names = available_point_names(editor.action_points, editor.action_point_names, editor._rig_profile_loaded)
            body = editor.action_points.get(state.frame.frame_index) if state.frame else None
            for name in names:
                combo.addItem(name + (" (unset)" if body is None or body.get(name) is None else ""), name)
            default = editor.ui.actionPointComboBox.currentData()
            if combo is self.parent_point and body is not None and body.get("center") is not None:
                default = "center"
            combo.setCurrentIndex(combo.findData(selected or default))
        self.follow_facing = QtWidgets.QCheckBox("Follow parent's logical facing")
        self.follow_facing.setChecked(member.follow_facing)
        layout.addRow(self.follow_facing)
        self.follow_action = QtWidgets.QCheckBox("Select the parent's action name (missing action hides sprite)")
        self.follow_action.setChecked(member.follow_action)
        layout.addRow(self.follow_action)
        self.synchronized = QtWidgets.QCheckBox("Synchronize timeline phase with parent")
        self.synchronized.setChecked(member.synchronized)
        layout.addRow(self.synchronized)
        self.inherit_mirroring = QtWidgets.QCheckBox("Mirror artwork with parent (separate from facing)")
        self.inherit_mirroring.setChecked(member.inherit_mirroring)
        layout.addRow(self.inherit_mirroring)
        note = QtWidgets.QLabel("Temporary preview binding. Coordinates are edited and saved on each sprite using its Action Points panel.")
        note.setWordWrap(True)
        layout.addRow(note)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.ok = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        for combo in (self.parent_point, self.child_point):
            combo.currentIndexChanged.connect(self.validate)
        self.validate()

    def validate(self):
        self.ok.setEnabled(self.parent_point.currentData() is not None and self.child_point.currentData() is not None)

    def accept(self):
        self.preview.bind(self.member, self.parent_point.currentData(), self.child_point.currentData(),
                          follow_facing=self.follow_facing.isChecked(), follow_action=self.follow_action.isChecked(),
                          synchronized=self.synchronized.isChecked(), inherit_mirroring=self.inherit_mirroring.isChecked())
        super().accept()


def add_sprite_preview(root, path=None):
    if path is None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(root.window, "Add Sprite Preview", "", "Sprite files (*.json *.xml)")
    if not path:
        return
    host = root.document_host
    preview = root.point_preview
    editor = host.open_file(path)
    if editor is None:
        return
    try:
        if preview is None:
            if editor is root:
                msg = "Choose another sprite resource for the preview."
                raise ValueError(msg)
            if editor.point_preview is not None:
                msg = "This sprite already belongs to a point preview."
                raise ValueError(msg)
            preview = ActionPointPreview(root)
        member = preview.add_sprite(editor)
    except ValueError as error:
        root.create_error_popup(str(error))
        return
    for member_editor in (root, editor):
        member_editor.workspace.set_visible(True)
    dialog = RelationshipDialog(preview, member)
    try:
        dialog.exec()
    finally:
        dialog.deleteLater()


def load_preview(host, path, *, add_to_recent=True):
    """Validate every referenced source before replacing inspection state."""
    from conversion import SpriteDocument
    from preview import read_workspace
    from utils import FormatError
    path = Path(path).resolve()
    data = read_workspace(path)
    paths = [(path.parent / data["root"]).resolve(),
             *((path.parent / part["sprite"]).resolve() for part in data["parts"])]
    if len(paths) != len(set(paths)):
        msg = "This preview supports one instance per open resource"
        raise FormatError(msg)
    documents = [SpriteDocument.load(source) for source in paths]
    for document, part in zip(documents[1:], data["parts"]):
        if part.get("action") and not any(g.name==part["action"] for g in document.groups):
            msg = f"Preview action {part['action']!r} is missing; current preview is unchanged"
            raise FormatError(msg)
    current = host.current_editor.point_preview if host.current_editor else None
    for editor in host._editors.values():
        if editor.file_name and Path(editor.file_name).resolve() in paths and editor.point_preview not in (None, current):
            msg = "A resource already belongs to another preview; end it before loading this preview"
            raise FormatError(msg)
    if current:
        current.remove(current.root)
    editors = [host.open_file(source, add_to_recent=False) for source in paths]
    if any(e is None for e in editors):
        msg = "Could not open all preview resources"
        raise FormatError(msg)
    preview = ActionPointPreview(editors[0])
    preview.members[0].visible = data.get("root_visible",True)
    preview.members[0].display_name = data.get("root_name","")
    try:
        for editor, part in zip(editors[1:], data["parts"]):
            member = preview.add_sprite(editor, parent_point=part["parent_point"], child_point=part["child_point"])
            member.display_name = part.get("name", "")
            for flag in ("follow_facing", "follow_action", "synchronized", "inherit_mirroring", "visible"):
                if flag in part:
                    setattr(member, flag, part[flag])
            action = part.get("action")
            if action:
                item = next((i for i in editor._get_action_list_items() if i.anim_group.name == action), None)
                if item is None:
                    msg = f"{editor.file_name}: preview action {action!r} is missing"
                    raise FormatError(msg)
                editor.openGLWidget.makeCurrent()
                editor.ui.actionListWidget.setCurrentItem(item)
                item.mouseClickEvent(None)
            editor.ui.previewShadowCheckBox.setChecked(False)
        preview.order = [preview.members[i].identity for i in data.get("order", range(len(editors)))]
        for editor in editors:
            editor.workspace.set_visible(True)
        preview.select(preview.members[0].identity)
        preview.refresh()
        if add_to_recent:
            preview.root.add_recent_list(path)
        return preview
    except Exception:
        preview.remove(preview.root)
        raise


def load_preview_dialog(editor):
    path, _ = QtWidgets.QFileDialog.getOpenFileName(editor.window, "Load Editor Preview", str(Path(editor.file_name).with_suffix(".preview.json")) if editor.file_name else "", "Editor Preview (*.preview.json)")
    if path:
        try:
            load_preview(editor.document_host, path)
        except (ValueError, OSError) as error:
            editor.create_error_popup(str(error))
