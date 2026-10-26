"""Validation for the exported FrameData.xml and AnimData.xml formats."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

from data import FRAME_DATA, SheetFormat
from utils import WHITE, FormatError, get_image_size, integer, known_children

if TYPE_CHECKING:
    from conversion import SpriteDocument
    from data import AnimationSequence, AnimGroup
    from main import AnimationEditor

_CHANNEL_MAX = 255
_DIRECTION_COUNT = len(FRAME_DATA)


def detect_format(root: ET.Element) -> SheetFormat:
    """Identify the sheet format from the root element's frame dimensions."""
    return (
        SheetFormat.SINGLE if root.find("FrameWidth") is not None else SheetFormat.MULTI
    )


_FRAME_TAGS = {
    "Name",
    "Index",
    "CopyOf",
    "FrameWidth",
    "FrameHeight",
    "ShadowSize",
    "RushFrame",
    "HitFrame",
    "ReturnFrame",
    "FrameIndex",
    "HFlip",
    "Duration",
    "XOffset",
    "YOffset",
}


def validate_source(
    path: str | Path,
    expected: SheetFormat | None = None,
) -> ET.ElementTree:
    """Validate a sprite bundle's XML, images, markers, and references."""
    path = Path(path)
    # ElementTree does not resolve external entities in sprite XML.
    tree = ET.parse(path)  # noqa: S314
    root = tree.getroot()
    _validate_source_structure(root)
    format_name = detect_format(root)
    if expected is not None and format_name != expected:
        msg = f"This is {format_name.value} data; use its matching loader."
        raise FormatError(msg)
    single = format_name == SheetFormat.SINGLE
    # Runtime-only idle behavior is intentionally ignored by the editor.
    known_children(
        root,
        {"CutsceneIdle", "ShadowSize", "Anims", "FrameWidth", "FrameHeight"}
        if single
        else {"CutsceneIdle", "ShadowSize", "Anims"},
    )
    if integer(root, "ShadowSize", 1) not in (0, 1, 2):
        msg = "ShadowSize must be 0 (small), 1 (normal) or 2 (large)."
        raise FormatError(msg)
    anims = root.find("Anims")
    if anims is None or not len(anims):
        msg = "No animations found."
        raise FormatError(msg)
    known_children(anims, {"Anim"}, repeated={"Anim"})
    names, indices, by_name = set(), set(), {}
    frame_count = _validate_single_atlas(root, path) if single else 0
    for anim in anims:
        name = _validate_source_identity(anim, names, indices, single=single)
        by_name[name] = anim
        allowed = {"Name", "Index", "CopyOf", "RushFrame", "HitFrame", "ReturnFrame"}
        allowed |= (
            {"Sequences"} if single else {"FrameWidth", "FrameHeight", "Durations"}
        )
        known_children(anim, allowed)
        if anim.find("CopyOf") is not None:
            known_children(anim, {"Name", "Index", "CopyOf"})
            continue
        if single:
            lengths = _validate_single_sequences(anim, frame_count)
        else:
            dimensions = _validate_multi_images(anim, path)
            lengths = [_validate_durations(anim, dimensions[2])]
            _validate_multi_markers(path, name, dimensions)
        _validate_source_events(anim, lengths)
    _validate_copies(by_name, single=single)
    return tree


def _validate_source_structure(root: ET.Element) -> None:
    for node in root.iter():
        if node.attrib:
            msg = f"Unsupported attributes on {node.tag}; cannot preserve them."
            raise FormatError(msg)
        if node.tag in _FRAME_TAGS and len(node):
            msg = f"Unsupported nested data in {node.tag}; cannot preserve it."
            raise FormatError(msg)


def _validate_single_atlas(root: ET.Element, path: Path) -> int:
    w, h = integer(root, "FrameWidth"), integer(root, "FrameHeight")
    size = get_image_size(path.parent / "Anim.png")
    if min(w, h) <= 0 or size[0] % w or size[1] % h:
        msg = "Single-sheet dimensions must divide the sprite atlas."
        raise FormatError(msg)
    if (path.parent / "Offsets.png").exists() and get_image_size(
        path.parent / "Offsets.png",
    ) != size:
        msg = "Offsets.png dimensions must match Anim.png."
        raise FormatError(msg)
    return size[0] // w * (size[1] // h)


