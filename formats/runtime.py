from __future__ import annotations

import hashlib
import json
import mmap
import struct
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

from utils import FormatError

from .native import _document_data, _unique_object, _validate_copy_targets
from .packing import pack_texture

if TYPE_CHECKING:
    from conversion import SpriteDocument

MAGIC = b"MDFRUN\0\0"
VERSION = 1
HEADER = struct.Struct("<8sII")  # magic, version, JSON byte length
FRAME = struct.Struct("<7i")
ENTRY = struct.Struct("<2i")
CELL = struct.Struct("<8i")
POINT = struct.Struct("<3i")
TABLES = {"frames": FRAME, "sequence_entries": ENTRY, "cells": CELL, "points": POINT}


def _integer(value: object, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**31 - 1:
        msg = "Runtime values must fit signed 32-bit integers."
        raise FormatError(msg)
    return value


def _encode(document: SpriteDocument, texture: str):
    image, layout = pack_texture(document.sheet_image, (document.frame_width, document.frame_height))
    data = _document_data(document, texture, layout)
    binary = bytearray()

    def rows(records, record):
        span = [len(binary), len(records)]
        try:
            for values in records:
                binary.extend(record.pack(*values))
        except (struct.error, OverflowError) as error:
            msg = "Runtime numeric data exceeds the signed 32-bit range."
            raise FormatError(msg) from error
        _integer(len(binary))
        return span

    tables = {"frames": rows(data["frames"], FRAME)}
    sequence_start = len(binary)
    sequences = []
    for sequence in data["sequences"]:
        elapsed = 0
        entries = []
        for frame_id in sequence:
            elapsed += int(data["frames"][frame_id][1])
            entries.append((frame_id, elapsed))
        sequences.append(rows(entries, ENTRY))
    tables["sequence_entries"] = [sequence_start, (len(binary) - sequence_start) // ENTRY.size]
    names = sorted(set(data.get("action_point_names", ())) | {
        name for points in data["action_points"] if points for name in points
    })
    name_ids = {name: index for index, name in enumerate(names)}
    # Cells have fixed positions even when their points are sparse/unavailable.
    tables["cells"] = [len(binary), len(data["regions"])]
    binary.extend(bytes(CELL.size * len(data["regions"])))
    point_start = len(binary)
    cells = []
    for region, points in zip(data["regions"], data["action_points"]):
        records = [(name_ids[name], *points[name]) for name in sorted(points or {})]
        offset, count = rows(records, POINT)
        cells.append([*(region or [0] * 6), offset, -1 if points is None else count])
    for index, values in enumerate(cells):
        CELL.pack_into(binary, tables["cells"][0] + index * CELL.size, *values)
    tables["points"] = [point_start, (len(binary) - point_start) // POINT.size]
    index = {
        "format": "mdfe-runtime", "version": VERSION, "texture": texture,
        "texture_size": list(image.size), "frame_size": data["frame_size"],
        "shadow_size": data["shadow_size"], "ticks_per_second": 60,
        "data_bytes": len(binary), "data_sha256": hashlib.sha256(binary).hexdigest(),
        "point_names": names, "tables": tables,
        "animations": {
            name: {**{key: value for key, value in animation.items() if key != "directions"},
                   "directions": [sequences[sequence] for sequence in animation["directions"]]}
            for name, animation in data["animations"].items()
        },
    }
    return image, index, binary


def write_document(document: SpriteDocument, path: Path) -> None:
    """Write a runtime container and its packed PNG inside the staging folder."""
    texture = path.with_suffix(".png").name
    image, index, binary = _encode(document, texture)
    raw = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    padding = bytes(-(HEADER.size + len(raw)) % 4)
    image.save(path.with_name(texture), format="PNG")
    with path.open("wb") as output:
        output.write(HEADER.pack(MAGIC, VERSION, len(raw)))
        output.write(raw)
        output.write(padding)
        output.write(binary)


def validate_written(path: Path) -> None:
    """Check staged metadata and graphics before committing the bundle."""
    with RuntimeSprite(path) as sprite:
        with Image.open(path.with_name(sprite.index["texture"])) as image:
            image.load()
            if list(image.size) != sprite.index["texture_size"]:
                msg = "Runtime texture dimensions do not match the index."
                raise FormatError(msg)


class RuntimeSprite:
    """Read-only mapped numeric metadata; no PNG I/O or whole-frame expansion.

    Validation and checksum verification happen once on open. Frame lookups
    unpack only the requested entry and frame. Keep entity playback state outside
    this shared resource. Use as a context manager or call close explicitly.
    """

    def __init__(self, path: str | Path) -> None:
        self.closed = False
        self._file = Path(path).open("rb")
        try:
            if self._file.seek(0, 2) < HEADER.size:
                msg = "Truncated runtime container header."
                raise FormatError(msg)
            self.mapping = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
            magic, version, length = HEADER.unpack_from(self.mapping)
            if magic != MAGIC or version != VERSION:
                msg = "Unsupported runtime container magic or version."
                raise FormatError(msg)
            self.data_offset = (HEADER.size + length + 3) & ~3
            if self.data_offset > len(self.mapping):
                msg = "Truncated runtime container index."
                raise FormatError(msg)
            if any(self.mapping[HEADER.size + length:self.data_offset]):
                msg = "Invalid runtime container alignment padding."
                raise FormatError(msg)
            try:
                self.index = json.loads(self.mapping[HEADER.size:HEADER.size + length],
                                        object_pairs_hook=_unique_object)
            except (ValueError, UnicodeError) as error:
                msg = f"Invalid runtime container JSON: {error}"
                raise FormatError(msg) from error
            self._validate()
        except Exception:
            self.close()
            raise

    def __enter__(self) -> RuntimeSprite:
        self._ensure_open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        if not self.closed:
            if hasattr(self, "mapping"):
                self.mapping.close()
            self._file.close()
            self.closed = True

    def _ensure_open(self) -> None:
        if self.closed:
            msg = "Runtime container is closed."
            raise RuntimeError(msg)

    def _span(self, span, record, table=None):
        if not isinstance(span, list) or len(span) != 2:
            msg = "Invalid runtime table span."
            raise FormatError(msg)
        offset, count = (_integer(value) for value in span)
        end = offset + count * record.size
        if offset % 4 or end > self.index["data_bytes"]:
            msg = "Runtime table span is outside numeric storage."
            raise FormatError(msg)
        if table is not None:
            start, size = self.index["tables"][table]
            if offset < start or (offset - start) % record.size or end > start + size * record.size:
                msg = f"Runtime span is outside the {table} table."
                raise FormatError(msg)
        return offset, count

    def _row(self, record: struct.Struct, offset: int) -> tuple[int, ...]:
        self._ensure_open()
        return record.unpack_from(self.mapping, self.data_offset + offset)

    def _validate(self) -> None:
        index = self.index
        fields = {"format", "version", "texture", "texture_size", "frame_size", "shadow_size",
                  "ticks_per_second", "data_bytes", "data_sha256", "point_names", "tables", "animations"}
        if not isinstance(index, dict) or set(index) != fields:
            msg = "Missing or unknown runtime index fields."
            raise FormatError(msg)
        if index["format"] != "mdfe-runtime" or type(index["version"]) is not int or index["version"] != VERSION:
            msg = "Unsupported runtime index version."
            raise FormatError(msg)
        size = _integer(index["data_bytes"])
        if size % 4 or self.data_offset + size != len(self.mapping):
            msg = "Runtime numeric storage size mismatch."
            raise FormatError(msg)
        with memoryview(self.mapping) as view:
            with view[self.data_offset:] as payload:
                if hashlib.sha256(payload).hexdigest() != index["data_sha256"]:
                    msg = "Runtime numeric checksum mismatch."
                    raise FormatError(msg)
        texture = index["texture"]
        if (not isinstance(texture, str) or not texture or any(char in texture for char in '/\\<>:"|?*')
                or Path(texture).suffix.lower() != ".png"):
            msg = "Runtime texture must be a local PNG filename."
            raise FormatError(msg)
        for field in ("frame_size", "texture_size"):
            value = index[field]
            if not isinstance(value, list) or len(value) != 2:
                msg = f"Invalid runtime {field}."
                raise FormatError(msg)
            for number in value:
                _integer(number, 1)
        if index["shadow_size"] is not None and _integer(index["shadow_size"]) not in (0, 1, 2):
            msg = "Invalid runtime shadow size."
            raise FormatError(msg)
        if _integer(index["ticks_per_second"], 1) != 60:
            msg = "Unsupported runtime tick rate."
            raise FormatError(msg)
        from project import validate_point_names
        validate_point_names(index["point_names"], "runtime point_names")
        if not isinstance(index["tables"], dict) or set(index["tables"]) != set(TABLES):
            msg = "Invalid runtime table directory."
            raise FormatError(msg)
        end = 0
        for name, record in TABLES.items():
            offset, count = self._span(index["tables"][name], record)
            if offset != end:
                msg = "Runtime numeric tables must be contiguous and ordered."
                raise FormatError(msg)
            end = offset + count * record.size
        if end != size:
            msg = "Runtime tables do not cover numeric storage."
            raise FormatError(msg)
        for position in range(index["tables"]["frames"][1]):
            cell, duration, flip, *_ = self._table_row("frames", position)
            if not 0 <= cell < index["tables"]["cells"][1] or duration <= 0 or flip not in (0, 1):
                msg = "Invalid runtime frame record."
                raise FormatError(msg)
        for position in range(index["tables"]["cells"][1]):
            x, y, width, height, tx, ty, offset, count = self.cell(position)
            if min(x, y, width, height, tx, ty) < 0 or bool(width) != bool(height):
                msg = "Invalid runtime cell region."
                raise FormatError(msg)
            if (x + width > index["texture_size"][0] or y + height > index["texture_size"][1]
                    or tx + width > index["frame_size"][0] or ty + height > index["frame_size"][1]):
                msg = "Runtime cell region is outside texture or logical frame."
                raise FormatError(msg)
            if count < -1:
                msg = "Invalid runtime point count."
                raise FormatError(msg)
            self._span([offset, max(count, 0)], POINT, "points")
            previous = -1
            for item in range(max(count, 0)):
                name, *_ = self._row(POINT, offset + item * POINT.size)
                if not previous < name < len(index["point_names"]):
                    msg = "Invalid or unsorted runtime point name reference."
                    raise FormatError(msg)
                previous = name
        if not isinstance(index["animations"], dict):
            msg = "Invalid runtime animation directory."
            raise FormatError(msg)
        checked = set()
        indices = set()
        copies = {}
        for name, animation in index["animations"].items():
            if not name.strip() or not isinstance(animation, dict) or "directions" not in animation or set(animation) - {
                "directions", "index", "rush_frame", "hit_frame", "return_frame", "copy_of",
            }:
                msg = "Invalid runtime animation header."
                raise FormatError(msg)
            if "copy_of" in animation:
                if not isinstance(animation["copy_of"], str):
                    msg = "Invalid runtime animation copy hint."
                    raise FormatError(msg)
                copies[name] = animation["copy_of"]
            if "index" in animation:
                number = _integer(animation["index"])
                if number in indices:
                    msg = "Duplicate runtime animation index."
                    raise FormatError(msg)
                indices.add(number)
            directions = animation["directions"]
            if not isinstance(directions, list) or len(directions) not in (1, 8):
                msg = "Runtime animations require one or eight directions."
                raise FormatError(msg)
            events = [_integer(animation.get(event, -1), -1)
                      for event in ("rush_frame", "hit_frame", "return_frame")]
            for span in directions:
                offset, count = self._span(span, ENTRY, "sequence_entries")
                if count and any(event >= count for event in events):
                    msg = "Runtime event is outside its sequence."
                    raise FormatError(msg)
                if (offset, count) in checked:
                    continue
                checked.add((offset, count))
                elapsed = 0
                for position in range(count):
                    frame_id, end_tick = self._row(ENTRY, offset + position * ENTRY.size)
                    if not 0 <= frame_id < index["tables"]["frames"][1]:
                        msg = "Invalid runtime sequence frame reference."
                        raise FormatError(msg)
                    elapsed += self._table_row("frames", frame_id)[1]
                    if end_tick != elapsed:
                        msg = "Invalid runtime sequence timing boundary."
                        raise FormatError(msg)
        _validate_copy_targets(copies, set(index["animations"]))

    def _table_row(self, name: str, position: int) -> tuple[int, ...]:
        self._ensure_open()
        offset, count = self.index["tables"][name]
        if type(position) is not int or not 0 <= position < count:
            raise IndexError(position)
        record = TABLES[name]
        return self._row(record, offset + position * record.size)

    def sequence(self, animation: str, direction: int = 0) -> tuple[int, int]:
        self._ensure_open()
        if type(direction) is not int or not 0 <= direction < 8:
            raise IndexError(direction)
        directions = self.index["animations"][animation]["directions"]
        offset, count = directions[0 if len(directions) == 1 else direction]
        return offset, count

    def frame(self, animation: str, direction: int, position: int) -> tuple[int, ...]:
        offset, count = self.sequence(animation, direction)
        if type(position) is not int or not 0 <= position < count:
            raise IndexError(position)
        frame_id, _ = self._row(ENTRY, offset + position * ENTRY.size)
        return self._table_row("frames", frame_id)

    def frame_at_tick(self, animation: str, direction: int, tick: int) -> tuple[int, ...]:
        """Sample a looping sequence via binary search, without a duration list."""
        offset, count = self.sequence(animation, direction)
        if not count:
            msg = "Empty runtime sequence"
            raise IndexError(msg)
        if type(tick) is not int:
            msg = "Runtime ticks must be integers."
            raise TypeError(msg)
        tick %= self._row(ENTRY, offset + (count - 1) * ENTRY.size)[1]
        low, high = 0, count
        while low < high:
            middle = (low + high) // 2
            if self._row(ENTRY, offset + middle * ENTRY.size)[1] <= tick:
                low = middle + 1
            else:
                high = middle
        return self.frame(animation, direction, low)

    def cell(self, cell_id: int) -> tuple[int, ...]:
        return self._table_row("cells", cell_id)

    def points(self, cell_id: int, *, mirrored: bool = False) -> dict[str, tuple[int, int]] | None:
        *_, offset, count = self.cell(cell_id)
        if count == -1:
            return None
        result = {}
        width = self.index["frame_size"][0]
        for position in range(count):
            name, x, y = self._row(POINT, offset + position * POINT.size)
            result[self.index["point_names"][name]] = (
                width - 1 - 2 * (width // 2) - x if mirrored else x, y,
            )
        return result
