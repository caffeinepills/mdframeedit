"""Sprite file formats and image conversion using plain models and Pillow only."""

from __future__ import annotations

import copy
import math
import os
import pathlib
import shutil
import tempfile
import xml.etree.ElementTree as ElementTree
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Tuple

from PIL import Image, ImageDraw

from data import (
    ActionPoints,
    AnimationSequence,
    AnimFrame,
    AnimGroup,
    Offset,
    SheetFormat,
    TLRectangle,
    center_bounds,
)
from formats import detect_format, validate_export, validate_source
from utils import (
    FormatError,
    check_duplicate_images,
    get_action_points_from_pil_image,
    get_shadow_location_from_pil_image,
    integer,
    overlap_colors,
    round_up_to_mult,
    template_image,
    write_xml,
)


class ImageGrid:
    """A top-left Pillow atlas grid with no graphics context or UI ownership."""

    def __init__(self, image, width, height):
        self.image = image
        self.item_width, self.item_height = width, height
        self.columns, self.rows = image.width // width, image.height // height

    def __len__(self):
        return self.columns * self.rows

    def __getitem__(self, index):
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        x = index % self.columns * self.item_width
        y = index // self.columns * self.item_height
        return self.image.crop((x, y, x + self.item_width, y + self.item_height))


class AnimationSerialization:
    """XML/model helpers shared by the editor and the standalone document."""

    def _append_shadow_size(self, root):
        if self.shadow_size_present or self.shadow_size != self.loaded_shadow_size:
            ElementTree.SubElement(root, "ShadowSize").text = str(self.shadow_size)

    def is_sequence_collapsable(self, anim_group: AnimGroup):
        return all(
            sequence.frames == anim_group.directions[0].frames
            for sequence in anim_group.directions
        )

    def _can_trim_copy(self, group: AnimGroup, copy_name=None):
        copy_name = group.copy_name if copy_name is None else copy_name
        source = next(
            (candidate for candidate in self.groups if candidate.name == copy_name),
            None,
        )
        if source is None:
            return False
        return (
            group.rush_frame,
            group.hit_frame,
            group.return_frame,
            group.directions,
        ) == (
            source.rush_frame,
            source.hit_frame,
            source.return_frame,
            source.directions,
        )

    def _copy_target(self, group, copy_name):
        """Serialize a direct copy of a matching, fully written source."""
        target = next((item for item in self.groups if item.name == copy_name), None)
        seen = {group.name}
        while target is not None:
            if target.name in seen or not self._can_trim_copy(group, target.name):
                return ""
            seen.add(target.name)
            if not target.copy_name or not self._can_trim_copy(target):
                return target.name
            target = next(
                (item for item in self.groups if item.name == target.copy_name), None
            )
        return ""

    def create_base_anim_group_xml(
        self,
        anim_el: ElementTree.Element,
        name: str,
        index: int,
        group: AnimGroup,
        trim=False,
        copy_name="",
        size=None,
        allow_copy_chains=False,
    ) -> bool:
        ElementTree.SubElement(anim_el, "Name").text = name

        if index != -1:
            ElementTree.SubElement(anim_el, "Index").text = str(index)

        if trim:
            target = (
                copy_name
                if allow_copy_chains and self._can_trim_copy(group, copy_name)
                else self._copy_target(group, copy_name)
            )
            if target:
                ElementTree.SubElement(anim_el, "CopyOf").text = target
                return False

        if size:
            ElementTree.SubElement(anim_el, "FrameWidth").text = str(size[0])
            ElementTree.SubElement(anim_el, "FrameHeight").text = str(size[1])

        if group.rush_frame != -1:
            ElementTree.SubElement(anim_el, "RushFrame").text = str(group.rush_frame)

        if group.hit_frame != -1:
            ElementTree.SubElement(anim_el, "HitFrame").text = str(group.hit_frame)

        if group.return_frame != -1:
            ElementTree.SubElement(anim_el, "ReturnFrame").text = str(
                group.return_frame
            )

        return True

    def create_single_sheet_frame_data(
        self, anim_el: ElementTree.Element, group: AnimGroup, collapse
    ):
        sequences_ele = ElementTree.SubElement(anim_el, "Sequences")

        is_collapsable = False
        if collapse:
            is_collapsable = self.is_sequence_collapsable(group)

        for sequence in group.directions:
            seq_ele = ElementTree.SubElement(sequences_ele, "AnimSequence")

            for frame in sequence.frames:
                frame_ele = ElementTree.SubElement(seq_ele, "AnimFrame")

                ElementTree.SubElement(frame_ele, "FrameIndex").text = str(
                    frame.frame_index
                )
                ElementTree.SubElement(frame_ele, "Duration").text = str(frame.duration)
                ElementTree.SubElement(frame_ele, "HFlip").text = str(int(frame.flip))
                sprite_off = ElementTree.SubElement(frame_ele, "Sprite")
                ElementTree.SubElement(sprite_off, "XOffset").text = str(
                    frame.sprite_offset.x
                )
                ElementTree.SubElement(sprite_off, "YOffset").text = str(
                    frame.sprite_offset.y
                )
                shadow_off = ElementTree.SubElement(frame_ele, "Shadow")
                ElementTree.SubElement(shadow_off, "XOffset").text = str(
                    frame.shadow_offset.x
                )
                ElementTree.SubElement(shadow_off, "YOffset").text = str(
                    frame.shadow_offset.y
                )

            # Stop after writing a frame if we are collapsing it.
            if is_collapsable:
                break

        return True

    def _resolve_copies(self):
        by_name = {group.name: group for group in self.groups}
        resolved = set()

        def resolve(group):
            if group.name in resolved:
                return
            if group.copy_name:
                source = by_name[group.copy_name]
                resolve(source)
                group.rush_frame, group.hit_frame, group.return_frame = (
                    source.rush_frame,
                    source.hit_frame,
                    source.return_frame,
                )
                group.directions = copy.deepcopy(source.directions)
            resolved.add(group.name)

        for group in self.groups:
            resolve(group)

    def create_multi_sheet_frame_data(self, anim_el, group):
        durations = ElementTree.SubElement(anim_el, "Durations")
        for frame in group.directions[0].frames:
            ElementTree.SubElement(durations, "Duration").text = str(frame.duration)
        return True


