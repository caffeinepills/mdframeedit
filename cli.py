"""Sprite conversion using the standalone Pillow/JSON/XML backend."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
import os
import multiprocessing
from pathlib import Path
import sys
import subprocess
import xml.etree.ElementTree as ET


FORMAT_FILES = {"native": "Sprite.json", "single": "FrameData.xml", "multi": "AnimData.xml"}


@dataclass(frozen=True)
class ConversionJob:
    source: Path
    destination: Path
    target_format: str
    overwrite: bool = False
    trim_copies: bool = True
    collapse_singles: bool = True
    verify_png: bool = True
    optimize_png: bool = True


@dataclass(frozen=True)
class ConversionResult:
    source: Path
    destination: Path
    error: str = ""


class ConversionError(Exception):
    pass


def positive_integer(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def add_png_arguments(parser):
    parser.add_argument("--no-png-verification", dest="verify_png", action="store_false",
                        help="skip PNG pixel round-trip checks; keep format validation")
    parser.add_argument("--no-png-optimization", dest="optimize_png", action="store_false",
                        help="write ordinary RGBA PNGs without palette or compression optimization")


def create_parser():
    parser = argparse.ArgumentParser(
        description="Convert native JSON and single-sheet/multi-sheet XML sprites without opening the editor.",
        epilog="Other commands: palette INPUT; build INPUT --profile PROFILE.yaml -o OUTPUT.",
    )
    parser.add_argument("input", type=Path, help="source JSON/XML file or sprite folder")
    parser.add_argument(
        "--to", choices=FORMAT_FILES, required=True, help="destination sheet format"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="destination folder (keeps relative sprite folders for batches)",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="also search subfolders for source sprite JSON/XML files",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=positive_integer,
        default=min(4, os.cpu_count() or 1),
        help="parallel workers (default: %(default)s)",
    )
    parser.add_argument(
        "--executor",
        choices=("serial", "process"),
        default="process",
        help="conversion executor (default: %(default)s; serial uses no worker pool)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="allow replacing files in nonempty destination folders",
    )
    parser.add_argument(
        "--no-trim-copies",
        dest="trim_copies",
        action="store_false",
        help="write complete data instead of CopyOf references",
    )
    parser.add_argument(
        "--no-collapse-singles",
        dest="collapse_singles",
        action="store_false",
        help="write all eight directions, including identical sequences",
    )
    add_png_arguments(parser)
    return parser


def plan_conversions(args):
    source = args.input.resolve()
    output = args.output.resolve()
    if not source.exists():
        msg = f"Input does not exist: {source}"
        raise ConversionError(msg)
    if output.exists() and not output.is_dir():
        msg = f"Output must be a folder: {output}"
        raise ConversionError(msg)
    if source.is_file():
        if source.suffix.lower() not in (".xml", ".json"):
            raise ConversionError("Input must be a JSON/XML file or a sprite folder.")
        sources = [source]
        root = source.parent
    else:
        root = source
        expected = (["framedata.xml", "animdata.xml"] if args.to == "native" else
                    ["sprite.json", FORMAT_FILES["multi" if args.to == "single" else "single"].casefold()])
        candidates = source.rglob("*") if args.recursive else source.iterdir()
        sources = sorted(
            path
            for path in candidates
            if path.is_file()
            and path.name.casefold() in expected
            and not path.is_relative_to(output)
        )
        # A folder can contain both an imported XML and its native authoring
        # document. Prefer the native source (or single XML for native import).
        preferred = {}
        for path in sources:
            previous = preferred.get(path.parent)
            if previous is None or expected.index(path.name.casefold()) < expected.index(previous.name.casefold()):
                preferred[path.parent] = path
        sources = sorted(preferred.values())
        if not sources:
            msg = (
                f"No {' or '.join(expected)} files found in {source}. "
                "Use --recursive to search subfolders."
            )
            raise ConversionError(msg)
    jobs = [
        ConversionJob(path, output / path.parent.relative_to(root), args.to,
                      args.overwrite, args.trim_copies, args.collapse_singles,
                      args.verify_png, args.optimize_png)
        for path in sources
    ]
    source_folders = {path.parent for path in sources}
    destinations = set()
    for job in jobs:
        if job.destination in source_folders:
            msg = f"Choose an output folder separate from the source sprites: {job.destination}"
            raise ConversionError(msg)
        key = str(job.destination).casefold()
        if key in destinations:
            msg0 = f"Multiple sprites would write to the same destination: {job.destination}"
            raise ConversionError(msg0)
        destinations.add(key)
    return jobs


def convert_job(job):
    """Convert an independent document and write to its own folder."""
    try:
        if job.destination.exists() and (
            not job.destination.is_dir()
            or (not job.overwrite and any(not path.is_dir() for path in job.destination.iterdir()))
        ):
            msg = f"Destination already contains files; use --overwrite to replace them: {job.destination}"
            raise ConversionError(msg)
        from conversion import SpriteDocument
        from data import SheetFormat
        from utils import png_save_options

        document = SpriteDocument.load(job.source)
        source_format = {SheetFormat.NATIVE: "native", SheetFormat.SINGLE: "single", SheetFormat.MULTI: "multi"}[document.sheet_format]
        if source_format == job.target_format:
            msg_0 = "Input is already native JSON data." if source_format == "native" else f"Input is already {job.target_format}-sheet data."
            raise ConversionError(msg_0)
        document.trim_copies = job.trim_copies
        document.collapse_singles = job.collapse_singles
        target = {"native": SheetFormat.NATIVE, "single": SheetFormat.SINGLE, "multi": SheetFormat.MULTI}[job.target_format]
        with png_save_options(verify=job.verify_png, optimize=job.optimize_png):
            document.export(job.destination, target)
        return ConversionResult(job.source, job.destination)
    except Exception as error:
        return ConversionResult(
            job.source, job.destination, str(error) or type(error).__name__
        )


def run_conversions(jobs, workers, executor="process"):
    if executor == "serial":
        for job in jobs:
            yield convert_job(job)
        return
    with ProcessPoolExecutor(
        max_workers=min(workers, len(jobs)),
        mp_context=multiprocessing.get_context("spawn"),
    ) as pool:
        futures = {pool.submit(convert_job, job): job for job in jobs}
        try:
            for future in as_completed(futures):
                yield future.result()
        except KeyboardInterrupt:
            # Finish active transactional writes, but cancel queued sprites.
            for future in futures:
                future.cancel()
            raise


def palette_main(argv: list[str]) -> int:
    """Print artwork statistics without starting the editor or writing files."""
    from PIL import Image

    from conversion import SpriteDocument
    from utils import FormatError, analyze_artwork

    parser = argparse.ArgumentParser(
        prog=f"{Path(sys.argv[0]).name} palette",
        description="Report artwork palette values and translucent pixels without opening the editor.",
    )
    parser.add_argument("input", type=Path, help="sprite JSON/XML file, sprite folder, or PNG sheet")
    args = parser.parse_args(argv)
    source = args.input.resolve()
    try:
        if source.is_dir():
            candidates = [
                path for path in source.iterdir()
                if path.is_file() and path.name.casefold() in {"sprite.json", "framedata.xml", "animdata.xml"}
            ]
            native = [path for path in candidates if path.name.casefold() == "sprite.json"]
            if native:
                candidates = native
            if len(candidates) != 1:
                msg = "Choose a sprite JSON/XML file directly. The folder must contain Sprite.json or exactly one FrameData.xml or AnimData.xml."
                raise ConversionError(msg)
            source = candidates[0]
        if source.suffix.lower() == ".png":
            with Image.open(source) as image:
                info = analyze_artwork(image)
        elif source.suffix.lower() in (".xml", ".json"):
            document = SpriteDocument.load(source)
            info = analyze_artwork(document.sheet_image)
        else:
            msg = "Input must be a sprite JSON/XML file, sprite folder, or PNG sheet."
            raise ConversionError(msg)
    except (ConversionError, FormatError, OSError, ET.ParseError) as error:
        print(f"Could not read palette: {error}", file=sys.stderr)
        return 1

    translucent = sum(color.pixels for color in info.palette if 0 < color.rgba[3] < 255)
    print(f"Source: {source}")
    print(f"Sheet size: {info.width} x {info.height}")
    print(f"Palette count (unique RGBA values, including transparent): {len(info.palette)}")
    print(f"Has translucent pixels: {'yes' if translucent else 'no'}")
    print(f"Translucent pixels (alpha 1-254): {translucent}")
    print("RGB hex   RGBA                  Pixels")
    for color in info.palette:
        rgb = "#" + "".join(f"{channel:02X}" for channel in color.rgba[:3])
        rgba = ", ".join(map(str, color.rgba))
        print(f"{rgb}   {rgba:<21} {color.pixels}")
    return 0


def main(argv=None):
    multiprocessing.freeze_support()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "build":
        return build_main(arguments[1:])
    if arguments and arguments[0] == "palette":
        return palette_main(arguments[1:])
    if arguments and arguments[0] == "convert":
        arguments.pop(0)
    parser = create_parser()
    args = parser.parse_args(arguments)
    # Keep spawned Windows workers on a CLI-only launcher instead of importing
    # the GUI entry point in main.py when the child interpreter starts.
    launcher = getattr(sys.modules["__main__"], "__file__", "")
    if (
        args.executor == "process"
        and not getattr(sys, "frozen", False)
        and "__compiled__" not in globals()
        and launcher
        and Path(launcher).resolve() == Path(__file__).with_name("main.py").resolve()
    ):
        return subprocess.call(
            [sys.executable, str(Path(__file__).resolve()), *arguments]
        )
    try:
        jobs = plan_conversions(args)
    except (ConversionError, OSError) as error:
        parser.error(str(error))
    successes = failures = 0
    try:
        for result in run_conversions(jobs, args.jobs, args.executor):
            if result.error:
                failures += 1
                print(
                    f"FAILED {result.source}: {result.error}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                successes += 1
                print(
                    f"OK {result.source} -> {result.destination / FORMAT_FILES[args.to]}",
                    flush=True,
                )
    except KeyboardInterrupt:
        print("Conversion interrupted.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError) as error:
        print(f"Could not run conversion workers: {error}", file=sys.stderr)
        return 1
    print(f"Converted {successes}; failed {failures}.")
    return 1 if failures else 0


def build_main(argv: list[str]) -> int:
    """Build sprites recursively using one editable project profile."""
    from project import build_sprite, load_profile, plan_builds
    from utils import FormatError, png_save_options

    parser = argparse.ArgumentParser(prog=f"{Path(sys.argv[0]).name} build",
        description="Build packed sprites from a source file or folder tree using one YAML project profile.")
    parser.add_argument("input", type=Path, help="source sprite JSON/XML file or folder (searched recursively)")
    parser.add_argument("--profile", type=Path, required=True, help="editable project profile YAML")
    parser.add_argument("-o", "--output", type=Path, required=True, help="destination folder (preserves relative sprite folders)")
    parser.add_argument("--overwrite", action="store_true", help="replace generated files in an existing output folder")
    add_png_arguments(parser)
    args = parser.parse_args(argv)
    try:
        profile = load_profile(args.profile)
        jobs = plan_builds(args.input, args.output)
        # Protect the shared profile at any generated sprite destination.
        managed = {"sprite.json", "sprite.png", "palette.json", "build.json"}
        if (args.profile.name.casefold() in managed and
            any(args.profile.resolve().parent == job.destination for job in jobs)):
            raise FormatError("Build output would overwrite the project profile.")
    except (FormatError, OSError, ValueError, ET.ParseError) as error:
        print(f"Build failed: {error}", file=sys.stderr)
        return 1
    successes = failures = 0
    for job in jobs:
        try:
            with png_save_options(verify=args.verify_png, optimize=args.optimize_png):
                target = build_sprite(job.source, profile, job.destination, overwrite=args.overwrite)
        except (FormatError, OSError, ValueError, ET.ParseError) as error:
            failures += 1
            print(f"FAILED {job.source}: {error}", file=sys.stderr, flush=True)
        else:
            successes += 1
            print(f"OK {job.source} -> {target}", flush=True)
    print(f"Built {successes}; failed {failures}.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