def _validate_source_identity(
    anim: ET.Element,
    names: set[str],
    indices: set[int],
    *,
    single: bool,
) -> str:
    name = anim.findtext("Name", "")
    # Names become filenames in the multi format; reject collisions and paths.
    key = name if single else name.casefold()
    if not name.strip() or key in names:
        msg = f"Missing or duplicate animation name: {name!r}."
        raise FormatError(msg)
    if not single and (
        any(char in name for char in '/\\<>:"|?*') or name.endswith((".", " "))
    ):
        msg = f"Animation name cannot be used as a filename: {name!r}."
        raise FormatError(msg)
    names.add(key)
    index = integer(anim, "Index", -1)
    if index != -1:
        if index < 0 or index in indices:
            msg = f"Duplicate or invalid Index {index} for {name}."
            raise FormatError(msg)
        indices.add(index)
    return name


def _validate_single_sequences(anim: ET.Element, frame_count: int) -> list[int]:
    name = anim.findtext("Name", "")
    sequences = anim.find("Sequences")
    if sequences is None or len(sequences) not in (1, 8):
        msg = f"{name}: single sheets need one or eight sequences."
        raise FormatError(msg)
    known_children(sequences, {"AnimSequence"}, repeated={"AnimSequence"})
    for sequence in sequences:
        known_children(sequence, {"AnimFrame"}, repeated={"AnimFrame"})
        for frame in sequence:
            _validate_single_frame(frame, name, frame_count)
    return [len(sequence) for sequence in sequences if len(sequence)]


def _validate_single_frame(
    frame: ET.Element,
    name: str,
    frame_count: int,
) -> None:
    known_children(
        frame,
        {"FrameIndex", "HFlip", "Duration", "Sprite", "Shadow"},
    )
    if not 0 <= integer(frame, "FrameIndex") < frame_count:
        msg = f"{name}: frame index is outside the atlas."
        raise FormatError(msg)
    if integer(frame, "Duration") <= 0 or integer(
        frame,
        "HFlip",
    ) not in (0, 1):
        msg = f"{name}: invalid duration or flip."
        raise FormatError(msg)
    for tag in ("Sprite", "Shadow"):
        offset = frame.find(tag)
        if offset is None:
            msg = f"{name}: missing {tag} offset."
            raise FormatError(msg)
        known_children(offset, {"XOffset", "YOffset"})
        integer(offset, "XOffset")
        integer(offset, "YOffset")


def _validate_multi_images(anim: ET.Element, path: Path) -> tuple[int, int, int, int]:
    name = anim.findtext("Name", "")
    w, h = integer(anim, "FrameWidth"), integer(anim, "FrameHeight")
    if min(w, h) <= 0 or w % 2 or h % 2:
        msg = f"{name}: multi-sheet frame dimensions must be positive and even."
        raise FormatError(msg)
    sizes = [
        get_image_size(path.parent / f"{name}-{suffix}.png")
        for suffix in ("Anim", "Offsets", "Shadow")
    ]
    if (
        len(set(sizes)) != 1
        or sizes[0][0] % w
        or sizes[0][1] % h
        or sizes[0][1] // h not in (1, 8)
    ):
        msg = f"{name}: invalid or mismatched PNG dimensions/directions."
        raise FormatError(msg)
    return w, h, sizes[0][0] // w, sizes[0][1] // h


def _validate_durations(anim: ET.Element, columns: int) -> int:
    name = anim.findtext("Name", "")
    durations = anim.find("Durations")
    if durations is None:
        msg = f"{name}: missing Durations."
        raise FormatError(msg)
    known_children(durations, {"Duration"}, repeated={"Duration"})
    if len(durations) != columns:
        msg = f"{name}: duration count does not match the image columns."
        raise FormatError(msg)
    for value in durations:
        try:
            valid = int(value.text) > 0
        except (TypeError, ValueError):
            valid = False
        if not valid:
            msg = f"{name}: durations must be positive integers."
            raise FormatError(msg)
    return len(durations)


