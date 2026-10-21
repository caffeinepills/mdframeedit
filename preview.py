"""Headless preview sampling, point alignment and workspace persistence."""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from data import FormatError

if TYPE_CHECKING:
    from conversion import SpriteDocument
    from data import AnimFrame

def sample_sequence(frames, ticks):
    """Sample the displayed entry at an explicit elapsed tick; empty is hidden."""
    if not isfinite(ticks):
        msg = "Animation time must be finite"
        raise ValueError(msg)
    if not frames:
        return None
    total = sum(frame.duration for frame in frames)
    if total <= 0 or any(frame.duration <= 0 for frame in frames):
        msg = "Animation durations must be positive"
        raise ValueError(msg)
    elapsed = ticks % total
    for frame in frames:
        if elapsed < frame.duration:
            return frame
        elapsed -= frame.duration
    return frames[-1]


def synchronized_time(parent_frames, child_frames, parent_ticks):
    """Share normalized cycle phase, without requiring matching frame counts.

    The consumer chooses clips and facing independently. No frame-index mapping,
    point coordinates, mirroring, mutable clocks or asset categories are involved.
    """
    if not parent_frames or not child_frames:
        return 0
    parent_total = sum(frame.duration for frame in parent_frames)
    child_total = sum(frame.duration for frame in child_frames)
    if min(parent_total, child_total) <= 0 or not isfinite(parent_ticks):
        msg = "Invalid animation phase"
        raise ValueError(msg)
    return (parent_ticks % parent_total) * child_total / parent_total


Vector = tuple[float, float]


def _vector(value, name):
    if not isinstance(value, (tuple, list)) or len(value) != 2 or any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) for v in value
    ):
        msg = f"{name} must contain two finite numbers."
        raise ValueError(msg)
    return tuple(value)


@dataclass(frozen=True)
class Presentation:
    """Place logical coordinates in a shared space; negative scales reflect axes.

    This is consumer presentation (e.g. viewport zoom/Y inversion), not authored
    sprite fitting. Position is the instance origin before its animation offset.
    """

    position: Vector = (0, 0)
    scale: Vector = (1, 1)

    def __post_init__(self):
        object.__setattr__(self, "position", _vector(self.position, "position"))
        object.__setattr__(self, "scale", _vector(self.scale, "scale"))
        if 0 in self.scale:
            msg = "Presentation scales must be nonzero."
            raise ValueError(msg)

    def vector(self, point: Vector) -> Vector:
        return point[0] * self.scale[0], point[1] * self.scale[1]

    def point(self, point: Vector) -> Vector:
        x, y = self.vector(point)
        return self.position[0] + x, self.position[1] + y


