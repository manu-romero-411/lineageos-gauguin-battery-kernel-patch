#!/usr/bin/env python3
"""Rebuild the newest LineageOS gauguin boot.img with the qpnp-qg battery fix.

Meant to run on a GitHub Actions Ubuntu runner, in two steps:

``latest``
    Ask the LineageOS download API for the newest nightly and write its
    details to ``build.json`` (plus step outputs when run under Actions).

``build``
    Take that nightly's stock ``boot.img``, build the kernel at the commit the
    nightly was built from with the battery fix applied, swap it into the
    stock image and patch a second copy with Magisk.  Writes
    ``boot_<date>.img``, ``boot-magisk_<date>.img`` and ``release-notes.md``.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from pathlib import Path
from typing import Iterator
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
SHIMS = Path(__file__).resolve().parent / "shims"
PATCHES = ROOT / "patches"

LINEAGE_API = "https://download.lineageos.org/api/v2/devices/{device}/builds"
DEFAULT_DEVICE = "gauguin"
DEFAULT_KERNEL_URL = "https://github.com/LineageOS/android_kernel_xiaomi_gauguin"
GOOGLESOURCE = "https://android.googlesource.com/platform/prebuilts"
DEFAULT_CLANG_REF = "bd96dfe349c962681f0e5388af874c771ef96670"
DEFAULT_CLANG_DIR = "clang-r416183b"
DEFAULT_GCC_REF = "refs/tags/android-12.1.0_r27"
MAGISK_API = "https://api.github.com/repos/topjohnwu/Magisk/releases?per_page=10"
USER_AGENT = "gauguin-bootfix"

BATTERY_DRIVER = "drivers/power/supply/qcom/qpnp-qg.c"
BATTERY_PATTERN = re.compile(r"(chip->batterysecret_support\s*=\s*)([^;]+);")
RELEASE_HASH = re.compile(r"-g([0-9a-f]{7,40})(?:-dirty)?$")
DEEPEN_STEPS = (50, 200, 1000)

GZIP_MAGIC = b"\x1f\x8b"
ARM64_IMAGE_MAGIC = b"ARM\x64"  # at offset 56 of a raw arm64 Image
IKCFG_START = b"IKCFG_ST"
IKCFG_END = b"IKCFG_ED"
KERNEL_TARGETS = {"raw": "Image", "gzip": "Image.gz-dtb"}
LZ4_LEGACY_MAGIC = bytes.fromhex("02214c18")

MAKE_VARS = [
    "ARCH=arm64",
    "CC=ccache clang",
    "CLANG_TRIPLE=aarch64-linux-gnu-",
    "CROSS_COMPILE=aarch64-linux-android-",
    "CROSS_COMPILE_ARM32=arm-linux-androideabi-",
]

# Files boot_patch.sh needs next to it (Magisk v31+ dropped stub.apk).
MAGISK_REQUIRED = ("boot_patch.sh", "util_functions.sh", "magiskboot", "magiskinit",
                   "magisk", "init-ld")

MAGISK_DEFAULT_ENV = {
    "BOOTMODE": "true",
    "KEEPVERITY": "true",
    "KEEPFORCEENCRYPT": "true",
    "PATCHVBMETAFLAG": "false",
    "RECOVERYMODE": "false",
    "LEGACYSAR": "false",
}

log = logging.getLogger("bootfix")


class BuildError(RuntimeError):
    """Raised for any expected, actionable failure."""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def run(cmd: list, cwd: Path | None = None, env: dict | None = None,
        capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command, logging it; fail loudly on a non-zero exit unless told not to."""
    words = [str(word) for word in cmd]
    log.info("$ %s", " ".join(words))
    return subprocess.run(
        words,
        cwd=cwd,
        env=env,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )


def output_of(cmd: list, cwd: Path | None = None) -> str:
    """Return the stripped stdout of a command (which must succeed)."""
    return run(cmd, cwd=cwd, capture=True).stdout.strip()


