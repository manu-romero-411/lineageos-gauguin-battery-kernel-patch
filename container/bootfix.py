#!/usr/bin/env python3
"""Build a boot.img for the Xiaomi gauguin with the qpnp-qg battery fix.

This script runs inside the Docker image defined next to it. It:

1. clones the LineageOS gauguin kernel (shallow, cached),
2. applies the battery fix plus any ``*.patch`` file found in ``/patches``,
3. builds ``Image.gz-dtb`` with the AOSP clang / GCC 4.9 toolchains,
4. swaps the kernel inside a stock LineageOS ``boot.img``,
5. optionally patches the result with Magisk.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path
from urllib.request import Request, urlopen

CACHE = Path("/cache")
OUTPUT = Path("/output")
PATCHES = Path("/patches")
SHIMS = Path("/opt/bootfix/shims")

DEFAULT_KERNEL_URL = "https://github.com/LineageOS/android_kernel_xiaomi_gauguin"
DEFAULT_KERNEL_REF = "lineage-24.0"
GOOGLESOURCE = "https://android.googlesource.com/platform/prebuilts"
DEFAULT_CLANG_REF = "bd96dfe349c962681f0e5388af874c771ef96670"
DEFAULT_CLANG_DIR = "clang-r416183b"
DEFAULT_GCC_REF = "refs/tags/android-12.1.0_r27"
MAGISK_API = "https://api.github.com/repos/topjohnwu/Magisk/releases?per_page=10"

BATTERY_DRIVER = "drivers/power/supply/qcom/qpnp-qg.c"
BATTERY_PATTERN = re.compile(r"(chip->batterysecret_support\s*=\s*)([^;]+);")

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
    """Raised for any expected, user-actionable failure."""


def run(cmd: list, cwd: Path | None = None, env: dict | None = None,
        capture: bool = False) -> str:
    """Run a command, logging it, and fail loudly on a non-zero exit code."""
    words = [str(word) for word in cmd]
    log.info("$ %s", " ".join(words))
    result = subprocess.run(
        words,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout if capture else ""


def download(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest``."""
    log.info("Downloading %s", url)
    request = Request(url, headers={"User-Agent": "gauguin-bootfix"})
    with urlopen(request, timeout=120) as response, dest.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def fetch_toolchain(url: str, dest: Path, strip_dir: bool = False) -> Path:
    """Download and unpack a toolchain tarball into ``dest`` (cached by URL)."""
    marker = dest / ".source-url"
    if marker.exists() and marker.read_text(encoding="utf-8") == url:
        return dest
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    archive = dest.parent / f"{dest.name}.tar.gz"
    download(url, archive)
    run(["tar", "-xzf", archive, "-C", dest] + (["--strip-components=1"] if strip_dir else []))
    archive.unlink()
    marker.write_text(url, encoding="utf-8")
    return dest


def sync_kernel(url: str, ref: str) -> Path:
    """Fetch ``ref`` (branch or commit) of the kernel into the cache, clean."""
    src = CACHE / "kernel"
    if not (src / ".git").exists():
        shutil.rmtree(src, ignore_errors=True)
        src.mkdir(parents=True)
        run(["git", "init", "-q"], cwd=src)
        run(["git", "remote", "add", "origin", url], cwd=src)
    run(["git", "remote", "set-url", "origin", url], cwd=src)
    try:
        run(["git", "fetch", "--depth", "1", "origin", ref], cwd=src)
    except subprocess.CalledProcessError as err:
        raise BuildError(
            f"Could not fetch '{ref}' from {url}. Does that branch exist yet? "
            "Try --kernel-ref lineage-23.2 or a commit hash."
        ) from err
    run(["git", "checkout", "-q", "-f", "FETCH_HEAD"], cwd=src)
    run(["git", "clean", "-qfdx"], cwd=src)
    return src


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
    patched = BATTERY_PATTERN.sub(r"\g<1>false;", text)
    target.write_text(patched, encoding="utf-8")
    log.info("batterysecret_support: %d assignment(s) forced to false", len(matches))
    run(["git", "diff", "--stat"], cwd=src)


def apply_extra_patches(src: Path) -> None:
    """Apply every ``*.patch`` in /patches (sorted) with ``git apply``."""
    for patch in sorted(PATCHES.glob("*.patch")):
        run(["git", "apply", "--check", patch], cwd=src)
        run(["git", "apply", patch], cwd=src)


