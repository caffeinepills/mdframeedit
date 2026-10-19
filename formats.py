"""Separate source and export validation for the different formats."""

from pathlib import Path
import xml.etree.ElementTree as ET

from PIL import Image

from data import SheetFormat
from utils import FormatError, integer, known_children, get_image_size


def detect_format(root):
    # Use FrameWidth as a measure of the Sheet format.
    return SheetFormat.SINGLE if root.find('FrameWidth') is not None else SheetFormat.MULTI

_frame_tags = {
    'Name', 'Index', 'CopyOf', 'FrameWidth', 'FrameHeight', 'ShadowSize',
    'RushFrame', 'HitFrame', 'ReturnFrame', 'FrameIndex', 'HFlip', 'Duration',
    'XOffset', 'YOffset'
}

def validate_source(path, expected=None):
    path = Path(path)
    tree = ET.parse(path)
    root = tree.getroot()

    for node in root.iter():
        if node.attrib:
            raise FormatError(f'Unsupported attributes on {node.tag}; cannot preserve them.')
        if node.tag in _frame_tags and len(node):
            raise FormatError(f'Unsupported nested data in {node.tag}; cannot preserve it.')
    format_name = detect_format(root)
    if expected is not None and format_name != expected:
        raise FormatError(f"This is {format_name.value} data; use its matching loader.")
    single = format_name == SheetFormat.SINGLE
    known_children(root, {'ShadowSize', 'Anims', 'FrameWidth', 'FrameHeight'} if single
                   else {'ShadowSize', 'Anims'})
    if integer(root, 'ShadowSize', 1) not in (0, 1, 2):
        raise FormatError('ShadowSize must be 0 (small), 1 (normal) or 2 (large).')
    anims = root.find('Anims')
    if anims is None or not len(anims):
        raise FormatError('No animations found.')
    known_children(anims, {'Anim'}, repeated={'Anim'})
    names, indices, by_name = set(), set(), {}
    if single:
        w, h = integer(root, 'FrameWidth'), integer(root, 'FrameHeight')
        size = get_image_size(path.parent / 'Anim.png')
        if min(w, h) <= 0 or size[0] % w or size[1] % h:
            raise FormatError('Single-sheet dimensions must divide the sprite atlas.')
        if (path.parent / 'Offsets.png').exists() and get_image_size(path.parent / 'Offsets.png') != size:
            raise FormatError('Offsets.png dimensions must match Anim.png.')
        frame_count = size[0] // w * (size[1] // h)
    for anim in anims:
        name = anim.findtext('Name', '')
        # Names become filenames in the multi format; reject collisions and paths.
        key = name if single else name.casefold()
        if not name.strip() or key in names:
            raise FormatError(f"Missing or duplicate animation name: {name!r}.")
        if not single and (any(char in name for char in '/\\<>:"|?*') or name.endswith(('.', ' '))):
            raise FormatError(f"Animation name cannot be used as a filename: {name!r}.")
        names.add(key)
        by_name[name] = anim
        index = integer(anim, 'Index', -1)
        if index != -1:
            if index < 0 or index in indices:
                raise FormatError(f"Duplicate or invalid Index {index} for {name}.")
            indices.add(index)
        allowed = {'Name', 'Index', 'CopyOf', 'RushFrame', 'HitFrame', 'ReturnFrame'}
        allowed |= {'Sequences'} if single else {'FrameWidth', 'FrameHeight', 'Durations'}
        known_children(anim, allowed)
        if anim.find('CopyOf') is not None:
            known_children(anim, {'Name', 'Index', 'CopyOf'})
            continue
        if single:
            sequences = anim.find('Sequences')
            if sequences is None or len(sequences) not in (1, 8):
                raise FormatError(f'{name}: single sheets need one or eight sequences.')
            known_children(sequences, {'AnimSequence'}, repeated={'AnimSequence'})
            for sequence in sequences:
                known_children(sequence, {'AnimFrame'}, repeated={'AnimFrame'})
                for frame in sequence:
                    known_children(frame, {'FrameIndex', 'HFlip', 'Duration', 'Sprite', 'Shadow'})
                    if not 0 <= integer(frame, 'FrameIndex') < frame_count:
                        raise FormatError(f'{name}: frame index is outside the atlas.')
                    if integer(frame, 'Duration') <= 0 or integer(frame, 'HFlip') not in (0, 1):
                        raise FormatError(f'{name}: invalid duration or flip.')
                    for tag in ('Sprite', 'Shadow'):
                        offset = frame.find(tag)
                        if offset is None:
                            raise FormatError(f'{name}: missing {tag} offset.')
                        known_children(offset, {'XOffset', 'YOffset'})
                        integer(offset, 'XOffset')
                        integer(offset, 'YOffset')
        else:
            w, h = integer(anim, 'FrameWidth'), integer(anim, 'FrameHeight')
            if min(w, h) <= 0 or w % 2 or h % 2:
                raise FormatError(f'{name}: multi-sheet frame dimensions must be positive and even.')
            sizes = [get_image_size(path.parent / f'{name}-{suffix}.png')
                     for suffix in ('Anim', 'Offsets', 'Shadow')]
            if len(set(sizes)) != 1 or sizes[0][0] % w or sizes[0][1] % h or sizes[0][1] // h not in (1, 8):
                raise FormatError(f'{name}: invalid or mismatched PNG dimensions/directions.')
            durations = anim.find('Durations')
            if durations is None:
                raise FormatError(f'{name}: missing Durations.')
            known_children(durations, {'Duration'}, repeated={'Duration'})
            if len(durations) != sizes[0][0] // w:
                raise FormatError(f'{name}: duration count does not match the image columns.')
            for value in durations:
                try:
                    valid = int(value.text) > 0
                except (TypeError, ValueError):
                    valid = False
                if not valid:
                    raise FormatError(f'{name}: durations must be positive integers.')
            # Decode checks happen before the editor clears its current document.
            # Histograms count markers in Pillow without a Python loop per pixel.
            with Image.open(path.parent / f'{name}-Offsets.png') as source:
                offsets = source.convert('RGBA')
            with Image.open(path.parent / f'{name}-Shadow.png') as source:
                shadows = source.convert('RGBA')
            for row in range(sizes[0][1] // h):
                for column in range(sizes[0][0] // w):
                    box = (column * w, row * h, (column + 1) * w, (row + 1) * h)
                    colors = offsets.crop(box).getcolors(w * h)
                    counts = [sum(count for count, color in colors if color[3] and color[channel] == 255)
                              for channel in range(3)]
                    heads = sum(count for count, color in colors if color == (0, 0, 0, 255))
                    if counts != [1, 1, 1] or heads > 1:
                        raise FormatError(f'{name}[{row},{column}]: missing or ambiguous attachment markers.')
                    whites = sum(count for count, color in shadows.crop(box).getcolors(w * h)
                                 if color == (255, 255, 255, 255))
                    if whites != 1:
                        raise FormatError(f'{name}[{row},{column}]: expected one white shadow anchor.')
        for tag in ('RushFrame', 'HitFrame', 'ReturnFrame'):
            value = integer(anim, tag, -1)
            # Single sequences can have different lengths; events must fit every
            # nonempty sequence, without imposing multi-sheet timing rules.
            lengths = [len(sequence) for sequence in sequences if len(sequence)] if single else [len(durations)]
            if value < -1 or (value >= 0 and any(value >= count for count in lengths)):
                raise FormatError(f'{name}: {tag} is outside the animation.')
    for name, anim in by_name.items():
        seen = {name}
        source = anim.findtext('CopyOf')
        while source is not None:
            if source not in by_name or source in seen:
                raise FormatError(f'{name}: missing or cyclic CopyOf target.')
            seen.add(source)
            next_source = by_name[source].findtext('CopyOf')
            if not single and next_source is not None:
                raise FormatError(f'{name}: AnimData.xml does not allow chained copies.')
            source = next_source
    return tree


def validate_export(editor, format_name):
    single = format_name == SheetFormat.SINGLE
    if not editor.groups or editor.image_grid is None:
        raise FormatError('No sprite data to save.')
    if editor.shadow_size not in (0, 1, 2):
        raise FormatError('ShadowSize must be 0 (small), 1 (normal) or 2 (large).')
    names, indices = set(), set()
    for group in editor.groups:
        name = group.name
        key = name if single else name.casefold()
        if not name.strip() or key in names or (not single and (any(char in name for char in '/\\<>:"|?*') or name.endswith(('.', ' ')))):
            raise FormatError(f'Missing, duplicate or invalid animation name: {name!r}.')
        names.add(key)
        if group.idx != -1:
            if group.idx < 0 or group.idx in indices:
                raise FormatError(f'Duplicate or invalid Index {group.idx}.')
            indices.add(group.idx)
        if len(group.directions) != 8:
            raise FormatError(f'{name}: expected eight editing sequences.')
        if not single:
            timings = [tuple(frame.duration for frame in sequence.frames) for sequence in group.directions]
            if not timings[0] or any(timing != timings[0] for timing in timings[1:]):
                raise FormatError(f'{name}: Duration values and frame counts must match across all directions for AnimData.xml. Save as FrameData.xml to preserve them.')
        for sequence in group.directions:
            for value in (group.rush_frame, group.hit_frame, group.return_frame):
                if value < -1 or (sequence.frames and value >= 0 and value >= len(sequence.frames)):
                    raise FormatError(f'{name}: event frame is outside a sequence.')
            for frame in sequence.frames:
                if frame.duration <= 0 or not 0 <= frame.frame_index < len(editor.image_grid) or frame.flip not in (0, 1):
                    raise FormatError(f'{name}: invalid duration, frame index or flip.')
                if not single and frame.frame_index not in editor.action_points:
                    raise FormatError(f'{name}: attachment points are unavailable; cannot create AnimData.xml losslessly.')
    for index, body in editor.action_points.items():
        if not 0 <= index < len(editor.image_grid):
            raise FormatError('Attachment point frame is outside the atlas.')
        points = body.all_pos()
        if body.head != body.center and any(body.head == point for point in (body.left_hand, body.right_hand)):
            raise FormatError('Head attachment overlaps a hand and cannot be encoded losslessly in RGB markers.')
        if single and any(not (-(editor.frame_width // 2) <= point.x < editor.frame_width - editor.frame_width // 2
                               and -(editor.frame_height // 2) <= point.y < editor.frame_height - editor.frame_height // 2)
                          for point in points):
            raise FormatError('Attachment point is outside the single-sheet cell; cannot save without clipping it.')
    if not single:
        referenced = {frame.frame_index for group in editor.groups for sequence in group.directions
                      for frame in sequence.frames}
        for index in range(len(editor.image_grid)):
            if index in referenced:
                continue
            region = editor.image_grid[index]
            image = Image.frombytes('RGBA', (region.width, region.height),
                                    region.get_image_data().get_data('RGBA', -region.width * 4))
            if image.getbbox() is not None or index in editor.action_points:
                raise FormatError('Unreferenced atlas artwork/attachment data cannot be preserved in AnimData.xml; save FrameData.xml instead.')