@contextlib.contextmanager
def group(title: str) -> Iterator[None]:
    """Fold the enclosed log lines into a collapsible group on GitHub Actions."""
    actions = os.environ.get("GITHUB_ACTIONS") == "true"
    if actions:
        print(f"::group::{title}", flush=True)
    else:
        log.info("== %s ==", title)
    try:
        yield
    finally:
        if actions:
            print("::endgroup::", flush=True)


def http_request(url: str, token: str = "") -> Request:
    """Build a request with our User-Agent and, for GitHub, an optional token."""
    headers = {"User-Agent": USER_AGENT}
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    return Request(url, headers=headers)


def fetch_json(url: str, token: str = "") -> object:
    """GET ``url`` and decode it as JSON."""
    with urlopen(http_request(url, token), timeout=60) as response:
        return json.load(response)


def download(url: str, dest: Path, retries: int = 3) -> None:
    """Download ``url`` to ``dest``, retrying transient failures."""
    for attempt in range(1, retries + 1):
        log.info("Downloading %s (attempt %d)", url, attempt)
        try:
            with urlopen(http_request(url), timeout=300) as response, dest.open("wb") as handle:
                shutil.copyfileobj(response, handle, 1 << 20)
            return
        except OSError as err:
            if attempt == retries:
                raise BuildError(f"Could not download {url}: {err}") from err
            time.sleep(10 * attempt)


def sha256_of(path: Path) -> str:
    """Return the hex SHA-256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def set_outputs(**values: str) -> None:
    """Write step outputs when running under GitHub Actions."""
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


# --------------------------------------------------------------------------
# LineageOS nightly lookup
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Nightly:
    """The LineageOS build a boot image is derived from."""

    version: str
    date: str               # yyyymmdd
    timestamp: int          # build time, seconds since the epoch
    zip_name: str
    source_name: str        # boot.img, or the full zip when no boot.img is published
    source_url: str
    source_sha256: str

    @property
    def needs_payload(self) -> bool:
        """True when boot.img has to be extracted from the update zip."""
        return self.source_name != "boot.img"

    def save(self, path: Path) -> None:
        """Write this nightly to a JSON file."""
        path.write_text(json.dumps(dataclasses.asdict(self), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Nightly:
        """Read a nightly written by :meth:`save`."""
        return cls(**json.loads(path.read_text(encoding="utf-8")))


def pick_nightly(builds: list[dict], date: str | None) -> Nightly:
    """Return the newest nightly (or the one from ``date``, yyyymmdd) of the API answer."""
    candidates = sorted(
        (b for b in builds if b.get("type", "nightly") == "nightly"),
        key=lambda b: int(b.get("datetime", 0)),
        reverse=True,
    )
    for build in candidates:
        build_date = str(build.get("date", "")).replace("-", "")
        if date and build_date != date:
            continue
        files = {f.get("filename", ""): f for f in build.get("files", [])}
        zips = [f for name, f in files.items()
                if name.startswith("lineage-") and name.endswith(".zip")]
        if not zips:
            continue
        source = files.get("boot.img") or zips[0]
        return Nightly(
            version=str(build.get("version", "")),
            date=build_date,
            timestamp=int(build.get("datetime", 0)),
            zip_name=zips[0]["filename"],
            source_name=source["filename"],
            source_url=source["url"],
            source_sha256=source.get("sha256", ""),
        )
    wanted = f"no {date} nightly" if date else "no nightly"
    raise BuildError(f"The LineageOS API lists {wanted} with an update zip.")


def cmd_latest(args: argparse.Namespace) -> None:
    """``latest`` subcommand: resolve the nightly and record it."""
    builds = fetch_json(LINEAGE_API.format(device=args.device))
    if not isinstance(builds, list):
        raise BuildError("Unexpected answer from the LineageOS API.")
    nightly = pick_nightly(builds, args.date or None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    nightly.save(args.output)
    log.info("Newest nightly: %s (LineageOS %s, source %s)",
             nightly.zip_name, nightly.version, nightly.source_name)
    set_outputs(date=nightly.date, version=nightly.version, zip_name=nightly.zip_name,
                needs_payload=str(nightly.needs_payload).lower())


# --------------------------------------------------------------------------
# Stock boot image
# --------------------------------------------------------------------------

def obtain_stock_boot(nightly: Nightly, workdir: Path) -> Path:
    """Download the nightly's boot.img (directly or out of the update zip)."""
    source = workdir / nightly.source_name
    download(nightly.source_url, source)
    if nightly.source_sha256:
        actual = sha256_of(source)
        if actual != nightly.source_sha256.lower():
            raise BuildError(f"SHA-256 mismatch for {nightly.source_name}: got {actual}.")
    if not nightly.needs_payload:
        return source
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        if "boot.img" in names:
            archive.extract("boot.img", workdir)
            source.unlink()
            return workdir / "boot.img"
        if "payload.bin" not in names:
            raise BuildError("The update zip has neither boot.img nor payload.bin.")
        archive.extract("payload.bin", workdir)
    source.unlink()
    out_dir = workdir / "payload_out"
    run(["payload-dumper-go", "-p", "boot", "-o", out_dir, workdir / "payload.bin"])
    found = sorted(out_dir.rglob("boot.img"))
    if not found:
        raise BuildError("payload-dumper-go did not produce a boot.img.")
    return found[0]