def find_defconfig(src: Path) -> str:
    """Auto-discover a ``*gauguin*defconfig`` (last-resort fallback)."""
    configs = src / "arch" / "arm64" / "configs"
    found = sorted(p.relative_to(configs).as_posix() for p in configs.rglob("*gauguin*defconfig"))
    if len(found) != 1:
        raise BuildError(
            f"No usable config: the stock kernel embeds none and defconfig candidates "
            f"are {found}. Use --defconfig (e.g. vendor/lito-perf_defconfig)."
        )
    log.info("Using defconfig %s", found[0])
    return found[0]


def choose_config(args: argparse.Namespace, src: Path,
                  image: BootImage) -> tuple[str | None, str | None]:
    """Pick (defconfig, stock_config): explicit defconfig > stock config > autodetect."""
    if args.defconfig:
        return args.defconfig, None
    stock_config = image.embedded_config()
    if stock_config:
        log.info("Using the config embedded in the stock kernel")
        return None, stock_config
    return find_defconfig(src), None


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

    def __init__(self, src: Path, jobs: int, toolchain_bins: list[Path], kcflags: str) -> None:
        self.src = src
        self.extra_vars = [f"KCFLAGS={kcflags}"] if kcflags else []
        self.out = CACHE / "out"
        self.jobs = jobs
        self.env = dict(os.environ)
        self.env["PATH"] = os.pathsep.join([str(p) for p in toolchain_bins] + [self.env["PATH"]])
        self.env["CCACHE_DIR"] = str(CACHE / "ccache")
        self.env["KBUILD_BUILD_USER"] = "bootfix"
        self.env["KBUILD_BUILD_HOST"] = "docker"

    def make(self, *targets: str) -> None:
        """Run make with the shared cross-compilation variables."""
        run(
            ["make", f"-j{self.jobs}", f"O={self.out}", *MAKE_VARS, *self.extra_vars, *targets],
            cwd=self.src,
            env=self.env,
        )

    def pin_release(self, stock_release: str | None) -> None:
        """Make the kernel release string match the stock one.

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
            return
        head = run(["git", "rev-parse", "--short=12", "HEAD"], cwd=self.src, capture=True)
        scm.write_text(f"-g{head.strip()}", encoding="utf-8")
        log.warning(
            "Stock release %r does not extend %r; vermagic will differ from "
            "the stock kernel and vendor modules may refuse to load.",
            stock_release,
            release,
        )

    def configure(self, defconfig: str | None, stock_config: str | None) -> None:
        """Create .config from a defconfig or from the stock kernel's config."""
        config_file = self.out / ".config"
        if defconfig:
            self.make(defconfig)
            config = config_file.read_text(encoding="utf-8")
        else:
            self.out.mkdir(parents=True, exist_ok=True)
            config = stock_config or ""
        config_file.write_text(relax_frame_warn(config), encoding="utf-8")
        self.make("olddefconfig")

    def build(self, defconfig: str | None, stock_config: str | None, target: str,
              stock_release: str | None) -> Path:
        """Configure and build ``target``; return the resulting image path."""
        self.configure(defconfig, stock_config)
        self.pin_release(stock_release)
        self.make(target)
        image = self.out / "arch" / "arm64" / "boot" / target
        if not image.exists():
            raise BuildError(f"Build finished but {image} is missing.")
        return image


class BootImage:
    """Thin wrapper around magiskboot working on an unpacked boot image."""

    def __init__(self, magiskboot: Path, workdir: Path) -> None:
        self.magiskboot = magiskboot
        self.workdir = workdir

    def _mb(self, *args: str) -> None:
        run([self.magiskboot, *args], cwd=self.workdir)

    def unpack(self, source: Path) -> None:
        """Unpack without decompressing anything (components stay untouched)."""
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


def pick_magisk_asset(releases: list[dict], stable_only: bool) -> dict:
    """Return the APK asset of the newest release (pre-releases included by default).

    The ``-legacy`` (old View-based UI) APK is skipped.
    """
    for release in releases:
        if release["draft"] or (stable_only and release["prerelease"]):
            continue
        apks = [a for a in release["assets"]
                if a["name"].endswith(".apk") and "legacy" not in a["name"].lower()]
        named = [a for a in apks if a["name"].startswith("Magisk")]
        if named or apks:
            return (named or apks)[0]
    raise BuildError("No Magisk APK found in the latest releases; use --magisk-apk.")