def _validate_multi_markers(
    path: Path,
    name: str,
    dimensions: tuple[int, int, int, int],
) -> None:
    w, h, columns, rows = dimensions
    # Decode checks happen before the editor clears its current document.
    # Histograms count markers in Pillow without a Python loop per pixel.
    with Image.open(path.parent / f"{name}-Offsets.png") as source:
        offsets = source.convert("RGBA")
    with Image.open(path.parent / f"{name}-Shadow.png") as source:
        shadows = source.convert("RGBA")
    with Image.open(path.parent / f"{name}-Anim.png") as source:
        sprites = source.convert("RGBA")
    for row in range(rows):
        for column in range(columns):
            box = (column * w, row * h, (column + 1) * w, (row + 1) * h)
            # Entirely empty triplets are transparent timed frames, not broken markers.
            if all(image.crop(box).getbbox() is None for image in (sprites, offsets, shadows)):
                continue
            colors = offsets.crop(box).getcolors(w * h)
            counts = [
                sum(
                    count
                    for count, color in colors
                    if color[3] and color[channel] == _CHANNEL_MAX
                )
                for channel in range(3)
            ]
            heads = sum(count for count, color in colors if color == (0, 0, 0, 255))
            if counts != [1, 1, 1] or heads > 1:
                msg = (
                    f"{name}[{row},{column}]: missing or ambiguous attachment markers."
                )
                raise FormatError(msg)
            whites = sum(
                count
                for count, color in shadows.crop(box).getcolors(w * h)
                if color == WHITE
            )
            if whites != 1:
                msg = f"{name}[{row},{column}]: expected one white shadow anchor."
                raise FormatError(msg)


def _validate_source_events(anim: ET.Element, lengths: list[int]) -> None:
    name = anim.findtext("Name", "")
    for tag in ("RushFrame", "HitFrame", "ReturnFrame"):
        value = integer(anim, tag, -1)
        if value < -1 or (value >= 0 and any(value >= count for count in lengths)):
            msg = f"{name}: {tag} is outside the animation."
            raise FormatError(msg)


def _validate_copies(by_name: dict[str, ET.Element], *, single: bool) -> None:
    for name, anim in by_name.items():
        seen = {name}
        source = anim.findtext("CopyOf")
        while source is not None:
            if source not in by_name or source in seen:
                msg = f"{name}: missing or cyclic CopyOf target."
                raise FormatError(msg)
            seen.add(source)
            next_source = by_name[source].findtext("CopyOf")
            if not single and next_source is not None:
                msg = f"{name}: AnimData.xml does not allow chained copies."
                raise FormatError(msg)
            source = next_source


def validate_export(
    editor: SpriteDocument | AnimationEditor,
    format_name: SheetFormat,
) -> None:
    """Reject exports that would discard artwork, timing, or attachment data."""
    if any(not layer.reference_only for layer in getattr(editor, "layers", [])):
        raise FormatError("Migrate historical attachments before exporting individual sprite resources to XML.")
    if format_name not in (SheetFormat.SINGLE, SheetFormat.MULTI):
        msg = "Use native format validation for JSON documents."
        raise FormatError(msg)
    single = format_name == SheetFormat.SINGLE
    if not editor.groups or editor.image_grid is None:
        msg = "No sprite data to save."
        raise FormatError(msg)
    if editor.shadow_size not in (0, 1, 2):
        msg = "ShadowSize must be 0 (small), 1 (normal) or 2 (large)."
        raise FormatError(msg)
    names, indices = set(), set()
    for group in editor.groups:
        _validate_export_identity(group, names, indices, single=single)
        _validate_export_group(editor, group, single=single)
    _validate_export_action_points(editor, single=single)
    if not single:
        _validate_unreferenced_frames(editor)


def _validate_export_identity(
    group: AnimGroup,
    names: set[str],
    indices: set[int],
    *,
    single: bool,
) -> None:
    name = group.name
    key = name if single else name.casefold()
    if (
        not name.strip()
        or key in names
        or (
            not single
            and (
                any(char in name for char in '/\\<>:"|?*') or name.endswith((".", " "))
            )
        )
    ):
        msg = f"Missing, duplicate or invalid animation name: {name!r}."
        raise FormatError(msg)
    names.add(key)
    if group.idx != -1:
        if group.idx < 0 or group.idx in indices:
            msg = f"Duplicate or invalid Index {group.idx}."
            raise FormatError(msg)
        indices.add(group.idx)