@dataclass
class SpriteDocument(AnimationSerialization):
    sheet_image: Image.Image
    frame_width: int
    frame_height: int
    groups: list[AnimGroup] = field(default_factory=list)
    action_points: dict[int, ActionPoints] = field(default_factory=dict)
    action_pt_image: Image.Image | None = None
    shadow_image: Image.Image = field(default_factory=template_image)
    shadow_size: int = 1
    shadow_size_present: bool = True
    loaded_shadow_size: int = 1
    loaded_action_points: dict[int, ActionPoints] = field(default_factory=dict)
    trim_copies: bool = True
    collapse_singles: bool = True
    source_tree: ElementTree.ElementTree | None = field(
        default=None, init=False, repr=False
    )
    sheet_format: SheetFormat | None = field(default=None, init=False)

    def __post_init__(self):
        self.image_grid = ImageGrid(
            self.sheet_image, self.frame_width, self.frame_height
        )

    @classmethod
    def load(cls, path, expected=None):
        path = pathlib.Path(path)
        tree = validate_source(path, expected)
        root = tree.getroot()
        if detect_format(root) == SheetFormat.SINGLE:
            document = cls._load_single(path.parent, root)
        else:
            document = cls._load_multi(path.parent, root)
        document._resolve_copies()
        document.loaded_action_points = copy.deepcopy(document.action_points)
        document.source_tree = tree
        document.sheet_format = detect_format(root)
        return document

    @staticmethod
    def _read_image(path):
        with Image.open(path) as image:
            return image.convert("RGBA")

    @staticmethod
    def _read_group(element):
        return AnimGroup(
            integer(element, "Index", -1),
            element.findtext("Name"),
            integer(element, "RushFrame", -1),
            integer(element, "HitFrame", -1),
            integer(element, "ReturnFrame", -1),
            copy_name=element.findtext("CopyOf", ""),
        )

    @classmethod
    def _load_single(cls, directory, root):
        document = cls(
            cls._read_image(directory / "Anim.png"),
            integer(root, "FrameWidth"),
            integer(root, "FrameHeight"),
            shadow_size=integer(root, "ShadowSize", 1),
            loaded_shadow_size=integer(root, "ShadowSize", 1),
            shadow_size_present=root.find("ShadowSize") is not None,
        )
        if (directory / "Offsets.png").exists():
            document.action_pt_image = cls._read_image(directory / "Offsets.png")
            grid = ImageGrid(
                document.action_pt_image, document.frame_width, document.frame_height
            )
            for index, image in enumerate(grid):
                left, center, right, head = get_action_points_from_pil_image(image)
                if left is not None and center is not None and right is not None:
                    body = ActionPoints(
                        left, center, right, head if head is not None else center
                    )
                    body.add(
                        Offset(
                            -(document.frame_width // 2), -(document.frame_height // 2)
                        )
                    )
                    document.action_points[index] = body
        for element in root.find("Anims"):
            group = cls._read_group(element)
            if not group.copy_name:
                sequences = []
                for sequence in element.find("Sequences"):
                    frames = []
                    for index, frame in enumerate(sequence):
                        frames.append(
                            AnimFrame(
                                index,
                                integer(frame, "FrameIndex"),
                                integer(frame, "HFlip"),
                                integer(frame, "Duration"),
                                Offset(
                                    integer(frame.find("Shadow"), "XOffset"),
                                    integer(frame.find("Shadow"), "YOffset"),
                                ),
                                Offset(
                                    integer(frame.find("Sprite"), "XOffset"),
                                    integer(frame.find("Sprite"), "YOffset"),
                                ),
                            )
                        )
                    sequences.append(AnimationSequence(frames))
                group.directions = sequences
                group.__post_init__()
            document.groups.append(group)
        return document

    @classmethod
    def _load_multi(cls, directory, root):
        groups, frames, animation_frames = [], [], []
        max_width = max_height = 0
        for element in root.find("Anims"):
            group = cls._read_group(element)
            groups.append(group)
            if group.copy_name:
                continue
            width, height = (
                integer(element, "FrameWidth"),
                integer(element, "FrameHeight"),
            )
            sprites, offsets, shadows = [
                cls._read_image(directory / f"{group.name}-{suffix}.png")
                for suffix in ("Anim", "Offsets", "Shadow")
            ]
            durations = [int(value.text) for value in element.find("Durations")]
            row_count = sprites.height // height
            for direction in range(row_count):
                row = -direction % row_count
                for column, duration in enumerate(durations):
                    box = (
                        column * width,
                        row * height,
                        (column + 1) * width,
                        (row + 1) * height,
                    )
                    image = sprites.crop(box)
                    bounds = image.getbbox() or (
                        width // 2,
                        height // 2,
                        width // 2 + 1,
                        height // 2 + 1,
                    )
                    cropped = TLRectangle.from_bounds(bounds)
                    left, center, right, head = get_action_points_from_pil_image(
                        offsets.crop(box)
                    )
                    body = ActionPoints(
                        left, center, right, head if head is not None else center
                    )
                    body.add(Offset(-cropped.center[0], -cropped.center[1]))
                    point_bounds = center_bounds(body.get_rect())
                    max_width = max(max_width, cropped.width, point_bounds.width)
                    max_height = max(max_height, cropped.height, point_bounds.height)
                    anchor = get_shadow_location_from_pil_image(shadows.crop(box))
                    frame = AnimFrame(
                        len(group.directions[direction].frames),
                        duration=duration,
                        sprite_offset=Offset(
                            cropped.x - (width // 2 - cropped.width // 2),
                            cropped.y - (height // 2 - cropped.height // 2),
                        ),
                        shadow_offset=Offset(
                            anchor.x - width // 2, anchor.y - height // 2
                        ),
                    )
                    group.directions[direction].frames.append(frame)
                    frames.append((image.crop(bounds), body))
                    animation_frames.append(frame)
            if row_count == 1:
                group.set_single_direction(group.directions[0])
        unique, mapping, points = check_duplicate_images(frames, True)
        width, height = round_up_to_mult(max_width, 2), round_up_to_mult(max_height, 2)
        columns = math.ceil(math.sqrt(len(unique)))
        atlas = Image.new("RGBA", (width * columns, height * columns))
        offsets = Image.new("RGBA", atlas.size)
        draw = ImageDraw.Draw(offsets)
        action_points = {}
        for index, image in enumerate(unique):
            x, y = index % columns * width, index // columns * height
            atlas.paste(
                image,
                (
                    x + width // 2 - image.width // 2,
                    y + height // 2 - image.height // 2,
                ),
            )
            positions = defaultdict(list)
            for point, color in zip(
                points[index].all_pos(),
                ((255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255), (0, 0, 0, 255)),
            ):
                positions[(x + width // 2 + point.x, y + height // 2 + point.y)].append(
                    color
                )
            for position, color in overlap_colors(positions).items():
                draw.point(position, color)
            action_points[index] = points[index]
        for index, frame in enumerate(animation_frames):
            frame.frame_index, frame.flip = (
                mapping[index].frame_index,
                mapping[index].flip,
            )
            if frame.flip and frames[index][0].width % 2:
                frame.sprite_offset.x += 1
            frame.reset()
        return cls(
            atlas,
            width,
            height,
            groups=groups,
            action_points=action_points,
            action_pt_image=offsets,
            shadow_size=integer(root, "ShadowSize", 1),
            loaded_shadow_size=integer(root, "ShadowSize", 1),
            shadow_size_present=root.find("ShadowSize") is not None,
        )

    def write_single_xml(self, file_name):
        validate_export(self, SheetFormat.SINGLE)
        collapse = self.collapse_singles

        root = ElementTree.Element("AnimData")

        ElementTree.SubElement(root, "FrameWidth").text = str(self.frame_width)
        ElementTree.SubElement(root, "FrameHeight").text = str(self.frame_height)
        self._append_shadow_size(root)

        anims_el = ElementTree.SubElement(root, "Anims")

        trim = self.trim_copies

        for group_anim in self.groups:
            anim_el = ElementTree.SubElement(anims_el, "Anim")
            if self.create_base_anim_group_xml(
                anim_el,
                group_anim.name,
                group_anim.idx,
                group_anim,
                trim=trim,
                copy_name=group_anim.copy_name,
                allow_copy_chains=True,
            ):
                self.create_single_sheet_frame_data(anim_el, group_anim, collapse)

        ElementTree.indent(root)

        tree = ElementTree.ElementTree(root)
        write_xml(tree, file_name)

    def write_multi_xml(self, file_name, frame_sizes: dict[str, Tuple[int, int]]):

        root = ElementTree.Element("AnimData")

        self._append_shadow_size(root)

        anims_el = ElementTree.SubElement(root, "Anims")

        trim = self.trim_copies

        for group_anim in self.groups:
            anim_el = ElementTree.SubElement(anims_el, "Anim")
            size = (
                frame_sizes[group_anim.name] if group_anim.name in frame_sizes else None
            )
            if self.create_base_anim_group_xml(
                anim_el,
                group_anim.name,
                group_anim.idx,
                group_anim,
                trim=trim,
                copy_name=group_anim.copy_name,
                size=size,
            ):
                self.create_multi_sheet_frame_data(anim_el, group_anim)

        ElementTree.indent(root)

        tree = ElementTree.ElementTree(root)
        write_xml(tree, file_name)

    def export(self, directory, format_name, xml_name=None):
        """Validate and render the complete bundle before touching its destination."""
        directory = pathlib.Path(directory)
        xml_name = xml_name or format_name.value
        if pathlib.Path(xml_name).suffix.lower() != ".xml":
            raise FormatError("Animation data must be saved as an XML file.")
        validate_export(self, format_name)
        # Staging also catches encoding/layout failures before an overwrite.
        directory.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="mdframeedit-export-", dir=directory.parent
        ) as temporary:
            stage = pathlib.Path(temporary)
            if format_name == SheetFormat.SINGLE:
                self._write_single_sheet(stage)
            else:
                self._write_multiple_sheets(stage)
            validate_source(stage / format_name.value, format_name)
            directory.mkdir(parents=True, exist_ok=True)
            files = sorted(
                stage.iterdir(), key=lambda path: path.suffix.lower() == ".xml"
            )
            replacements = [
                (
                    path,
                    directory
                    / (xml_name if path.name == format_name.value else path.name),
                )
                for path in files
            ]
            if (
                format_name == SheetFormat.SINGLE
                and not (stage / "Offsets.png").exists()
            ):
                replacements.append((None, directory / "Offsets.png"))
            backup = stage / "backup"
            backup.mkdir()
            for _, target in replacements:
                if target.exists():
                    shutil.copy2(target, backup / target.name)
            changed = []
            try:
                for source, target in replacements:
                    if source is None:
                        if target.exists():
                            target.unlink()
                            changed.append(target)
                    else:
                        os.replace(source, target)
                        changed.append(target)
            except OSError as write_error:
                try:
                    for target in reversed(changed):
                        saved = backup / target.name
                        if saved.exists():
                            os.replace(saved, target)
                        else:
                            target.unlink(missing_ok=True)
                except OSError:
                    rescue = pathlib.Path(
                        tempfile.mkdtemp(
                            prefix="mdframeedit-recovery-", dir=directory.parent
                        )
                    )
                    shutil.copytree(backup, rescue, dirs_exist_ok=True)
                    msg = f"Save failed. Original file backups are in {rescue}."
                    raise FormatError(msg) from write_error
                raise
        return directory / xml_name

    def _write_single_sheet(self, directory):
        self.write_single_xml(str(directory / "FrameData.xml"))
        with open(directory / "Anim.png", "wb") as output:
            self.sheet_image.save(output, format="PNG")
        changed_points = {
            index: body
            for index, body in self.action_points.items()
            if body != self.loaded_action_points.get(index)
        }
        if changed_points or (self.action_pt_image is None and self.action_points):
            if self.action_pt_image is None:
                image = Image.new(
                    "RGBA", (self.sheet_image.width, self.sheet_image.height)
                )
            else:
                image = self.action_pt_image.copy()
            draw = ImageDraw.Draw(image)
            for index, body in (changed_points or self.action_points).items():
                x = (
                    index % self.image_grid.columns
                ) * self.frame_width + self.frame_width // 2
                y = (
                    index // self.image_grid.columns
                ) * self.frame_height + self.frame_height // 2
                image.paste(
                    (0, 0, 0, 0),
                    (
                        x - self.frame_width // 2,
                        y - self.frame_height // 2,
                        x - self.frame_width // 2 + self.frame_width,
                        y - self.frame_height // 2 + self.frame_height,
                    ),
                )
                positions = defaultdict(list)
                for point, color in zip(
                    body.all_pos(),
                    (
                        (255, 0, 0, 255),
                        (0, 255, 0, 255),
                        (0, 0, 255, 255),
                        (0, 0, 0, 255),
                    ),
                ):
                    positions[(x + point.x, y + point.y)].append(color)
                for position, color in overlap_colors(positions).items():
                    draw.point(position, color)
            image.save(directory / "Offsets.png")
        elif self.action_pt_image is not None:
            with open(directory / "Offsets.png", "wb") as output:
                self.action_pt_image.save(output, format="PNG")

    def _write_multiple_sheets(self, directory):
        shadow = self.shadow_image
        anchor = get_shadow_location_from_pil_image(shadow) or Offset(
            shadow.width // 2, shadow.height // 2
        )
        tiles = {}
        group_sizes = {}
        for group in self.groups:
            if self.trim_copies and self._copy_target(group, group.copy_name):
                continue
            directions = [group.directions[0], *reversed(group.directions[1:])]
            if self.collapse_singles and self.is_sequence_collapsable(group):
                directions = directions[:1]
            prepared = []
            half_width = half_height = 1
            for sequence in directions:
                row = []
                for frame in sequence.frames:
                    if frame.frame_index not in tiles:
                        region = self.image_grid[frame.frame_index]
                        tiles[frame.frame_index] = region
                    tile = tiles[frame.frame_index]
                    image = (
                        tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                        if frame.flip
                        else tile
                    )
                    bounds = image.getbbox() or (
                        tile.width // 2,
                        tile.height // 2,
                        tile.width // 2 + 1,
                        tile.height // 2 + 1,
                    )
                    sprite = image.crop(bounds)
                    sx = bounds[0] - tile.width // 2 + frame.sprite_offset.x
                    sy = bounds[1] - tile.height // 2 + frame.sprite_offset.y
                    body = self.action_points[frame.frame_index]
                    pivot = tile.width - 1 - 2 * (tile.width // 2)
                    points = [
                        (
                            (-point.x + pivot if frame.flip else point.x)
                            + frame.sprite_offset.x,
                            point.y + frame.sprite_offset.y,
                        )
                        for point in body.all_pos()
                    ]
                    mx, my = (
                        frame.shadow_offset.x - anchor.x,
                        frame.shadow_offset.y - anchor.y,
                    )
                    half_width = max(
                        half_width,
                        abs(sx),
                        abs(sx + sprite.width),
                        abs(mx),
                        abs(mx + shadow.width),
                        *(max(abs(x), abs(x + 1)) for x, y in points),
                    )
                    half_height = max(
                        half_height,
                        abs(sy),
                        abs(sy + sprite.height),
                        abs(my),
                        abs(my + shadow.height),
                        *(max(abs(y), abs(y + 1)) for x, y in points),
                    )
                    row.append((sprite, sx, sy, points, shadow, mx, my))
                prepared.append(row)
            width, height = (
                round_up_to_mult(half_width * 2, 8),
                round_up_to_mult(half_height * 2, 8),
            )
            group_sizes[group.name] = (width, height)
            size = (width * len(prepared[0]), height * len(prepared))
            sprites = Image.new("RGBA", size)
            offsets = Image.new("RGBA", size)
            shadows = Image.new("RGBA", size)
            draw = ImageDraw.Draw(offsets)
            for row_index, row in enumerate(prepared):
                for column, (sprite, sx, sy, points, shadow, mx, my) in enumerate(row):
                    cx, cy = (
                        column * width + width // 2,
                        row_index * height + height // 2,
                    )
                    sprites.paste(sprite, (cx + sx, cy + sy))
                    shadows.paste(shadow, (cx + mx, cy + my))
                    positions = defaultdict(list)
                    for point, color in zip(
                        points,
                        (
                            (255, 0, 0, 255),
                            (0, 255, 0, 255),
                            (0, 0, 255, 255),
                            (0, 0, 0, 255),
                        ),
                    ):
                        positions[(cx + point[0], cy + point[1])].append(color)
                    for position, color in overlap_colors(positions).items():
                        draw.point(position, color)
            for suffix, image in (
                ("Anim", sprites),
                ("Offsets", offsets),
                ("Shadow", shadows),
            ):
                image.save(directory / f"{group.name}-{suffix}.png")
        self.write_multi_xml(str(directory / "AnimData.xml"), group_sizes)
