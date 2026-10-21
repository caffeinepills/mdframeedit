"""Editable project profiles and derived sprite builds, separate from authoring."""

from __future__ import annotations

import copy
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from conversion import SpriteDocument
from data import AnimGroup, Offset
from formats import NativeFormat
from formats.base import DocumentT
from formats.native import write_packed_document
from utils import FormatError


@dataclass(frozen=True)
class PackingSettings:
    padding: int = 1
    power_of_two: bool = False


@dataclass(frozen=True)
class PaletteSettings:
    enabled: bool = True
    include_transparent: bool = False


@dataclass(frozen=True)
class RushSettings:
    enabled: bool = False
    divisor: int = 3
    actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class CustomAnimation:
    source: str
    frames: tuple[int, ...] | None = None
    duration: int | None = None
    index: int | None = None


@dataclass(frozen=True)
class AnimationSettings:
    fallbacks: dict[str, tuple[str, ...]] = field(default_factory=dict)
    custom: dict[str, CustomAnimation] = field(default_factory=dict)
    rush: RushSettings = field(default_factory=RushSettings)


@dataclass(frozen=True)
class ProjectProfile:
    packing: PackingSettings = field(default_factory=PackingSettings)
    palette: PaletteSettings = field(default_factory=PaletteSettings)
    animations: AnimationSettings = field(default_factory=AnimationSettings)


@dataclass(frozen=True)
class BuildJob:
    source: Path
    destination: Path