@dataclass(frozen=True)
class SpriteSample:
    """Snapshot of one instance's explicitly selected displayed artwork/entry.

    Point coordinates are relative to (width//2, height//2), positive Y down.
    Facing is metadata only; it never changes artwork selection or flip here.
    """

    instance_id: str
    frame_size: tuple[int, int]
    frame_index: int
    action_points: Mapping[str, Vector]
    sprite_offset: Vector = (0, 0)
    mirrored: bool = False
    facing: int = 0
    presentation: Presentation = field(default_factory=Presentation)

    def __post_init__(self):
        if not isinstance(self.instance_id, str) or not self.instance_id.strip():
            msg = "A sprite sample needs a nonempty instance id."
            raise ValueError(msg)
        size = tuple(self.frame_size)
        if len(size) != 2 or any(type(v) is not int or v < 1 for v in size):
            msg = "Logical frame dimensions must be positive integers."
            raise ValueError(msg)
        if type(self.frame_index) is not int or self.frame_index < 0:
            msg = "Sheet frame index must be a nonnegative integer."
            raise ValueError(msg)
        if type(self.facing) is not int or not 0 <= self.facing < 8:
            msg = "Logical facing must be from 0 to 7."
            raise ValueError(msg)
        if type(self.mirrored) is not bool:
            msg = "Artwork mirroring must be a boolean."
            raise ValueError(msg)
        if not isinstance(self.presentation, Presentation):
            msg = "Expected a Presentation transform."
            raise ValueError(msg)
        points = {}
        for name, point in self.action_points.items():
            if not isinstance(name, str) or not name.strip():
                msg = "Action Points need nonempty names."
                raise ValueError(msg)
            points[name] = _vector(point, f"Action Point {name!r}")
        object.__setattr__(self, "frame_size", size)
        object.__setattr__(self, "action_points", MappingProxyType(points))
        object.__setattr__(self, "sprite_offset", _vector(self.sprite_offset, "sprite_offset"))

    def artwork_point(self, point: Vector) -> Vector:
        """Mirror within the logical cell, without mirroring animation offsets."""
        x, y = point
        if self.mirrored:
            width = self.frame_size[0]
            x = width - 1 - 2 * (width // 2) - x
        return x, y

    def rendered_point(self, name: str) -> Vector | None:
        """Current point in presentation space; unset returns None, never zero."""
        point = self.action_points.get(name)
        if point is None:
            return None
        x, y = self.artwork_point(point)
        return self.presentation.point((x + self.sprite_offset[0], y + self.sprite_offset[1]))


def sample_sprite(instance_id: str, resource: SpriteDocument, frame: AnimFrame, *,
                  facing: int = 0, presentation: Presentation = Presentation()) -> SpriteSample:
    """Copy an already selected animation entry and its sheet-frame coordinates.

    The caller selects the action, direction and timeline entry independently
    for each sprite. No timeline lookup, advancement or mutable clock is owned
    here. Rig Profiles, artwork pixels and legacy layer configuration are unused.
    """
    if type(frame.frame_index) is not int or not 0 <= frame.frame_index < len(resource.image_grid):
        msg = "Sampled sheet frame is outside the sprite resource."
        raise ValueError(msg)
    if type(frame.flip) not in (int, bool) or frame.flip not in (0, 1):
        msg = "Invalid animation entry flip."
        raise ValueError(msg)
    body = resource.action_points.get(frame.frame_index)
    points = {name: point.data() for name, point in body.items()} if body is not None else {}
    return SpriteSample(instance_id, (resource.frame_width, resource.frame_height), frame.frame_index,
                        points, frame.sprite_offset.data(), bool(frame.flip), facing, presentation)


@dataclass(frozen=True)
class ActionPointRelationship:
    parent_instance: str
    parent_point: str
    child_instance: str
    child_point: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for value in (
            self.parent_instance, self.parent_point, self.child_instance, self.child_point,
        )):
            msg = "Relationships need two instance ids and two Action Point names."
            raise ValueError(msg)


@dataclass(frozen=True)
class ResolvedPlacement:
    """Replacement child presentation, retaining its scale and own animation."""

    presentation: Presentation
    parent_point_position: Vector
    rendered_child_point: Vector
    artwork_origin: Vector


@dataclass(frozen=True)
class ResolutionFailure:
    code: Literal["instance_mismatch", "parent_point_unset", "child_point_unset"]
    instance_id: str
    point_name: str
    message: str


