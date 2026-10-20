from __future__ import annotations

import csv
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from formats import validate_source
from utils import FormatError


def add_action_aliases(
    source: str | Path,
    destination: str | Path,
    action: str,
    fallbacks: list[str],
    index: int | None = None,
) -> tuple[Path, dict[str, int]]:
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    action = action.strip()
    fallbacks = list(dict.fromkeys(name.strip() for name in fallbacks if name.strip()))
    if not source.is_dir():
        msg = "Choose an existing source folder."
        raise ValueError(msg)
    if source == destination or source in destination.parents or destination in source.parents:
        msg = "The output folder must be separate from the source folder."
        raise ValueError(msg)
    if destination.exists():
        msg = "Choose a new output folder; existing folders are never overwritten."
        raise ValueError(msg)
    if not action or not fallbacks:
        msg = "Enter an action name and at least one fallback action."
        raise ValueError(msg)
    if action.casefold() in {name.casefold() for name in fallbacks}:
        msg = "The new action cannot be one of its own fallbacks."
        raise ValueError(msg)
    if index is not None and index < 0:
        msg = "The action index must be nonnegative or automatic."
        raise ValueError(msg)

    def scan_error(error: OSError) -> None:
        raise error

    files = []
    for directory, directories, names in os.walk(source, followlinks=False, onerror=scan_error):
        for name in directories + names:
            path = Path(directory) / name
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                msg_0 = f"Source contains a link or junction: {path}"
                raise ValueError(msg_0)
        files.extend(
            Path(directory) / name for name in names
            if name in {"FrameData.xml", "AnimData.xml"}
        )
    files.sort()
    if not files:
        msg = "No FrameData.xml or AnimData.xml files found in the source folder."
        raise ValueError(msg)

    destination.mkdir(parents=True, exist_ok=False)
    log_path = destination / "action-alias.log"
    suffix = 1
    while (source / log_path.name).exists():
        log_path = destination / f"action-alias-{suffix}.log"
        suffix += 1
    with log_path.open("x", encoding="utf-8", newline="") as log:
        writer = csv.writer(log, delimiter="\t")
        writer.writerow(["Source", str(source)])
        writer.writerow(["Output", str(destination)])
        writer.writerow(["Action", action])
        writer.writerow(["Fallback order", *fallbacks])
        writer.writerow(["Index", "Automatic per sprite" if index is None else index])
        writer.writerow(["File", "Status", "Details"])
        log.flush()
        counts: Counter[str] = Counter()
        try:
            # The directory was created exclusively above; the report name
            # cannot collide with anything in the source.
            shutil.copytree(source, destination, dirs_exist_ok=True)
        except Exception as error:
            writer.writerow(["", "COPY_ERROR", str(error)])
            log.flush()
            msg_0 = f"Copy failed. Partial output and log retained at {log_path}: {error}"
            raise OSError(msg_0) from error

        for original in files:
            relative = original.relative_to(source)
            path = destination / relative
            try:
                status, detail = _add_alias(path, action, fallbacks, index)
            except Exception as error:
                status, detail = "ERROR", str(error)
            counts[status] += 1
            writer.writerow([str(relative), status, detail])
            log.flush()
        writer.writerow(["TOTAL", "", "; ".join(f"{key}={value}" for key, value in sorted(counts.items()))])
    return log_path, dict(counts)


def _add_alias(path: Path, action: str, fallbacks: list[str], index: int | None) -> tuple[str, str]:
    tree = validate_source(path)
    root = tree.getroot()
    anims = root.find("Anims")
    by_name = {anim.findtext("Name").casefold(): anim for anim in anims}
    if action.casefold() in by_name:
        return "ALREADY_EXISTS", f"Kept {by_name[action.casefold()].findtext('Name')} unchanged."
    if root.find("FrameWidth") is None and (
        any(char in action for char in '/\\<>:"|?*') or action.endswith((".", " "))
    ):
        msg = f"Action name cannot be used in AnimData.xml: {action!r}"
        raise FormatError(msg)
    exact_names = {anim.findtext("Name"): anim for anim in anims}
    for fallback in fallbacks:
        matches = [anim for name, anim in exact_names.items() if name.casefold() == fallback.casefold()]
        if fallback in exact_names:
            selected = exact_names[fallback]
            break
        if len(matches) > 1:
            msg = f"Ambiguous fallback {fallback!r}; use its exact spelling."
            raise FormatError(msg)
        if matches:
            selected = matches[0]
            break
    else:
        return "NO_FALLBACK", f"Skipped; none available: {', '.join(fallbacks)}"

    # Direct references work in both formats; multi sheets forbid copy chains.
    target = selected
    while target.find("CopyOf") is not None:
        target = exact_names[target.findtext("CopyOf")]
    indices = {int(anim.findtext("Index", "-1")) for anim in anims}
    chosen_index = max(indices, default=-1) + 1 if index is None else index
    if chosen_index in indices:
        return "INDEX_CONFLICT", f"Skipped; index {chosen_index} is already in use."
    alias = ET.SubElement(anims, "Anim")
    ET.SubElement(alias, "Name").text = action
    ET.SubElement(alias, "Index").text = str(chosen_index)
    ET.SubElement(alias, "CopyOf").text = target.findtext("Name")

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".xml", delete=False) as output:
            temporary = Path(output.name)
            tree.write(output, encoding="utf-8", xml_declaration=True)
        validate_source(temporary)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return "ADDED", (
        f"{action} -> CopyOf {target.findtext('Name')} (index {chosen_index}; "
        f"matched fallback {selected.findtext('Name')})"
    )