def resolve_magisk_apk(args: argparse.Namespace) -> Path:
    """Return a Magisk APK: the given one, or the newest release (cached)."""
    if args.magisk_apk:
        return args.magisk_apk
    cache = CACHE / "magisk"
    cache.mkdir(parents=True, exist_ok=True)
    try:
        request = Request(MAGISK_API, headers={"User-Agent": "gauguin-bootfix"})
        with urlopen(request, timeout=60) as response:
            releases = json.load(response)
        asset = pick_magisk_asset(releases, args.magisk_stable)
        apk = cache / asset["name"]
        if not apk.exists():
            download(asset["browser_download_url"], apk)
        log.info("Using Magisk APK %s", apk.name)
        return apk
    except (OSError, KeyError, ValueError) as err:
        cached = sorted(cache.glob("*.apk"))
        if not cached:
            raise BuildError(f"Could not get the Magisk APK ({err}); use --magisk-apk.") from err
        log.warning("Could not query Magisk releases (%s); using %s", err, cached[-1].name)
        return cached[-1]


def extract_magisk_tools(apk: Path, dest: Path) -> None:
    """Lay out the Magisk files the way ``boot_patch.sh`` expects them.

    ``magiskboot`` is the x86_64 build (runs on this host); everything that
    ends up inside the ramdisk comes from the arm64-v8a build.
    """
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    try:
        _extract_magisk_members(apk, dest)
    except KeyError as err:
        raise BuildError(f"Unexpected Magisk APK layout, missing {err}.") from err


def _extract_magisk_members(apk: Path, dest: Path) -> None:
    """Copy the needed members out of the APK (see ``extract_magisk_tools``)."""
    with zipfile.ZipFile(apk) as archive:
        names = set(archive.namelist())

        def pull(member: str, name: str) -> None:
            target = dest / name
            with archive.open(member) as source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            target.chmod(0o755)

        for member in sorted(names):
            match = re.fullmatch(r"lib/arm64-v8a/lib(.+)\.so", member)
            if match and match.group(1) != "magiskboot":
                pull(member, match.group(1))
        pull("lib/x86_64/libmagiskboot.so", "magiskboot")
        if "lib/armeabi-v7a/libmagisk.so" in names:
            pull("lib/armeabi-v7a/libmagisk.so", "magisk32")
        for asset in ("boot_patch.sh", "util_functions.sh", "stub.apk"):
            pull(f"assets/{asset}", asset)


def magisk_patch(tools: Path, boot: Path, output: Path, args: argparse.Namespace) -> None:
    """Run Magisk's own ``boot_patch.sh`` on ``boot`` (like the Magisk app)."""
    env = dict(os.environ)
    env.update(MAGISK_DEFAULT_ENV)
    if args.preinit_device:
        env["PREINITDEVICE"] = args.preinit_device
    for item in args.magisk_env:
        key, _, value = item.partition("=")
        env[key] = value
    env["PATH"] = os.pathsep.join([str(SHIMS), env["PATH"]])
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for item in tools.iterdir():
            shutil.copy2(item, work / item.name)
        shutil.copy(boot, work / "boot.img")
        try:
            run(["sh", "./boot_patch.sh", "boot.img"], cwd=work, env=env)
        except subprocess.CalledProcessError as err:
            raise BuildError(
                "Magisk's boot_patch.sh failed on the host. The kernel-only image "
                "was still written; patch it with the Magisk app instead."
            ) from err
        shutil.copy(work / "new-boot.img", output)


def obtain_base_boot(args: argparse.Namespace, workdir: Path) -> Path:
    """Return a stock LineageOS boot.img (given directly or taken from a zip)."""
    if args.boot:
        return args.boot
    with zipfile.ZipFile(args.zip) as archive:
        names = archive.namelist()
        if "boot.img" in names:
            archive.extract("boot.img", workdir)
            return workdir / "boot.img"
        if "payload.bin" not in names:
            raise BuildError("The zip has neither boot.img nor payload.bin.")
        archive.extract("payload.bin", workdir)
    out_dir = workdir / "payload_out"
    run(["payload-dumper-go", "-p", "boot", "-o", out_dir, workdir / "payload.bin"])
    found = sorted(out_dir.rglob("boot.img"))
    if not found:
        raise BuildError("payload-dumper-go did not produce a boot.img.")
    return found[0]