class BootImage:
    """Thin wrapper around magiskboot working on an unpacked boot image."""

    def __init__(self, magiskboot: Path, workdir: Path) -> None:
        self.magiskboot = magiskboot
        self.workdir = workdir

    def _mb(self, *args: str) -> None:
        run([self.magiskboot, *args], cwd=self.workdir)

    def unpack(self, source: Path) -> None:
        """Unpack without decompressing anything (components stay untouched)."""
        self.workdir.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, self.workdir / "boot.img")
        self._mb("unpack", "-n", "boot.img")

    def kernel_format(self) -> str:
        """Return ``raw`` (plain arm64 Image) or ``gzip`` (Image.gz-dtb)."""
        data = (self.workdir / "kernel").read_bytes()
        if data[:2] == GZIP_MAGIC:
            return "gzip"
        if data[56:60] == ARM64_IMAGE_MAGIC:
            return "raw"
        raise BuildError("Unsupported stock kernel format (neither raw Image nor gzip).")

    def _plain_kernel(self) -> bytes:
        """Return the stock kernel with any gzip layer removed."""
        data = (self.workdir / "kernel").read_bytes()
        if self.kernel_format() == "gzip":
            data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data)
        return data

    def kernel_release(self) -> str | None:
        """Return the ``uname -r`` string embedded in the stock kernel.

        The version must start with a digit: the kernel also contains the
        ``Linux version %s ...`` format string, which must not match.
        """
        match = re.search(rb"Linux version (\d\S*) ", self._plain_kernel())
        return match.group(1).decode() if match else None

    def embedded_config(self) -> str | None:
        """Return the .config embedded in the stock kernel (IKCONFIG), if any."""
        data = self._plain_kernel()
        start = data.find(IKCFG_START)
        end = data.find(IKCFG_END, start)
        if start < 0 or end < 0:
            return None
        blob = data[start + len(IKCFG_START):end]
        try:
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(blob).decode("utf-8")
        except (zlib.error, UnicodeDecodeError):
            return None

    def swap_kernel(self, new_kernel: Path) -> None:
        """Replace the kernel image."""
        shutil.copy(new_kernel, self.workdir / "kernel")

    def ensure_gzip_ramdisk(self) -> None:
        """Recompress an lz4_legacy ramdisk as gzip.

        magiskboot's lz4_legacy repacking miscalculates chunk sizes on this
        device and corrupts the ramdisk (boot-time kernel panic), so the
        ramdisk is normalised to gzip before any repack.
        """
        ramdisk = self.workdir / "ramdisk.cpio"
        if not ramdisk.exists():
            return
        with ramdisk.open("rb") as handle:
            if handle.read(4) != LZ4_LEGACY_MAGIC:
                return
        log.info("Ramdisk is lz4_legacy; converting to gzip")
        self._mb("decompress", "ramdisk.cpio", "ramdisk.raw")
        self._mb("compress=gzip", "ramdisk.raw", "ramdisk.cpio")
        (self.workdir / "ramdisk.raw").unlink()

    def repack(self, output: Path) -> None:
        """Repack all components into ``output``."""
        self._mb("repack", "-n", "boot.img", "new-boot.img")
        shutil.copy(self.workdir / "new-boot.img", output)


