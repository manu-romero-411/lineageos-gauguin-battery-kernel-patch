# gauguin-bootfix

Weekly-rebuilt `boot.img` files for the **Xiaomi Mi 10T Lite / Redmi Note 9 Pro 5G
(gauguin)** on **LineageOS**, with a kernel patched against the frozen battery
percentage (`Charge counter: 0`) seen with non-original batteries
([LineageOS/issues#87](https://github.com/LineageOS/issues/issues/87),
[#290](https://github.com/LineageOS/issues/issues/290)).

Everything is done by a GitHub Actions workflow: every Thursday it takes the newest
official LineageOS nightly for gauguin, rebuilds its kernel with the fix and publishes
two images as a release:

| File | Content |
| --- | --- |
| `boot_<yyyymmdd>.img` | Stock LineageOS boot image, kernel swapped for the patched one. |
| `boot-magisk_<yyyymmdd>.img` | The same image, patched with Magisk. |

`<yyyymmdd>` is the date of the LineageOS nightly the images belong to, which is also
the release tag. Only flash an image on that exact nightly.

> **Unofficial and not affiliated with LineageOS.** Flashing a custom `boot.img` is at
> your own risk. Keep the stock `boot.img` of your build as a way back.

## The fix

Xiaomi's fuel gauge driver (`drivers/power/supply/qcom/qpnp-qg.c`) tries to authenticate
a Maxim 1-Wire chip that only OEM batteries carry before loading the battery profile.
With a non-OEM battery that never succeeds and the reported percentage freezes. The
patch forces `chip->batterysecret_support = false`, so every battery goes through the
regular profile lookup by battery ID resistance.

The build **fails loudly** if the assignment is not found, instead of publishing an
unpatched kernel. The fix itself comes from
[pocarropa/gauguin-kernel-builder-LOS](https://github.com/pocarropa/gauguin-kernel-builder-LOS).

It only helps if your problem is the one above. With an original battery you do not need it.

## How the images are built

1. The [LineageOS download API](https://download.lineageos.org/devices/gauguin) gives the
   newest nightly. Its `boot.img` is downloaded (or extracted from the update zip /
   `payload.bin` when not published separately) and its SHA-256 checked.
2. The kernel source is `LineageOS/android_kernel_xiaomi_gauguin`, branch
   `lineage-<version of the nightly>`, at **the commit the nightly was built from**: the
   `-g<hash>` at the end of the stock kernel release identifies it. Failing that, the
   last commit before the nightly's build time is used.
3. The battery fix and any `patches/*.patch` (sorted, via `git apply`) are applied.
4. The kernel is built with the AOSP clang (r416183b) and GCC 4.9 toolchains using the
   **config embedded in the stock kernel** (IKCONFIG), so it matches the build LineageOS
   shipped. The kernel release string is pinned to the stock one so vendor modules keep
   loading (vermagic).
5. `magiskboot` swaps the kernel inside the stock `boot.img`; the dtb, ramdisk and
   everything else are kept → `boot_<date>.img`.
6. Magisk's own `boot_patch.sh` patches a copy → `boot-magisk_<date>.img`. The newest
   Magisk release is used, **pre-releases (betas) included**.

Everything else in the images is upstream LineageOS.

## Releases

- The workflow runs on Thursdays at 05:17 UTC. If a release for the newest nightly
  already exists, it does nothing.
- It can be started by hand from the **Actions** tab (*Build boot images → Run
  workflow*), optionally with a specific nightly date (`yyyymmdd`, must still be listed
  by LineageOS) and *force* to rebuild and replace an existing release.
- The release notes list the LineageOS build, kernel commit, kernel release, Magisk
  version and the SHA-256 of both images.

### Magisk pre-init device

`boot_patch.sh` cannot detect Magisk's pre-init partition off the phone, so it is set
in the workflow. The default is `sda30`. To change it, add a repository variable
(*Settings → Secrets and variables → Actions → Variables*) named
`MAGISK_PREINIT_DEVICE`. Get the right value from the phone (root):

```sh
adb shell su -c 'magisk --preinit-device'
```

Keep the Magisk app on the phone at the same version as the one in the image (shown in
the release notes).

## Flashing

Test first with a temporary boot, which does not touch the partition:

```sh
adb reboot bootloader
fastboot boot boot-magisk_<yyyymmdd>.img
```

Check that the kernel is the patched one and the hardware works:

```sh
adb shell cat /proc/version     # should mention bootfix@github
adb shell su -c 'magisk -v'
```

Then flash it permanently:

```sh
fastboot flash boot boot-magisk_<yyyymmdd>.img
```

**After every LineageOS update** the OTA overwrites `boot`: flash the release matching
the new nightly.

## Running the build elsewhere

The workflow only calls `scripts/bootfix.py`, which runs on any Debian/Ubuntu x86_64
machine with the packages from the workflow's *Install build dependencies* step:

```sh
python3 scripts/bootfix.py latest                       # writes out/nightly.json
python3 scripts/bootfix.py build --preinit-device sda30 # writes out/*.img
```

`build --help` lists the remaining options (`--kernel-ref`, `--defconfig`, `--kcflags`,
`--magisk-stable`, `--magisk-apk`, toolchain refs, `--jobs`).

## Notes and limitations

- Only stock kernels in raw `Image` or gzip (`Image.gz-dtb`) format are supported.
- magiskboot's `lz4_legacy` repacking corrupts the ramdisk on this device (boot-time
  kernel panic, as found by pocarropa), so an `lz4_legacy` ramdisk is recompressed as gzip.
- `CONFIG_FRAME_WARN` is set to 0: with `-Werror`, the AOSP clang used here trips over
  stack frame sizes (e.g. `sha3_256_hmac`) that LineageOS's compiler does not.
- Magisk's `getprop` and `dos2unix` are replaced by small shims in `scripts/shims/`.
- Toolchains and ccache are kept in the Actions cache; GitHub drops caches unused for
  7 days, so a weekly run may sometimes start cold (slower, same result).

## Layout

```
.github/workflows/build.yml   weekly build and release
scripts/bootfix.py            the whole pipeline (latest / build)
scripts/shims/                getprop and dos2unix for running boot_patch.sh on a PC
patches/                      extra *.patch files, applied after the battery fix
```
