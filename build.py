#!/usr/bin/env python3
"""Host-side wrapper: builds the Docker image and runs the boot.img build.

Anything this script does not recognise is forwarded to the container
(for example ``--magisk``, ``--kernel-ref`` or ``--jobs``).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMAGE = "gauguin-bootfix"
INPUT_FLAGS = ("zip", "boot", "magisk_apk")


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    """Parse host options; return them together with the forwarded ones."""
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--zip", type=Path, help="LineageOS update zip")
    parser.add_argument("--boot", type=Path, help="stock LineageOS boot.img")
    parser.add_argument("--magisk-apk", type=Path, help="Magisk APK to use")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "cache")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output")
    parser.add_argument("--engine", default="docker", choices=("docker", "podman"))
    return parser.parse_known_args()


def container_command(args: argparse.Namespace, forwarded: list[str]) -> list[str]:
    """Assemble the ``docker run`` command line."""
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    base = [args.engine, "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}"]
    if args.engine == "podman":
        base.append("--userns=keep-id")
    volumes = [
        "-v", f"{args.cache_dir.resolve()}:/cache",
        "-v", f"{args.output_dir.resolve()}:/output",
        "-v", f"{(ROOT / 'patches').resolve()}:/patches:ro",
    ]
    inputs: list[str] = []
    for name in INPUT_FLAGS:
        path = getattr(args, name)
        if path is None:
            continue
        if not path.is_file():
            sys.exit(f"File not found: {path}")
        target = f"/input/{name}/{path.name}"
        volumes += ["-v", f"{path.resolve()}:{target}:ro"]
        inputs += [f"--{name.replace('_', '-')}", target]
    return base + volumes + [IMAGE] + inputs + forwarded


def main() -> int:
    """Entry point."""
    args, forwarded = parse_args()
    if not (args.zip or args.boot):
        sys.exit("Give either --zip or --boot (a stock LineageOS image).")
    subprocess.run([args.engine, "build", "-t", IMAGE, str(ROOT / "container")], check=True)
    result = subprocess.run(container_command(args, forwarded), check=False)
    if result.returncode == 0:
        print(f"\nDone. Images are in {args.output_dir.resolve()}")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