# --------------------------------------------------------------------------
# Kernel
# --------------------------------------------------------------------------

def fetch_toolchain(url: str, dest: Path) -> Path:
    """Download and unpack a toolchain tarball into ``dest`` (cached by URL)."""
    marker = dest / ".source-url"
    if marker.exists() and marker.read_text(encoding="utf-8") == url:
        log.info("Toolchain cached: %s", dest.name)
        return dest
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    archive = dest.parent / f"{dest.name}.tar.gz"
    download(url, archive)
    run(["tar", "-xzf", archive, "-C", dest])
    archive.unlink()
    marker.write_text(url, encoding="utf-8")
    return dest


def prepare_toolchains(args: argparse.Namespace) -> list[Path]:
    """Download (or reuse) clang and GCC 4.9; return their ``bin`` directories."""
    toolchains = args.toolchain_dir
    clang = fetch_toolchain(
        f"{GOOGLESOURCE}/clang/host/linux-x86/+archive/{args.clang_ref}/{args.clang_dir}.tar.gz",
        toolchains / "clang",
    )
    gcc64 = fetch_toolchain(
        f"{GOOGLESOURCE}/gcc/linux-x86/aarch64/aarch64-linux-android-4.9/+archive/"
        f"{args.gcc_ref}.tar.gz",
        toolchains / "gcc64",
    )
    gcc32 = fetch_toolchain(
        f"{GOOGLESOURCE}/gcc/linux-x86/arm/arm-linux-androideabi-4.9/+archive/"
        f"{args.gcc_ref}.tar.gz",
        toolchains / "gcc32",
    )
    return [clang / "bin", gcc64 / "bin", gcc32 / "bin"]


