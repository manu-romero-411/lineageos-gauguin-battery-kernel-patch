# gauguin-bootfix

Build, inside Docker, a `boot.img` for the **Xiaomi Mi 10T Lite / Redmi Note 9 Pro 5G
(gauguin)** running **LineageOS 24 (Android 17)** with a kernel patched against the
frozen battery percentage (`Charge counter: 0`) seen with non-original batteries
([LineageOS/issues#87](https://github.com/LineageOS/issues/issues/87),
[#290](https://github.com/LineageOS/issues/issues/290)). Optionally, Magisk is patched
into the image too, so you do not have to reinstall it after every update.

> **Unofficial and not affiliated with LineageOS.** Flashing a custom `boot.img` is at
> your own risk. Keep your stock `boot.img` as a way back.

## The fix

Xiaomi's fuel gauge driver (`drivers/power/supply/qcom/qpnp-qg.c`) tries to authenticate
a Maxim 1-Wire chip that only OEM batteries carry before loading the battery profile.
With a non-OEM battery that never succeeds and the reported percentage freezes. The
patch forces `chip->batterysecret_support = false`, so every battery goes through the
regular profile lookup by battery ID resistance.

The patch step **fails loudly** if the assignment is not found, instead of silently
building an unpatched kernel. The fix itself comes from
[pocarropa/gauguin-kernel-builder-LOS](https://github.com/pocarropa/gauguin-kernel-builder-LOS);
this project reuses it and adds the boot image and Magisk handling.

It only helps if your problem is the one above. With an original battery you do not need it.

## What it does

1. Clones (shallow, cached) `LineageOS/android_kernel_xiaomi_gauguin`, branch `lineage-24.0`.
2. Applies the battery fix, then any `patches/*.patch` (sorted, via `git apply`).
3. Builds the kernel with the AOSP clang (r416183b) and GCC 4.9 toolchains, using the
   **kernel config embedded in your stock kernel** (IKCONFIG), so it matches the build
   LineageOS shipped. `--defconfig` forces a defconfig instead.
4. Pins the kernel release string to the stock one (including the `-g<hash>` suffix) so
   vendor modules keep loading (vermagic).
5. Swaps the kernel inside a stock `boot.img` with `magiskboot`; the dtb and everything
   else are kept.
6. With `--magisk`: runs the Magisk APK's own `boot_patch.sh` on the host.

## Requirements

- Docker (or Podman with `--engine podman`) and Python 3 on the host.
- Several GB of free disk for the toolchains and the build cache (`cache/`).
- A **stock, unpatched** LineageOS `boot.img` or the LineageOS update zip.
- For `--magisk`: your device's pre-init partition (see below).

## Usage

```sh
git clone https://github.com/<your-user>/gauguin-bootfix
cd gauguin-bootfix

# Patched kernel only, from a stock boot.img
./build.py --boot boot.img

# From the LineageOS update zip (boot.img or payload.bin inside)
./build.py --zip lineage-24.0-YYYYMMDD-nightly-gauguin-signed.zip

# Patched kernel + Magisk
./build.py --zip lineage-24.0-...-signed.zip --magisk --preinit-device <partition>
```

Output, in `output/`:

| File | Content |
| --- | --- |
| `boot-batteryfix.img` | Patched kernel, no Magisk. Always written. |
| `boot-batteryfix-magisk.img` | Same, patched with Magisk (only with `--magisk`). |
| `Image` (or `Image.gz-dtb`) | The built kernel. |

### Magisk

- By default the **newest release, pre-releases included** is used (the `-legacy` APK is
  skipped). `--magisk-stable` ignores pre-releases; `--magisk-apk <file>` uses a specific
  APK. Use the same version as the Magisk app installed on the phone.
- `boot_patch.sh` cannot detect the pre-init partition on a PC, so `--preinit-device`
  is **required** with `--magisk`. Get it from the phone (root):

  ```sh
  adb shell su -c 'magisk --preinit-device'
  ```

- Extra variables for `boot_patch.sh` can be passed with `--magisk-env KEY=VALUE`
  (defaults: `KEEPVERITY=true`, `KEEPFORCEENCRYPT=true`, `PATCHVBMETAFLAG=false`).
- If the Magisk step fails, `boot-batteryfix.img` is already written: patch it with the
  Magisk app instead (Install → Select and patch a file).

### Flashing

Test first with a temporary boot, which does not touch the partition:

```sh
adb reboot bootloader
fastboot boot output/boot-batteryfix-magisk.img
```

Check that the kernel is yours and the hardware works:

```sh
adb shell cat /proc/version     # should mention bootfix@docker
adb shell su -c 'magisk -v'
```

Then flash it permanently, the way you usually flash your boot image:

```sh
fastboot flash boot output/boot-batteryfix-magisk.img
```

**After every LineageOS update** the OTA overwrites `boot`: run the build again with the
new zip. The cache makes rebuilds incremental.

## Options

Options not listed as host options are forwarded to the container.

| Option | Description |
| --- | --- |
| `--zip`, `--boot` | Stock LineageOS zip or `boot.img` (one is required). |
| `--magisk` | Also produce a Magisk-patched image. |
| `--magisk-apk`, `--magisk-stable` | Pick the Magisk APK / ignore pre-releases. |
| `--preinit-device` | Magisk `PREINITDEVICE` (required with `--magisk`). |
| `--magisk-env KEY=VALUE` | Extra environment for `boot_patch.sh` (repeatable). |
| `--kernel-url`, `--kernel-ref` | Kernel repository and branch or commit (default `lineage-24.0`). |
| `--defconfig` | Use this defconfig instead of the stock kernel's embedded config. |
| `--kernel-target` | `make` target; auto-detected (`Image` or `Image.gz-dtb`). |
| `--kcflags` | Extra `KCFLAGS`, e.g. `--kcflags=-Wno-error`. |
| `--clang-ref`, `--clang-dir`, `--gcc-ref` | Toolchain sources. |
| `--jobs` | Parallel jobs (default: CPU count). |
| `--cache-dir`, `--output-dir`, `--engine` | Host paths and `docker`/`podman`. |

To build exactly the source of your installed build, pin `--kernel-ref` to its commit.
The kernel release of your stock image is printed during the build.

## Notes and limitations

- Only stock kernels in raw `Image` or gzip (`Image.gz-dtb`) format are supported.
- magiskboot's `lz4_legacy` repacking corrupts the ramdisk on this device (boot-time
  kernel panic, as found by pocarropa), so an `lz4_legacy` ramdisk is recompressed as gzip.
- `CONFIG_FRAME_WARN` is set to 0: with `-Werror`, the AOSP clang used here trips over
  stack frame sizes (e.g. `sha3_256_hmac`) that LineageOS's compiler does not.
- Magisk's `getprop` and `dos2unix` are replaced by small shims in `container/shims/`.
- If the kernel lacks an embedded config, use `--defconfig` (for example
  `vendor/lito-perf_defconfig`, a guess for this SoC family; unverified).

## Status

Tested on a Mi 10T Lite with LineageOS 24.0: the patched kernel boots, Wi-Fi works and
Magisk v31.0 runs. Whether it fixes the battery freeze over time is still being
evaluated.

## Layout

```
build.py              host wrapper (builds the image, runs the container)
container/
  Dockerfile
  bootfix.py          the whole pipeline
  shims/getprop       Magisk helpers for running boot_patch.sh on a PC
  shims/dos2unix
patches/              extra *.patch files, applied after the battery fix
```
