"""Native, versioned sprite authoring documents (JSON plus PNG)."""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, TypeVar, cast

from PIL import Image

from data import (
    ActionPoints,
    AnimationSequence,
    AnimFrame,
    AnimGroup,
    Offset,
    SheetFormat,
)
from utils import FormatError, save_png
from .packing import NativeRegion, TextureLayout, grid_layout, pack_texture, unpack_texture

if TYPE_CHECKING:
    from conversion import SpriteDocument

DocumentT = TypeVar("DocumentT", bound="SpriteDocument")
PointName = Literal["left_hand", "center", "right_hand", "head"]
EventName = Literal["rush_frame", "hit_frame", "return_frame"]


NativeActionPoints = dict[str, list[int]]


class _RequiredPooledAnimation(TypedDict):
    directions: list[int]


class NativePooledAnimation(_RequiredPooledAnimation, total=False):
    """Named animation metadata referencing shared sequence records."""

    index: int
    rush_frame: int
    hit_frame: int
    return_frame: int
    copy_of: str


# JSON decodes frame records as lists; the validator enforces seven fields
# with a boolean flip and integers everywhere else.
NativeFrameRecord = list[int | bool]


class _RequiredSpriteData(TypedDict):
    """Native grid or packed data usable immediately after json.loads."""

    format: str
    version: int
    texture: str
    frame_size: list[int]
    shadow_size: int | None
    action_points: list[NativeActionPoints | None]
    frames: list[NativeFrameRecord]
    sequences: list[list[int]]
    animations: dict[str, NativePooledAnimation]
    grid_size: list[int]
    regions: list[NativeRegion | None]






class NativeSpriteData(_RequiredSpriteData, total=False):
    action_point_names: list[str]
    rig_profile: str
    rig_profile_id: str


FORMAT = "mdframeedit"
VERSION = 3
NAMED_POINTS_VERSION = 4
FRAME_INDEX = 0
DURATION = 1
FLIP = 2
SPRITE_X = 3
SPRITE_Y = 4
SHADOW_X = 5
SHADOW_Y = 6
FRAME_RECORD_LENGTH = 7
POINT_NAMES: tuple[PointName, ...] = ("left_hand", "center", "right_hand", "head")
EVENT_NAMES: tuple[EventName, ...] = ("rush_frame", "hit_frame", "return_frame")
ROOT_FIELDS = ("format", "version", "texture", "frame_size", "shadow_size", "action_points",
               "animations", "frames", "sequences", "grid_size", "regions")