def _validate_export_group(
    editor: SpriteDocument | AnimationEditor,
    group: AnimGroup,
    *,
    single: bool,
) -> None:
    name = group.name
    if len(group.directions) != _DIRECTION_COUNT:
        msg = f"{name}: expected eight editing sequences."
        raise FormatError(msg)
    if not single:
        timings = [
            tuple(frame.duration for frame in sequence.frames)
            for sequence in group.directions
        ]
        if not timings[0] or any(timing != timings[0] for timing in timings[1:]):
            msg = (
                f"{name}: Duration values and frame counts must match "
                "across all directions for AnimData.xml. "
                "Save as FrameData.xml to preserve them."
            )
            raise FormatError(msg)
    for sequence in group.directions:
        _validate_export_sequence(editor, group, sequence, single=single)


def _validate_export_sequence(
    editor: SpriteDocument | AnimationEditor,
    group: AnimGroup,
    sequence: AnimationSequence,
    *,
    single: bool,
) -> None:
    name = group.name
    for value in (group.rush_frame, group.hit_frame, group.return_frame):
        if value < -1 or (
            sequence.frames and value >= 0 and value >= len(sequence.frames)
        ):
            msg = f"{name}: event frame is outside a sequence."
            raise FormatError(msg)
    for frame in sequence.frames:
        if (
            frame.duration <= 0
            or not 0 <= frame.frame_index < len(editor.image_grid)
            or frame.flip not in (0, 1)
        ):
            msg = f"{name}: invalid duration, frame index or flip."
            raise FormatError(msg)
        if (
            not single
            and frame.frame_index not in editor.action_points
            and _frame_has_artwork(editor, frame.frame_index)
        ):
            msg = (
                f"{name}: attachment points are unavailable; "
                "cannot create AnimData.xml losslessly."
            )
            raise FormatError(msg)


def _frame_has_artwork(editor, index: int) -> bool:
    region = editor.image_grid[index]
    image = (
        region if isinstance(region, Image.Image) else Image.frombytes(
            "RGBA", (region.width, region.height),
            region.get_image_data().get_data("RGBA", -region.width * 4),
        )
    )
    return image.getbbox() is not None


def _validate_export_action_points(
    editor: SpriteDocument | AnimationEditor,
    *,
    single: bool,
) -> None:
    if getattr(editor, "rig_profile", None) or getattr(editor, "rig_profile_id", None) is not None or getattr(editor, "action_point_names", ()):
        raise FormatError("XML cannot preserve Rig Profile references or named Action Point declarations. Save native JSON instead.")
    for index, body in editor.action_points.items():
        if set(dict(body.items())) != {"left_hand", "center", "right_hand", "head"}:
            raise FormatError("XML cannot represent custom or unset Action Points. Save native JSON to preserve them.")
        if not 0 <= index < len(editor.image_grid):
            msg = "Attachment point frame is outside the atlas."
            raise FormatError(msg)
        points = body.all_pos()
        if body.head != body.center and any(
            body.head == point for point in (body.left_hand, body.right_hand)
        ):
            msg = (
                "Head attachment overlaps a hand and cannot be encoded "
                "losslessly in RGB markers."
            )
            raise FormatError(msg)
        if single and any(
            not (
                -(editor.frame_width // 2)
                <= point.x
                < editor.frame_width - editor.frame_width // 2
                and -(editor.frame_height // 2)
                <= point.y
                < editor.frame_height - editor.frame_height // 2
            )
            for point in points
        ):
            msg = (
                "Attachment point is outside the single-sheet cell; "
                "cannot save without clipping it."
            )
            raise FormatError(msg)


def _validate_unreferenced_frames(editor: SpriteDocument | AnimationEditor) -> None:
    referenced = {
        frame.frame_index
        for group in editor.groups
        for sequence in group.directions
        for frame in sequence.frames
    }
    for index in range(len(editor.image_grid)):
        if index in referenced:
            continue
        region = editor.image_grid[index]
        image = (
            region
            if isinstance(region, Image.Image)
            else Image.frombytes(
                "RGBA",
                (region.width, region.height),
                region.get_image_data().get_data("RGBA", -region.width * 4),
            )
        )
        if image.getbbox() is not None or index in editor.action_points:
            msg = (
                "Unreferenced atlas artwork/attachment data cannot be preserved "
                "in AnimData.xml; save FrameData.xml instead."
            )
            raise FormatError(msg)