def git_commit(src: Path, rev: str) -> str | None:
    """Return the full hash ``rev`` resolves to, or None if unknown/ambiguous."""
    result = run(["git", "rev-parse", "--verify", "-q", f"{rev}^{{commit}}"],
                 cwd=src, capture=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def commit_before(src: Path, timestamp: int) -> str | None:
    """Return the last first-parent commit of FETCH_HEAD made before ``timestamp``."""
    if not timestamp:
        return None
    result = run(["git", "rev-list", "-1", "--first-parent", f"--before={timestamp}",
                  "FETCH_HEAD"], cwd=src, capture=True, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def locate_kernel_commit(src: Path, ref: str, stock_release: str | None,
                         timestamp: int) -> str:
    """Find the kernel commit the nightly was built from.

    The stock release string ends in ``-g<hash>`` (CONFIG_LOCALVERSION_AUTO):
    the shallow history is deepened until that commit shows up.  Without a
    usable hash, the last first-parent commit before the build time is used,
    and the branch tip as a last resort.
    """
    match = RELEASE_HASH.search(stock_release or "")
    wanted = match.group(1) if match else None
    for depth in (0, *DEEPEN_STEPS):
        if depth:
            run(["git", "fetch", "-q", f"--deepen={depth}", "origin", ref], cwd=src)
        commit = git_commit(src, wanted) if wanted else commit_before(src, timestamp)
        if commit:
            log.info("Kernel commit %s (%s)", commit,
                     "matches the stock kernel" if wanted else "last before the nightly")
            return commit
    if wanted:
        log.warning("Commit %s of the stock kernel not found on %s", wanted, ref)
        commit = commit_before(src, timestamp)
        if commit:
            log.warning("Using %s, the last commit before the nightly was built", commit)
            return commit
    commit = output_of(["git", "rev-parse", "FETCH_HEAD"], cwd=src)
    log.warning("Using the tip of %s (%s)", ref, commit)
    return commit


def sync_kernel(args: argparse.Namespace, ref: str, stock_release: str | None,
                timestamp: int) -> tuple[Path, str]:
    """Fetch the kernel source of the nightly; return (tree, commit)."""
    src = args.work_dir / "kernel"
    shutil.rmtree(src, ignore_errors=True)
    src.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=src)
    run(["git", "remote", "add", "origin", args.kernel_url], cwd=src)
    try:
        run(["git", "fetch", "-q", "--depth", "1", "origin", ref], cwd=src)
    except subprocess.CalledProcessError as err:
        raise BuildError(f"Could not fetch '{ref}' from {args.kernel_url}.") from err
    commit = locate_kernel_commit(src, ref, stock_release, timestamp)
    run(["git", "checkout", "-q", "-f", commit], cwd=src)
    return src, commit


def apply_battery_fix(src: Path) -> None:
    """Force ``chip->batterysecret_support = false`` in the qpnp-qg driver.

    Fails loudly if the assignment is not found, so an upstream change never
    results in a silently unpatched kernel.
    """
    target = src / BATTERY_DRIVER
    if not target.exists():
        raise BuildError(f"{BATTERY_DRIVER} not found in the kernel tree.")
    text = target.read_text(encoding="utf-8")
    matches = BATTERY_PATTERN.findall(text)
    if not matches:
        raise BuildError(
            "No 'chip->batterysecret_support = ...;' assignment found in "
            f"{BATTERY_DRIVER}; upstream changed, the fix needs a manual look."
        )
    target.write_text(BATTERY_PATTERN.sub(r"\g<1>false;", text), encoding="utf-8")
    log.info("batterysecret_support: %d assignment(s) forced to false", len(matches))
    run(["git", "diff", "--stat"], cwd=src)


def apply_extra_patches(src: Path) -> list[str]:
    """Apply every ``patches/*.patch`` (sorted) with ``git apply``; return their names."""
    applied = []
    for patch in sorted(PATCHES.glob("*.patch")):
        run(["git", "apply", "--check", patch], cwd=src)
        run(["git", "apply", patch], cwd=src)
        applied.append(patch.name)
    return applied


def relax_frame_warn(config: str) -> str:
    """Disable the stack-frame-size warning (set CONFIG_FRAME_WARN=0).

    With -Werror the AOSP clang used here trips over frame sizes that the
    clang LineageOS builds with does not (e.g. sha3_256_hmac in the maxim
    driver); the warning is only advisory.
    """
    pattern = re.compile(r"^CONFIG_FRAME_WARN=.*$", re.MULTILINE)
    if pattern.search(config):
        return pattern.sub("CONFIG_FRAME_WARN=0", config)
    return config + "\nCONFIG_FRAME_WARN=0\n"


class KernelBuilder:
    """Drives the out-of-tree kernel build with the AOSP toolchains."""

    def __init__(self, src: Path, out: Path, args: argparse.Namespace,
                 toolchain_bins: list[Path]) -> None:
        self.src = src
        self.out = out
        self.jobs = args.jobs
        self.extra_vars = [f"KCFLAGS={args.kcflags}"] if args.kcflags else []
        self.env = dict(os.environ)
        self.env["PATH"] = os.pathsep.join([str(p) for p in toolchain_bins] + [self.env["PATH"]])
        self.env.setdefault("CCACHE_DIR", str(args.work_dir / "ccache"))
        self.env["KBUILD_BUILD_USER"] = "bootfix"
        self.env["KBUILD_BUILD_HOST"] = "github"

    def make(self, *targets: str) -> None:
        """Run make with the shared cross-compilation variables."""
        run(
            ["make", f"-j{self.jobs}", f"O={self.out}", *MAKE_VARS, *self.extra_vars, *targets],
            cwd=self.src,
            env=self.env,
        )

    def configure(self, defconfig: str | None, stock_config: str | None) -> None:
        """Create .config from a defconfig or from the stock kernel's config."""
        shutil.rmtree(self.out, ignore_errors=True)
        self.out.mkdir(parents=True)
        config_file = self.out / ".config"
        if defconfig:
            self.make(defconfig)
            config = config_file.read_text(encoding="utf-8")
        else:
            config = stock_config or ""
        config_file.write_text(relax_frame_warn(config), encoding="utf-8")
        self.make("olddefconfig")

    def pin_release(self, stock_release: str | None, commit: str) -> str:
        """Make the kernel release string match the stock one; return it.

        Vendor modules check the vermagic string, so the ``-g<hash>`` suffix is
        taken from the stock kernel instead of being derived from the (now
        dirty) git tree.
        """
        scm = self.src / ".scmversion"
        scm.write_text("", encoding="utf-8")
        self.make("prepare")
        release_file = self.out / "include" / "config" / "kernel.release"
        release = release_file.read_text(encoding="utf-8").strip()
        if stock_release and stock_release.startswith(release):
            scm.write_text(stock_release[len(release):], encoding="utf-8")
            log.info("Kernel release pinned to %s", stock_release)
            return stock_release
        suffix = f"-g{commit[:12]}"
        scm.write_text(suffix, encoding="utf-8")
        log.warning("Stock release %r does not extend %r; vendor modules may refuse to load.",
                    stock_release, release)
        return release + suffix

    def build(self, defconfig: str | None, stock_config: str | None, target: str,
              stock_release: str | None, commit: str) -> tuple[Path, str]:
        """Configure and build ``target``; return (image path, kernel release)."""
        self.configure(defconfig, stock_config)
        release = self.pin_release(stock_release, commit)
        self.make(target)
        image = self.out / "arch" / "arm64" / "boot" / target
        if not image.exists():
            raise BuildError(f"Build finished but {image} is missing.")
        return image, release


def find_defconfig(src: Path) -> str:
    """Auto-discover a ``*gauguin*defconfig`` (last-resort fallback)."""
    configs = src / "arch" / "arm64" / "configs"
    found = sorted(p.relative_to(configs).as_posix() for p in configs.rglob("*gauguin*defconfig"))
    if len(found) != 1:
        raise BuildError(
            f"The stock kernel embeds no config and defconfig candidates are {found}; "
            "pass --defconfig."
        )
    log.info("Using defconfig %s", found[0])
    return found[0]


# --------------------------------------------------------------------------
# Magisk
# --------------------------------------------------------------------------

def pick_magisk_asset(releases: list[dict], stable_only: bool) -> tuple[str, dict]:
    """Return (tag, APK asset) of the newest release, pre-releases included by default.

    The ``-legacy`` (old View-based UI) APK is skipped.
    """
    for release in releases:
        if release.get("draft") or (stable_only and release.get("prerelease")):
            continue
        apks = [a for a in release.get("assets", [])
                if a["name"].endswith(".apk") and "legacy" not in a["name"].lower()]
        named = [a for a in apks if a["name"].startswith("Magisk")]
        if named or apks:
            return release.get("tag_name", ""), (named or apks)[0]
    raise BuildError("No Magisk APK found in the latest releases.")


def get_magisk_apk(args: argparse.Namespace, dest_dir: Path) -> tuple[Path, str]:
    """Download the newest Magisk APK; return (path, version tag)."""
    if args.magisk_apk:
        return args.magisk_apk, args.magisk_apk.stem
    releases = fetch_json(MAGISK_API, os.environ.get("GITHUB_TOKEN", ""))
    if not isinstance(releases, list):
        raise BuildError("Unexpected answer from the GitHub releases API.")
    tag, asset = pick_magisk_asset(releases, args.magisk_stable)
    apk = dest_dir / asset["name"]
    download(asset["browser_download_url"], apk)
    log.info("Using Magisk %s (%s)", tag, apk.name)
    return apk, tag or apk.stem


def extract_magisk_tools(apk: Path, dest: Path) -> None:
    """Lay out the Magisk files the way ``boot_patch.sh`` expects them.

    ``magiskboot`` is the x86_64 build (runs on this host); everything that
    ends up inside the ramdisk comes from the arm64-v8a build.  Files only
    older Magisk versions use (``stub.apk``, ``magisk32``) are copied when
    present; the ones every version needs are checked afterwards.
    """
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(apk) as archive:
        names = set(archive.namelist())

        def pull(member: str, name: str) -> None:
            if member not in names:
                return
            target = dest / name
            with archive.open(member) as source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            target.chmod(0o755)

        for member in sorted(names):
            match = re.fullmatch(r"lib/arm64-v8a/lib(.+)\.so", member)
            if match and match.group(1) != "magiskboot":
                pull(member, match.group(1))
        pull("lib/x86_64/libmagiskboot.so", "magiskboot")
        pull("lib/armeabi-v7a/libmagisk.so", "magisk32")
        for asset in ("boot_patch.sh", "util_functions.sh", "stub.apk"):
            pull(f"assets/{asset}", asset)
    missing = [name for name in MAGISK_REQUIRED if not (dest / name).exists()]
    if missing:
        raise BuildError(f"Unexpected Magisk APK layout, missing {', '.join(missing)}.")


def magisk_patch(tools: Path, boot: Path, output: Path, preinit_device: str) -> None:
    """Run Magisk's own ``boot_patch.sh`` on ``boot`` (like the Magisk app)."""
    env = dict(os.environ)
    env.update(MAGISK_DEFAULT_ENV)
    env["PREINITDEVICE"] = preinit_device
    env["PATH"] = os.pathsep.join([str(SHIMS), env["PATH"]])
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for item in tools.iterdir():
            shutil.copy2(item, work / item.name)
        shutil.copy(boot, work / "boot.img")
        try:
            run(["sh", "./boot_patch.sh", "boot.img"], cwd=work, env=env)
        except subprocess.CalledProcessError as err:
            raise BuildError("Magisk's boot_patch.sh failed.") from err
        shutil.copy(work / "new-boot.img", output)


# --------------------------------------------------------------------------
# build subcommand
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Kernel:
    """The kernel that went into the images."""

    ref: str
    commit: str
    release: str
    patches: list[str]
    image: Path


def build_kernel(args: argparse.Namespace, stock: BootImage, nightly: Nightly) -> Kernel:
    """Fetch, patch and build the kernel matching the stock image."""
    ref = args.kernel_ref or f"lineage-{nightly.version}"
    stock_release = stock.kernel_release()
    log.info("Stock kernel release: %s", stock_release)
    with group("Toolchains"):
        bins = prepare_toolchains(args)
    with group("Kernel source"):
        src, commit = sync_kernel(args, ref, stock_release, nightly.timestamp)
        apply_battery_fix(src)
        patches = apply_extra_patches(src)
    with group("Kernel build"):
        stock_config = None if args.defconfig else stock.embedded_config()
        defconfig = args.defconfig or (None if stock_config else find_defconfig(src))
        target = args.kernel_target or KERNEL_TARGETS[stock.kernel_format()]
        builder = KernelBuilder(src, args.work_dir / "out", args, bins)
        image, release = builder.build(defconfig, stock_config, target, stock_release, commit)
    return Kernel(ref, commit, release, patches, image)


def release_notes(nightly: Nightly, kernel: Kernel, magisk: str,
                  args: argparse.Namespace, images: list[Path]) -> str:
    """Render the Markdown body of the GitHub release."""
    commit_url = f"{args.kernel_url}/commit/{kernel.commit}"
    lines = [
        f"Boot images for **{nightly.zip_name}** (LineageOS {nightly.version}).",
        "",
        "| | |",
        "| --- | --- |",
        f"| Kernel | [`{kernel.commit[:12]}`]({commit_url}) on `{kernel.ref}` |",
        f"| Kernel release | `{kernel.release}` |",
        "| Battery fix | `batterysecret_support = false` in `qpnp-qg.c` |",
        f"| Extra patches | {', '.join(kernel.patches) or 'none'} |",
        f"| Magisk | {magisk} (pre-init device `{args.preinit_device}`) |",
        "",
        "Only flash these on the LineageOS build named above.",
        "",
        "```",
    ]
    lines += [f"{sha256_of(image)}  {image.name}" for image in images]
    lines += ["```", ""]
    return "\n".join(lines)


def cmd_build(args: argparse.Namespace) -> None:
    """``build`` subcommand: the whole pipeline."""
    nightly = Nightly.load(args.nightly)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    plain = args.output_dir / f"boot_{nightly.date}.img"
    rooted = args.output_dir / f"boot-magisk_{nightly.date}.img"

    with tempfile.TemporaryDirectory(dir=args.work_dir) as tmp:
        tmp_dir = Path(tmp)
        with group("Magisk"):
            apk, magisk = get_magisk_apk(args, tmp_dir)
            tools = tmp_dir / "magisk"
            extract_magisk_tools(apk, tools)
        with group("Stock boot.img"):
            stock = BootImage(tools / "magiskboot", tmp_dir / "unpacked")
            stock.unpack(obtain_stock_boot(nightly, tmp_dir))
        kernel = build_kernel(args, stock, nightly)
        with group("Boot images"):
            stock.swap_kernel(kernel.image)
            stock.ensure_gzip_ramdisk()
            stock.repack(plain)
            magisk_patch(tools, plain, rooted, args.preinit_device)

    notes = args.output_dir / "release-notes.md"
    notes.write_text(release_notes(nightly, kernel, magisk, args, [plain, rooted]),
                     encoding="utf-8")
    log.info("Wrote %s, %s and %s", plain.name, rooted.name, notes.name)
    set_outputs(boot=str(plain), boot_magisk=str(rooted), notes=str(notes))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    latest = sub.add_parser("latest", help="resolve the newest LineageOS nightly")
    latest.add_argument("--device", default=DEFAULT_DEVICE)
    latest.add_argument("--date", default="", help="pick this nightly (yyyymmdd) instead")
    latest.add_argument("--output", type=Path, default=Path("out/nightly.json"))
    latest.set_defaults(func=cmd_latest)

    build = sub.add_parser("build", help="build the boot images for a resolved nightly")
    build.add_argument("--nightly", type=Path, default=Path("out/nightly.json"))
    build.add_argument("--output-dir", type=Path, default=Path("out"))
    build.add_argument("--work-dir", type=Path, default=Path("work"))
    build.add_argument("--toolchain-dir", type=Path, default=Path("work/toolchains"))
    build.add_argument("--kernel-url", default=DEFAULT_KERNEL_URL)
    build.add_argument("--kernel-ref", default="",
                       help="branch to search (default: lineage-<version of the nightly>)")
    build.add_argument("--defconfig", help="use this defconfig instead of the stock config")
    build.add_argument("--kernel-target", help="make target (default: auto-detected)")
    build.add_argument("--kcflags", default="", help="extra KCFLAGS for the kernel build")
    build.add_argument("--clang-ref", default=DEFAULT_CLANG_REF)
    build.add_argument("--clang-dir", default=DEFAULT_CLANG_DIR)
    build.add_argument("--gcc-ref", default=DEFAULT_GCC_REF)
    build.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    build.add_argument("--preinit-device", default="sda30", help="Magisk PREINITDEVICE")
    build.add_argument("--magisk-stable", action="store_true",
                       help="ignore Magisk pre-releases (betas)")
    build.add_argument("--magisk-apk", type=Path, help="use this Magisk APK")
    build.set_defaults(func=cmd_build)
    args = parser.parse_args(argv)
    # Commands run in other directories (make inside the kernel tree, PATH
    # entries), so every path given on the command line is made absolute.
    for name, value in vars(args).items():
        if isinstance(value, Path):
            setattr(args, name, value.resolve())
    return args


def main() -> int:
    """Entry point."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()
    try:
        args.func(args)
    except BuildError as err:
        log.error("ERROR: %s", err)
        return 1
    except subprocess.CalledProcessError as err:
        log.error("ERROR: command failed with exit code %d", err.returncode)
        return 1
    except (OSError, ValueError) as err:
        log.error("ERROR: %s", err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