def plan_builds(source: str | Path, destination: str | Path) -> list[BuildJob]:
    """Discover one preferred source per sprite folder and preserve its relative path."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.exists():
        msg = f"Build source does not exist: {source}"
        raise FormatError(msg)
    root = source.parent if source.is_file() else source
    if destination == root or destination in root.parents or root in destination.parents:
        msg = "Build output must be separate from the source sprite folder."
        raise FormatError(msg)
    if destination.exists() and not destination.is_dir():
        msg = "Build output must be a folder."
        raise FormatError(msg)
    if source.is_file():
        if source.suffix.lower() not in (".json", ".xml"):
            msg = "Build source must be a sprite JSON/XML file or folder."
            raise FormatError(msg)
        return [BuildJob(source, destination)]
    priority = {"sprite.json": 0, "framedata.xml": 1, "animdata.xml": 2}
    preferred: dict[Path, Path] = {}
    for path in source.rglob("*"):
        if path.name.casefold() not in priority or path.is_symlink() or not path.is_file():
            continue
        previous = preferred.get(path.parent)
        if previous is None or priority[path.name.casefold()] < priority[previous.name.casefold()]:
            preferred[path.parent] = path
        elif path.name.casefold() == previous.name.casefold():
            msg = f"Ambiguous sprite sources in {path.parent}: {previous.name}, {path.name}."
            raise FormatError(msg)
    if not preferred:
        msg = "No Sprite.json, FrameData.xml or AnimData.xml files found in the source tree."
        raise FormatError(msg)
    jobs: list[BuildJob] = []
    destinations: set[str] = set()
    for folder in sorted(preferred, key=lambda path: (len(path.parts), str(path).casefold())):
        target = (destination / folder.relative_to(root)).resolve()
        if not target.is_relative_to(destination):
            msg = f"Build destination escapes the output folder: {target}"
            raise FormatError(msg)
        key = str(target).casefold()
        if key in destinations:
            msg = f"Multiple sprites would write to the same destination: {target}"
            raise FormatError(msg)
        destinations.add(key)
        jobs.append(BuildJob(preferred[folder], target))
    return jobs


def _object(value: object, fields: tuple[str, ...], where: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) - set(fields):
        msg = f"{where}: expected an object with only {', '.join(fields)}."
        raise FormatError(msg)
    return cast("dict[str, object]", value)


def _int(value: object, minimum: int, where: str) -> int:
    if type(value) is not int or value < minimum:
        msg = f"{where}: expected an integer >= {minimum}."
        raise FormatError(msg)
    return value


def _bool(value: object, where: str) -> bool:
    if type(value) is not bool:
        msg = f"{where}: expected a boolean."
        raise FormatError(msg)
    return value


def _name(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"{where}: expected a nonblank animation name."
        raise FormatError(msg)
    return value


def _names(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        msg = f"{where}: expected a list of animation names."
        raise FormatError(msg)
    return tuple(_name(item, where) for item in value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            msg = f"Duplicate project profile field: {key!r}."
            raise FormatError(msg)
        result[key] = value
    return result


def profile_from_data(value: object) -> ProjectProfile:
    """Validate profile settings once, before loading artwork or writing outputs."""
    data = _object(value, ("format", "version", "packing", "palette", "animations"), "Project profile")
    if data.get("format") != "mdframeedit-project" or type(data.get("version")) is not int or data["version"] != 1:
        msg = "Unsupported project profile format or version."
        raise FormatError(msg)
    packing = _object(data.get("packing", {}), ("padding", "power_of_two"), "packing")
    palette = _object(data.get("palette", {}), ("enabled", "include_transparent"), "palette")
    anim = _object(data.get("animations", {}), ("fallbacks", "custom", "rush"), "animations")
    rush = _object(anim.get("rush", {}), ("enabled", "divisor", "actions"), "animations.rush")
    fallbacks = anim.get("fallbacks", {})
    custom = anim.get("custom", {})
    if not isinstance(fallbacks, dict) or not isinstance(custom, dict):
        msg = "animations.fallbacks and animations.custom must be objects keyed by animation name."
        raise FormatError(msg)
    fallback_rules: dict[str, tuple[str, ...]] = {}
    for name, choices in fallbacks.items():
        name = _name(name, "Fallback name")
        fallback_rules[name] = _names(choices, name)
        if not fallback_rules[name]:
            msg = f"{name}: provide at least one fallback."
            raise FormatError(msg)
    custom_rules: dict[str, CustomAnimation] = {}
    for name, rule in custom.items():
        name = _name(name, "Custom animation name")
        definition = _object(rule, ("source", "frames", "duration", "index"), name)
        frames: tuple[int, ...] | None = None
        if "frames" in definition:
            selection = definition["frames"]
            if not isinstance(selection, list) or not selection:
                msg = f"{name}: frames must be a nonempty list of timeline positions."
                raise FormatError(msg)
            frames = tuple(_int(i, 0, f"{name}: frames") for i in selection)
        custom_rules[name] = CustomAnimation(
            _name(definition.get("source"), f"{name}: source"), frames,
            _int(definition["duration"], 1, f"{name}: duration") if "duration" in definition else None,
            _int(definition["index"], 0, f"{name}: index") if "index" in definition else None,
        )
    if fallback_rules.keys() & custom_rules.keys():
        msg = "An animation cannot have both a custom definition and a fallback rule."
        raise FormatError(msg)
    return ProjectProfile(
        PackingSettings(_int(packing.get("padding", 1), 1, "packing.padding"),
                        _bool(packing.get("power_of_two", False), "packing.power_of_two")),
        PaletteSettings(_bool(palette.get("enabled", True), "palette.enabled"),
                        _bool(palette.get("include_transparent", False), "palette.include_transparent")),
        AnimationSettings(fallback_rules, custom_rules, RushSettings(
            _bool(rush.get("enabled", False), "animations.rush.enabled"),
            _int(rush.get("divisor", 3), 1, "animations.rush.divisor"),
            _names(rush.get("actions", []), "animations.rush.actions"))),
    )


def load_profile(path: str | Path) -> ProjectProfile:
    """Read user-editable UTF-8 JSON without imports, plugins or compiled settings."""
    try:
        return profile_from_data(json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object))
    except (ValueError, UnicodeError) as error:
        msg = f"Invalid project profile JSON: {error}"
        raise FormatError(msg) from error


def apply_profile(document: SpriteDocument, profile: ProjectProfile) -> SpriteDocument:
    """Apply game rules to a private copy; authoring state and files are untouched."""
    result = copy.deepcopy(document)
    originals = {group.name: group for group in result.groups}
    rules = profile.animations
    resolved: dict[str, AnimGroup] = {}
    visiting: set[str] = set()

    def resolve(name: str) -> AnimGroup:
        if name in resolved:
            return resolved[name]
        if name in visiting:
            msg = f"Cyclic animation rules involving {name!r}."
            raise FormatError(msg)
        if name not in rules.custom and name in originals:
            resolved[name] = originals[name]
            return originals[name]
        visiting.add(name)
        if name in rules.custom:
            rule = rules.custom[name]
            source = originals[name] if rule.source == name and name in originals else resolve(rule.source)
            group = copy.deepcopy(source)
            group.name, group.copy_name = name, ""
            group.idx = rule.index if rule.index is not None else originals[name].idx if name in originals else -1
            sequences = group.directions[:1] if group.is_single_direction else group.directions
            for sequence in sequences:
                frames = sequence.frames
                if rule.frames is not None:
                    if any(index >= len(frames) for index in rule.frames):
                        msg = f"{name}: selected frame is outside a source direction."
                        raise FormatError(msg)
                    sequence.frames = [copy.deepcopy(frames[index]) for index in rule.frames]
                for index, frame in enumerate(sequence.frames):
                    frame.idx = index
                    if rule.duration is not None:
                        frame.duration = rule.duration
            if rule.frames is not None:
                for event in ("rush_frame", "hit_frame", "return_frame"):
                    position = getattr(group, event)
                    setattr(group, event, rule.frames.index(position) if position in rule.frames else -1)
        elif name in rules.fallbacks:
            for candidate in rules.fallbacks[name]:
                if candidate in originals or candidate in rules.custom or candidate in rules.fallbacks:
                    group = copy.deepcopy(resolve(candidate))
                    group.name, group.idx, group.copy_name = name, -1, ""
                    break
            else:
                msg = f"{name}: none of the configured fallbacks exist."
                raise FormatError(msg)
        else:
            msg = f"Unknown animation source: {name!r}."
            raise FormatError(msg)
        visiting.remove(name)
        resolved[name] = group
        return group

    order = list(dict.fromkeys([*originals, *rules.custom, *rules.fallbacks]))
    result.groups = [resolve(name) for name in order]
    rush = rules.rush
    if rush.enabled:
        missing = set(rush.actions) - resolved.keys()
        if missing:
            msg = f"Unknown rush animations: {', '.join(sorted(missing))}."
            raise FormatError(msg)
        for group in result.groups:
            if (rush.actions and group.name not in rush.actions) or group.rush_frame < 0:
                continue
            sequences = group.directions[:1] if group.is_single_direction else group.directions
            for sequence in sequences:
                if not sequence.frames:
                    continue
                anchor = sequence.frames[group.rush_frame]
                for frame in sequence.frames[group.rush_frame + 1:]:
                    for attribute in ("sprite_offset", "shadow_offset"):
                        origin = getattr(anchor, attribute)
                        offset = getattr(frame, attribute)
                        setattr(frame, attribute, Offset(origin.x + (offset.x - origin.x) // rush.divisor,
                                                         origin.y + (offset.y - origin.y) // rush.divisor))
    return result


def palette_data(document: SpriteDocument, include_transparent: bool) -> dict[str, object]:
    """Extract deterministic exact RGBA colors from logical artwork, excluding padding."""
    counts: Counter[tuple[int, int, int, int]] = Counter(document.sheet_image.get_flattened_data())
    colors = sorted(color for color in counts if include_transparent or color[3] != 0)
    return {"format": "mdframeedit-palette", "version": 1,
            "colors": [list(color) for color in colors], "counts": [counts[color] for color in colors]}


class ProjectBuildFormat(NativeFormat):
    """Build-time adapter reusing the native schema and bundle transaction."""

    def __init__(self, profile: ProjectProfile) -> None:
        self.profile = profile

    def write(self, document: SpriteDocument, directory: Path, filename: str) -> None:
        write_packed_document(document, directory / filename, padding=self.profile.packing.padding,
                       power_of_two=self.profile.packing.power_of_two)
        if self.profile.palette.enabled:
            (directory / "Palette.json").write_text(
                json.dumps(palette_data(document, self.profile.palette.include_transparent), separators=(",", ":")) + "\n",
                encoding="utf-8")

    def validate_written(self, directory: Path, filename: str, document_class: type[DocumentT]) -> None:
        super().validate_written(directory, filename, document_class)
        # Validate the optional palette before committing any destination files.
        if self.profile.palette.enabled:
            json.loads((directory / "Palette.json").read_text(encoding="utf-8"))

    def obsolete_files(self, directory: Path) -> tuple[str, ...]:
        # Remove reports produced by the earlier build implementation on rebuild.
        return ("Build.json",) if self.profile.palette.enabled else ("Build.json", "Palette.json")


def build_sprite(source: str | Path, profile: ProjectProfile, destination: str | Path, *, overwrite: bool = False) -> Path:
    """Build one sprite into an isolated output bundle with rollback on failure."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source.is_dir():
        candidates = {path.name.casefold(): path for path in source.iterdir() if path.is_file()}
        source = next((candidates[name] for name in ("sprite.json", "framedata.xml", "animdata.xml") if name in candidates), source)
    if not source.is_file() or source.suffix.lower() not in (".json", ".xml"):
        msg = "Choose a sprite JSON/XML file or a folder containing Sprite.json, FrameData.xml or AnimData.xml."
        raise FormatError(msg)
    if destination == source.parent or destination in source.parents or source.parent in destination.parents:
        msg = "Build output must be separate from the source sprite folder."
        raise FormatError(msg)
    if destination.exists() and (not destination.is_dir() or
        (not overwrite and any(not path.is_dir() for path in destination.iterdir()))):
        msg = "Build output already contains files; use --overwrite to replace generated assets."
        raise FormatError(msg)
    document = apply_profile(SpriteDocument.load(source), profile)
    return document.export(destination, ProjectBuildFormat(profile))