def prepare_toolchains(args: argparse.Namespace) -> list[Path]:
    """Download (or reuse) clang and GCC 4.9; return their ``bin`` directories."""
    toolchains = CACHE / "toolchains"
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


def compile_kernel(args: argparse.Namespace, image: BootImage, bins: list[Path]) -> Path:
    """Fetch, patch and build the kernel; return the path of the built image."""
    stock_release = image.kernel_release()
    log.info("Stock kernel release: %s", stock_release)
    src = sync_kernel(args.kernel_url, args.kernel_ref)
    apply_battery_fix(src)
    apply_extra_patches(src)
    defconfig, stock_config = choose_config(args, src, image)
    target = args.kernel_target or KERNEL_TARGETS[image.kernel_format()]
    builder = KernelBuilder(src, args.jobs, bins, args.kcflags)
    return builder.build(defconfig, stock_config, target, stock_release)


def build(args: argparse.Namespace) -> None:
    """Whole pipeline."""
    if args.magisk and not args.preinit_device:
        raise BuildError(
            "--magisk needs --preinit-device (boot_patch.sh cannot detect it on a PC). "
            "Get it from the phone: adb shell su -c 'magisk --preinit-device'"
        )
    OUTPUT.mkdir(parents=True, exist_ok=True)
    tools = CACHE / "magisk" / "tools"
    extract_magisk_tools(resolve_magisk_apk(args), tools)
    bins = prepare_toolchains(args)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        base = obtain_base_boot(args, tmp_dir)
        (tmp_dir / "unpacked").mkdir()
        image = BootImage(tools / "magiskboot", tmp_dir / "unpacked")
        image.unpack(base)
        kernel = compile_kernel(args, image, bins)
        shutil.copy(kernel, OUTPUT / kernel.name)

        image.swap_kernel(kernel)
        image.ensure_gzip_ramdisk()
        result = OUTPUT / "boot-batteryfix.img"
        image.repack(result)
        log.info("Wrote %s", result)

        if args.magisk:
            patched = OUTPUT / "boot-batteryfix-magisk.img"
            magisk_patch(tools, result, patched, args)
            log.info("Wrote %s", patched)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--zip", type=Path, help="LineageOS update zip (boot.img or payload.bin)")
    source.add_argument("--boot", type=Path, help="stock LineageOS boot.img")
    parser.add_argument("--kernel-url", default=DEFAULT_KERNEL_URL)
    parser.add_argument("--kernel-ref", default=DEFAULT_KERNEL_REF, help="branch or commit")
    parser.add_argument("--defconfig",
                        help="force this defconfig instead of the stock kernel's own config")
    parser.add_argument("--kernel-target",
                        help="make target; auto-detected from the stock kernel when omitted")
    parser.add_argument("--clang-ref", default=DEFAULT_CLANG_REF)
    parser.add_argument("--clang-dir", default=DEFAULT_CLANG_DIR)
    parser.add_argument("--gcc-ref", default=DEFAULT_GCC_REF)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--kcflags", default="", help="extra KCFLAGS for the kernel build")
    parser.add_argument("--magisk", action="store_true", help="also produce a Magisk-patched image")
    parser.add_argument("--magisk-apk", type=Path, help="use this APK instead of the newest")
    parser.add_argument("--magisk-stable", action="store_true",
                        help="ignore pre-releases (betas) when picking the newest Magisk")
    parser.add_argument("--preinit-device", default="", help="Magisk PREINITDEVICE value")
    parser.add_argument("--magisk-env", action="append", default=[], metavar="KEY=VALUE",
                        help="extra environment for boot_patch.sh (repeatable)")
    return parser.parse_args(argv)


def main() -> int:
    """Entry point."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        build(parse_args())
    except BuildError as err:
        log.error("ERROR: %s", err)
        return 1
    except subprocess.CalledProcessError as err:
        log.error("ERROR: command failed with exit code %d", err.returncode)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