def resolve_relationship(relationship: ActionPointRelationship, parent: SpriteSample,
                         child: SpriteSample) -> ResolvedPlacement | ResolutionFailure:
    """Align the final rendered child point with the rendered parent point.

    Parent offsets contribute to the target. Child offsets are included in the
    placement calculation and remain unchanged in the sampled/authored data.
    The child's previous presentation position is replaced, not accumulated.
    Artwork flip, scale and logical facing are never inherited from the parent.
    """
    for sample, expected, name in ((parent, relationship.parent_instance, relationship.parent_point),
                                    (child, relationship.child_instance, relationship.child_point)):
        if sample.instance_id != expected:
            return ResolutionFailure("instance_mismatch", expected, name,
                                     f"Expected sample for instance {expected!r}, got {sample.instance_id!r}.")
    target = parent.rendered_point(relationship.parent_point)
    if target is None:
        return ResolutionFailure("parent_point_unset", parent.instance_id, relationship.parent_point,
                                 f"Parent Action Point {relationship.parent_point!r} is unset on sheet frame {parent.frame_index}.")
    point = child.action_points.get(relationship.child_point)
    if point is None:
        return ResolutionFailure("child_point_unset", child.instance_id, relationship.child_point,
                                 f"Child Action Point {relationship.child_point!r} is unset on sheet frame {child.frame_index}.")
    px, py = child.artwork_point(point)
    local_point = (px + child.sprite_offset[0], py + child.sprite_offset[1])
    x, y = child.presentation.vector(local_point)
    placed = Presentation((target[0] - x, target[1] - y), child.presentation.scale)
    return ResolvedPlacement(placed, target, placed.point(local_point), placed.point(child.sprite_offset))


FORMAT = "mdframeedit-preview"
FLAGS = ("follow_facing", "follow_action", "synchronized", "inherit_mirroring", "visible")
# Existing previews can retain this obsolete label flag; it has no effect.
LEGACY_FLAGS = ("reference_only",)


def write_workspace(path, data):
    path = Path(path)
    validate_workspace(data)
    references = [data["root"], *(part["sprite"] for part in data["parts"])]
    if any((path.parent / source).resolve() == path.resolve() for source in references):
        msg = "A preview cannot overwrite one of its sprite resources"
        raise FormatError(msg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        json.dump(data, file, indent=2)
        file.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_workspace(data):
    if not isinstance(data, dict) or data.get("format") != FORMAT or type(data.get("version")) is not int or data["version"] != 1:
        msg = "Invalid editor preview workspace"
        raise FormatError(msg)
    if set(data)-{"format","version","root","parts","order","root_visible","root_name"}:
        msg = "Unknown preview field; sprite data does not belong in an editor workspace"
        raise FormatError(msg)
    if "root_visible" in data and type(data["root_visible"]) is not bool:
        msg = "Root inspection visibility must be boolean"
        raise FormatError(msg)
    if "root_name" in data and not isinstance(data["root_name"],str):
        msg = "Root preview name must be text"
        raise FormatError(msg)
    if not isinstance(data.get("root"), str) or not data["root"]:
        msg = "Preview needs a root sprite path"
        raise FormatError(msg)
    parts = data.get("parts")
    if not isinstance(parts, list) or len(parts) > 10:
        msg = "Preview supports at most ten parts"
        raise FormatError(msg)
    for part in parts:
        if isinstance(part,dict) and set(part)-{"sprite","parent_point","child_point","action","name",*FLAGS,*LEGACY_FLAGS}:
            msg = "Unknown preview part field"
            raise FormatError(msg)
        if not isinstance(part, dict) or any(not isinstance(part.get(k), str) or not part[k] for k in ("sprite", "parent_point", "child_point")):
            msg = "Preview parts need sprite paths and named Action Points"
            raise FormatError(msg)
        if any(k in part and type(part[k]) is not bool for k in (*FLAGS, *LEGACY_FLAGS)):
            msg = "Preview flags must be boolean"
            raise FormatError(msg)
        if "action" in part and not isinstance(part["action"], str):
            msg = "Preview action must be text"
            raise FormatError(msg)
        if "name" in part and not isinstance(part["name"], str):
            msg = "Preview tab name must be text"
            raise FormatError(msg)
    order = data.get("order", list(range(len(parts)+1)))
    if not isinstance(order, list) or any(type(i) is not int for i in order) or sorted(order) != list(range(len(parts)+1)):
        msg = "Invalid preview draw order"
        raise FormatError(msg)
    return data


def read_workspace(path):
    try:
        return validate_workspace(json.loads(Path(path).read_text(encoding="utf-8")))
    except (ValueError, OSError) as error:
        msg = f"Cannot load preview: {error}"
        raise FormatError(msg) from error
