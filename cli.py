"""Sprite conversion using the standalone Pillow/XML backend."""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
import os
import multiprocessing
from pathlib import Path
import sys
import subprocess
import xml.etree.ElementTree as ET


FORMAT_FILES = {"single": "FrameData.xml", "multi": "AnimData.xml"}


@dataclass(frozen=True)
class ConversionJob:
    source: Path
    destination: Path
    target_format: str
    overwrite: bool = False
    trim_copies: bool = True
    collapse_singles: bool = True


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


def create_parser():
    parser = argparse.ArgumentParser(
        description="Convert single-sheet and multi-sheet sprite bundles without opening the editor."
    )
    parser.add_argument("input", type=Path, help="source XML file or sprite folder")
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
        help="also search subfolders for source sprite XML files",
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
        if source.suffix.lower() != ".xml":
            raise ConversionError("Input must be an XML file or a sprite folder.")
        sources = [source]
        root = source.parent
    else:
        root = source
        expected = FORMAT_FILES["multi" if args.to == "single" else "single"].casefold()
        candidates = source.rglob("*") if args.recursive else source.iterdir()
        sources = sorted(
            path
            for path in candidates
            if path.is_file()
            and path.name.casefold() == expected
            and not path.is_relative_to(output)
        )
        if not sources:
            msg = (
                f"No {expected} files found in {source}. "
                "Use --recursive to search subfolders."
            )
            raise ConversionError(msg)
    jobs = [
        ConversionJob(
            path,
            output / path.parent.relative_to(root),
            args.to,
            args.overwrite,
            args.trim_copies,
            args.collapse_singles,
        )
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
            or (
                not job.overwrite
                and any(not path.is_dir() for path in job.destination.iterdir())
            )
        ):
            msg = f"Destination already contains files; use --overwrite to replace them: {job.destination}"
            raise ConversionError(msg)
        root = ET.parse(job.source).getroot()
        source_format = "single" if root.find("FrameWidth") is not None else "multi"
        if source_format == job.target_format:
            msg_0 = f"Input is already {job.target_format}-sheet data."
            raise ConversionError(msg_0)
        from conversion import SpriteDocument
        from data import SheetFormat

        document = SpriteDocument.load(job.source)
        document.trim_copies = job.trim_copies
        document.collapse_singles = job.collapse_singles
        target = (
            SheetFormat.SINGLE if job.target_format == "single" else SheetFormat.MULTI
        )
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


def main(argv=None):
    multiprocessing.freeze_support()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "convert":
        arguments.pop(0)
    parser = create_parser()
    args = parser.parse_args(arguments)
    # Keep spawned Windows workers on a CLI-only launcher instead of importing
    # the GUI entry point in main.py when the child interpreter starts.
    launcher = getattr(sys.modules["__main__"], "__file__", "")
    if (
        args.executor == "process"
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


if __name__ == "__main__":
    sys.exit(main())