def _object(
    value: object,
    allowed: Collection[str],
    required: Collection[str],
    location: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        msg = f"{location}: missing or unknown fields."
        raise FormatError(msg)
    return cast("dict[str, object]", value)


def _integer(value: object, location: str, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise FormatError(f"{location}: expected an integer" + (f" >= {minimum}." if minimum is not None else "."))
    return value


def _pair(value: object, location: str, minimum: int | None = None) -> list[int]:
    if not isinstance(value, list) or len(value) != 2:
        msg = f"{location}: expected [x, y]."
        raise FormatError(msg)
    return [_integer(v, location, minimum) for v in value]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            msg = f"Duplicate JSON field: {key!r}."
            raise FormatError(msg)
        result[key] = value
    return result


def _header(value: object) -> dict[str, object]:
    allowed = (*ROOT_FIELDS, "rig_profile", "rig_profile_id", "action_point_names")
    data = _object(value, allowed, ("format", "version"), "Sprite")
    if data["format"] != FORMAT or type(data["version"]) is not int or data["version"] not in (VERSION, NAMED_POINTS_VERSION):
        msg = "Unsupported sprite JSON format or version."
        raise FormatError(msg)
    _object(data, allowed, ROOT_FIELDS, "Sprite")
    if data["version"] == VERSION and any(key in data for key in ("rig_profile", "rig_profile_id", "action_point_names")):
        raise FormatError("Rig Profiles and named Action Points require native version 4.")
    if "action_point_names" in data:
        from project import validate_point_names
        validate_point_names(data["action_point_names"], "action_point_names")
    if "rig_profile" in data:
        reference = data["rig_profile"]
        if not isinstance(reference, str) or not reference.strip() or "://" in reference or any(ord(c) < 32 for c in reference) or Path(reference).suffix.lower() not in (".yaml", ".yml"):
            raise FormatError("Rig Profile must be a YAML filesystem path.")
    if "rig_profile_id" in data:
        from project import validate_rig_id
        validate_rig_id(data["rig_profile_id"])
        if "rig_profile" not in data:
            raise FormatError("A named rig requires a rig_profile YAML path.")
    texture = data["texture"]
    if (not isinstance(texture, str) or not texture or texture in (".", "..")
            or any(c in texture for c in '/\\<>:"|?*') or Path(texture).suffix.lower() != ".png"):
        msg = "Texture must be a PNG filename in the same folder as the JSON."
        raise FormatError(msg)
    return data


def _validate_common(data: dict[str, object], image_size: tuple[int, int]) -> int:
    """Validate native texture geometry and attachment data."""
    width, height = _pair(data["frame_size"], "frame_size", 1)
    count = _validate_regions(data, image_size, (width, height))
    shadow = data["shadow_size"]
    if shadow is not None and (_integer(shadow, "shadow_size") not in (0, 1, 2)):
        msg = "Shadow Size must be null, 0, 1 or 2."
        raise FormatError(msg)
    points = data["action_points"]
    if not isinstance(points, list) or len(points) != count:
        msg = "Action Points must contain one entry per atlas cell (null for unavailable points)."
        raise FormatError(msg)
    for point_value in points:
        if point_value is not None:
            if data["version"] == VERSION:
                point = _object(point_value, POINT_NAMES, POINT_NAMES, "Action points")
            elif not isinstance(point_value, dict):
                raise FormatError("Action Points must be objects of named coordinates.")
            else:
                point = point_value
            from project import validate_point_name
            for point_name, coordinates in point.items():
                validate_point_name(point_name)
                _pair(coordinates, point_name)
    return count


def _validate_regions(data: dict[str, object], image_size: tuple[int, int], frame_size: tuple[int, int]) -> int:
    columns, rows = _pair(data["grid_size"], "grid_size", 1)
    regions = data["regions"]
    if not isinstance(regions, list) or len(regions) != columns * rows:
        raise FormatError("regions must contain one entry per logical cell in grid_size.")
    if min(image_size) <= 0:
        raise FormatError("Packed texture dimensions must be positive.")
    for region in regions:
        if region is None:
            continue
        if not isinstance(region, list) or len(region) != 6:
            raise FormatError("A region must contain six integer fields or be null.")
        x, y, width, height, tx, ty = [_integer(v, "Region", 0) for v in region]
        if width == 0 or height == 0:
            raise FormatError("Region width and height must be positive; use null for empty cells.")
        if x + width > image_size[0] or y + height > image_size[1]:
            raise FormatError("Region is outside the packed texture.")
        if tx + width > frame_size[0] or ty + height > frame_size[1]:
            raise FormatError("Region trim is outside the logical frame.")
    return len(regions)


def validate_data(data: object, image_size: tuple[int, int]) -> None:
    """Validate authoring input; engines can consume trusted JSON directly."""
    header = _header(data)
    count = _validate_common(header, image_size)
    _validate_tables(header, count)




def _validate_copy_targets(copies: dict[str, str], names: set[str]) -> None:
    for name in copies:
        seen, target = {name}, copies[name]
        while True:
            if target not in names or target in seen:
                msg = f"{name}: missing or cyclic copy_of target."
                raise FormatError(msg)
            seen.add(target)
            if target not in copies:
                break
            target = copies[target]


def _reference(value: object, count: int, location: str) -> int:
    index = _integer(value, location, 0)
    if index >= count:
        msg = f"{location}: reference is outside the table."
        raise FormatError(msg)
    return index


def _validate_tables(data: dict[str, object], cell_count: int) -> None:
    """Check each pooled record once and validate all animation references."""
    frames = data["frames"]
    sequences = data["sequences"]
    animations = data["animations"]
    if not isinstance(frames, list) or not isinstance(sequences, list):
        raise FormatError("frames and sequences must be tables.")
    for record in frames:
        if not isinstance(record, list) or len(record) != FRAME_RECORD_LENGTH:
            raise FormatError("A frame record must contain seven fields.")
        _reference(record[FRAME_INDEX], cell_count, "Atlas frame")
        _integer(record[DURATION], "Frame duration", 1)
        if type(record[FLIP]) is not bool:
            raise FormatError("Frame flip must be a boolean.")
        for offset in record[SPRITE_X:]:
            _integer(offset, "Frame offset")
    used_frames: set[int] = set()
    for sequence in sequences:
        if not isinstance(sequence, list):
            raise FormatError("Each sequence must be a list of frame-record references.")
        for reference in sequence:
            used_frames.add(_reference(reference, len(frames), "Sequence frame"))
    if not isinstance(animations, dict):
        raise FormatError("animations must be an object.")
    names: set[str] = set()
    indices: set[int] = set()
    copies: dict[str, str] = {}
    used_sequences: set[int] = set()
    for name, value in animations.items():
        if not isinstance(name, str) or not name.strip():
            raise FormatError("Missing animation name.")
        names.add(name)
        anim = _object(value, ("index", *EVENT_NAMES, "directions", "copy_of"), ("directions",), name)
        if "index" in anim:
            index = _integer(anim["index"], f"{name}: index", 0)
            if index in indices:
                raise FormatError(f"Duplicate animation index: {index}.")
            indices.add(index)
        directions = anim["directions"]
        if not isinstance(directions, list) or len(directions) not in (1, 8):
            raise FormatError(f"{name}: expected one shared direction or eight directions.")
        events = [_integer(anim.get(key, -1), f"{name}: {key}", -1) for key in EVENT_NAMES]
        for reference in directions:
            sequence_index = _reference(reference, len(sequences), f"{name}: sequence")
            used_sequences.add(sequence_index)
            sequence = sequences[sequence_index]
            if sequence and any(event >= len(sequence) for event in events):
                raise FormatError(f"{name}: event frame is outside a sequence.")
        if "copy_of" in anim:
            target = anim["copy_of"]
            if not isinstance(target, str):
                raise FormatError(f"{name}: invalid copy_of.")
            copies[name] = target
    _validate_copy_targets(copies, names)
    if len(used_sequences) != len(sequences) or len(used_frames) != len(frames):
        raise FormatError("Native tables contain unreferenced sequences or frame records.")


def _offset_data(offset: Offset) -> list[int]:
    return [offset.x, offset.y]


def document_data(document: SpriteDocument, texture: str) -> NativeSpriteData:
    """Build native authoring data with stable, full-cell texture regions."""
    layout = grid_layout(document.sheet_image, (document.frame_width, document.frame_height))
    return _document_data(document, texture, layout)


def _document_data(document: SpriteDocument, texture: str, layout: TextureLayout) -> NativeSpriteData:
    """Intern complete frame records and sequences without modifying the model."""
    if getattr(document, "layers", None) or getattr(document, "base_template", "") or getattr(document, "attachment_layout", None):
        raise FormatError("Native sprites cannot contain obsolete attachment composition data.")
    points: list[NativeActionPoints | None] = []
    for index in range(len(document.image_grid)):
        body = document.action_points.get(index)
        if body is None:
            points.append(None)
        else:
            point_data = {name: _offset_data(point) for name, point in body.items()}
            points.append(point_data)
    if any(type(index) is not int or not 0 <= index < len(points) for index in document.action_points):
        msg = "Action-point frame is outside the atlas."
        raise FormatError(msg)
    animations: dict[str, NativePooledAnimation] = {}
    frame_records: list[NativeFrameRecord] = []
    frame_indices: dict[tuple[int, int, bool, int, int, int, int], int] = {}
    sequence_records: list[list[int]] = []
    sequence_indices: dict[tuple[int, ...], int] = {}
    for group in document.groups:
        if group.name in animations:
            raise FormatError(f"Duplicate animation name: {group.name}.")
        anim: NativePooledAnimation = {"directions": []}
        if group.idx != -1:
            anim["index"] = group.idx
        for event_name in EVENT_NAMES:
            event: int = getattr(group, event_name)
            if event != -1:
                anim[event_name] = event
        # copy_of is a relationship hint. Explicit sequences always win, so
        # independent edits to a copy or its source can never be discarded.
        if group.copy_name:
            anim["copy_of"] = group.copy_name
        sequences = group.directions[:1] if group.is_single_direction else group.directions
        for sequence in sequences:
            references: list[int] = []
            for frame in sequence.frames:
                if type(frame.flip) not in (int, bool) or frame.flip not in (0, 1):
                    msg = f"{group.name}: invalid horizontal flip."
                    raise FormatError(msg)
                values = (frame.frame_index, frame.duration, frame.sprite_offset.x,
                          frame.sprite_offset.y, frame.shadow_offset.x, frame.shadow_offset.y)
                if any(type(value) is not int for value in values):
                    raise FormatError(f"{group.name}: frame fields must be integers.")
                record = (frame.frame_index, frame.duration, bool(frame.flip),
                          frame.sprite_offset.x, frame.sprite_offset.y,
                          frame.shadow_offset.x, frame.shadow_offset.y)
                record_index = frame_indices.get(record)
                if record_index is None:
                    record_index = len(frame_records)
                    frame_indices[record] = record_index
                    frame_records.append(list(record))
                references.append(record_index)
            sequence_key = tuple(references)
            sequence_index = sequence_indices.get(sequence_key)
            if sequence_index is None:
                sequence_index = len(sequence_records)
                sequence_indices[sequence_key] = sequence_index
                sequence_records.append(references)
            anim["directions"].append(sequence_index)
        animations[group.name] = anim
    data: NativeSpriteData = {"format": FORMAT, "version": VERSION, "texture": texture,
            "frame_size": [document.frame_width, document.frame_height],
            "grid_size": list(layout.grid_size), "regions": layout.regions,
            "shadow_size": document.shadow_size if document.shadow_size_present
            or document.shadow_size != document.loaded_shadow_size else None,
            "action_points": points, "frames": frame_records,
            "sequences": sequence_records, "animations": animations}
    if document.rig_profile or document.action_point_names or any(
        point is not None and set(point) != set(POINT_NAMES) for point in points
    ):
        data["version"] = NAMED_POINTS_VERSION
    if document.action_point_names:
        data["action_point_names"] = list(document.action_point_names)
    if document.rig_profile:
        data["rig_profile"] = document.rig_profile.resolve().as_posix()
    if document.rig_profile_id is not None:
        data["rig_profile_id"] = document.rig_profile_id
    validate_data(data, layout.texture_size)
    return data


def write_document(document: SpriteDocument, path: str | Path) -> None:
    """Write authoring JSON and an unchanged, externally editable grid PNG."""
    layout = grid_layout(document.sheet_image, (document.frame_width, document.frame_height))
    _write_document(document, Path(path), document.sheet_image, layout)


def write_packed_document(
    document: SpriteDocument, path: str | Path, *, padding: int = 1,
    power_of_two: bool = False,
) -> None:
    """Write cropped, padded texture regions for a derived project build."""
    packed, layout = pack_texture(document.sheet_image, (document.frame_width, document.frame_height),
                                  padding=padding, power_of_two=power_of_two)
    _write_document(document, Path(path), packed, layout, packed=True, padding=padding, power_of_two=power_of_two)


def _write_document(
    document: SpriteDocument, path: Path, image: Image.Image, layout: TextureLayout,
    *, packed: bool = False, padding: int = 1, power_of_two: bool = False,
) -> None:
    """Serialize either texture layout using the same direct-use native schema."""
    path = Path(path)
    texture = path.with_suffix(".png").name
    data = _document_data(document, texture, layout)
    if "rig_profile" in data:
        data["rig_profile"] = relative_sprite_path(document.rig_profile, path.parent)
    save_png(image, path.with_name(texture))
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def relative_sprite_path(source: Path, directory: Path) -> str:
    try:
        return Path(os.path.relpath(source.resolve(), directory.resolve())).as_posix()
    except ValueError:  # Different Windows drives cannot have a relative path.
        return source.resolve().as_posix()


def _texture_layout(data: NativeSpriteData, image_size: tuple[int, int]) -> TextureLayout:
    return TextureLayout((data["frame_size"][0], data["frame_size"][1]),
                         (data["grid_size"][0], data["grid_size"][1]), data["regions"], image_size)


def read_texture_layout(path: str | Path) -> TextureLayout:
    """Read the saved native layout for external artwork reload."""
    path = Path(path)
    header = _header(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object))
    with Image.open(path.with_name(cast("str", header["texture"]))) as image:
        validate_data(header, image.size)
        return _texture_layout(cast("NativeSpriteData", header), image.size)


def load_document(
    path: str | Path, document_class: type[DocumentT], *,
    _reference_directory: Path | None = None,
) -> DocumentT:
    """Validate native files and construct the requested document subclass."""
    path = Path(path)
    try:
        raw_data: object = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as error:
        msg = f"Invalid sprite JSON: {error}"
        raise FormatError(msg) from error
    # Validate the filename before reading any referenced file.
    header = _header(raw_data)
    texture = cast("str", header["texture"])
    with Image.open(path.with_name(texture)) as source:
        image = source.convert("RGBA")
    validate_data(raw_data, image.size)
    data = cast("NativeSpriteData", raw_data)
    image = unpack_texture(image, _texture_layout(data, image.size))
    document = document_class(image, data["frame_size"][0], data["frame_size"][1],
                              shadow_size=data["shadow_size"] if data["shadow_size"] is not None else 1,
                              loaded_shadow_size=data["shadow_size"] if data["shadow_size"] is not None else 1,
                              shadow_size_present=data["shadow_size"] is not None)
    for index, points in enumerate(data["action_points"]):
        if points is not None:
            document.action_points[index] = ActionPoints.from_named({name: Offset(*point) for name, point in points.items()})
    document.action_point_names = tuple(data.get("action_point_names", ()))
    if "rig_profile" in data:
        document.rig_profile = ((_reference_directory or path.parent) / data["rig_profile"]).resolve()
    document.rig_profile_id = data.get("rig_profile_id")
    _load_animations(document, data)
    document.loaded_action_points = copy.deepcopy(document.action_points)
    document.sheet_format = SheetFormat.NATIVE
    document.texture_name = texture
    return document


def _load_animations(document: SpriteDocument, data: NativeSpriteData) -> None:
    """Expand pooled storage into independent editor objects, never live aliases."""
    records = data["frames"]
    sequences = data["sequences"]
    for name, anim in data["animations"].items():
        directions: list[AnimationSequence] = []
        for sequence_index in anim["directions"]:
            frames: list[AnimFrame] = []
            for position, record_index in enumerate(sequences[sequence_index]):
                record = records[record_index]
                frames.append(AnimFrame(
                    position, int(record[FRAME_INDEX]), int(record[FLIP]), int(record[DURATION]),
                    Offset(int(record[SHADOW_X]), int(record[SHADOW_Y])),
                    Offset(int(record[SPRITE_X]), int(record[SPRITE_Y])),
                ))
            directions.append(AnimationSequence(frames))
        document.groups.append(AnimGroup(
            anim.get("index", -1), name, anim.get("rush_frame", -1),
            anim.get("hit_frame", -1), anim.get("return_frame", -1), directions,
            copy_name=anim.get("copy_of", ""),
        ))
