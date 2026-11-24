# neboot

Boot Cascadia in x86-64 UEFI from a kernel and initramfs on the EFI System Partition.
Choose **1** for Theseus, **2** for OpenRC recovery, or **3** to start a second
EFI application. A missing file produces a firmware status and returns you to
the menu. **Q** returns to firmware.

## AArch64 UEFI

With the Spaceghost Nelua compiler, Clang, and `lld-link` available, build and
stage the AArch64 EFI applications:

```sh
make stage-arm64 NELUA=/path/to/nelua
```

The staged applications are `build/aarch64/esp/EFI/BOOT/BOOTAA64.EFI` and
`build/aarch64/esp/EFI/TOOLS/second.efi`. The ARM build uses Clang’s AArch64
COFF target and links with `lld-link`. Its Linux boot options use the
`ttyAMA0` serial console on QEMU virt.

For a firmware test on Fedora Incus, install `qemu-system-aarch64-core`,
`edk2-aarch64`, `mtools`, `util-linux`, and Python 3. Then run:

```sh
make smoke-arm64 NELUA=/path/to/nelua
```

The menu smoke builds a temporary GPT/FAT disk, boots it with QEMU TCG,
and checks firmware relocation, the missing-kernel diagnostic, and child
chainloading back to the menu. Set `OVMF_CODE` and `OVMF_VARS` to a
matching AArch64 pflash pair when firmware is installed elsewhere.

For a Linux handoff test, supply an AArch64 EFI-stub kernel that supports
uncompressed initramfs archives and the PL011 serial console:

```sh
make smoke-arm64-linux NELUA=/path/to/nelua KERNEL=/path/to/arm64/vmlinuz
```

The target builds a minimal static init, stages it in a separate test ESP,
and checks that Linux executes it as PID 1 and powers off. It passed in
Fedora Incus with Fedora’s `7.3.4-200.fc44.aarch64` kernel. To stage your own
kernel and initramfs, use `make stage-arm64-linux KERNEL=... INITRD=...`.
The ARM Cascadia and Theseus boot path still needs an integration test.


## Run in QEMU

With the toolchain below on PATH and the Cascadia live launcher installed, run:

```sh
make run-live
```

This runs `cascadia-live --prepare` as the current user, extracts the loose
files from the verified ISO, stages the EFI applications and opens the serial
menu. Press **Ctrl+A**, then **X** to close QEMU. To use a particular image,
pass `ISO=/absolute/path/to/cascadia-native-x86_64.iso` to make.

For `cascadia-live --uefi`, install `tools/neboot-live` on PATH. This entry
point fetches the latest neboot into a private temporary directory and runs the
build there. `NEBOOT_TOOLCHAIN_ENV` can point to a Bash file that selects the
local toolchain. All builds and the VM run as the invoking user.

The host needs make, Python 3, xorriso, sfdisk, mtools, QEMU and x86-64 OVMF.
Set `OVMF_CODE` and `OVMF_VARS` to select a matching firmware pair. The
launcher makes a private copy of the variable store and uses a temporary FAT
disk. Use `QEMU_ACCEL=tcg` on a host without KVM.

## Build the applications

`make` writes `build/BOOTX64.EFI` and `build/second.efi` through the
reproducible C emission path. The compiler and linker are selected with
`NELUA`, `NECC` and `NELINKER`.

1. The [Nelua fork](https://github.com/Spaceghost/nelua/commit/d5be57955fe8551934db403e5faed3852536ab7a) emits freestanding C with `ms_abi` on the entry point and firmware function types.
2. [necc](https://github.com/Spaceghost/necc/commit/96de5c958caaf9774ab3b4452b65cdd7812f61dc) compiles that source to x86-64 ELF relocatable objects. Its SDK supplies TinyCC for preprocessing and assembly.
3. [nelinker](https://github.com/Spaceghost/nelinker/commit/55202d63a6e8e3982f36e90c409c272181f6a9de) links the objects as PE32+ EFI applications with `efi_main` as the entry point.

### Native necc backend

`make native` compiles the Nelua sources directly with necc's x86-64 backend
and links the resulting ELF objects into the same EFI applications. It writes
`build/native/BOOTX64.EFI` and `build/native/second.efi`. Use a current full
necc frontend SDK whose Nelua modules understand the `ms_abi` firmware
function types in `src/efi.nelua`, and a nelinker build with PE32+ EFI
support. The verified tool revisions were necc `8bacb93`, Nelua `5995eba5`
and nelinker `0c27372`. The SDK path is selected with `NECC_SYSROOT`.

To stage and boot these outputs, run `make stage-native` or
`make run-live-native`. The latter prepares the live image, stages the EFI
applications, and runs the same OVMF menu audit as `run-live`.

```sh
export NECC_SYSROOT=/path/to/native-necc-sdk
make native NECC=/path/to/necc NELINKER=/path/to/nelinker
cmp build/BOOTX64.EFI build/native/BOOTX64.EFI
cmp build/second.efi build/native/second.efi
```

The loader uses firmware console, filesystem, loaded-image and device-path
protocols. It opens the selected files before starting an image. For the Linux
EFI stub it passes the Cascadia command line and `initrd=\boot\initramfs` in
UTF-16 load options. [UEFI image services](https://uefi.org/specs/UEFI/2.10/07_Services_Boot_Services.html) · [Linux EFI stub](https://docs.kernel.org/admin-guide/efi-stub.html)

## Stage and verify

```sh
make stage ESP=build/esp KERNEL=/path/to/vmlinuz INITRD=/path/to/initramfs
python3 tests/smoke-ovmf.py --esp build/esp --logs build/logs
```

The ESP contains `EFI/BOOT/BOOTX64.EFI`, `EFI/TOOLS/second.efi`, `boot/vmlinuz`
and `boot/initramfs`. Use a kernel built with the x86-64 EFI stub. This proof
uses OVMF with Secure Boot disabled.

## Stage the Core profile

The opt-in Core profile starts the native Theseus service graph directly as
PID 1. It provides no OpenRC recovery entry. Build and stage it with a kernel
and Core developer initramfs:

```sh
make stage-core-native ESP=build/core-esp \
  KERNEL=/path/to/vmlinuz INITRD=/path/to/developer.cpio.gz
python3 tests/smoke-ovmf.py --core --esp build/core-esp --logs build/core-logs
```

The Core smoke test verifies `/proc/1/exe`, foundation and serial-console
state, then sends Theseus PID 1 a poweroff request and checks supervisor exit
and kernel poweroff. `--keep-disk build/core.img` retains its GPT/FAT boot disk.

The Core menu passes `rdinit=/init theseus.pid1=1` and `initrd=\\boot\\initramfs`.
The default `make stage` and `make run-live` profile remains the live menu with
Theseus, OpenRC recovery, and the EFI child application.

The smoke test reads the preferred base and DIR64 fixups from the file. It
compares the preferred base with the address reported by the running loader
through the EFI loaded-image protocol. Both guest boots exercise the relocated
menu banner. One run chainloads the second application and starts Theseus.
The other omits that application, checks the error, and starts OpenRC. Both
runs read PID 1 from the live guest. Add `--check-internet` to test guest HTTPS.

Two more disks omit the kernel and initramfs respectively. On each disk, the
test selects Theseus and OpenRC recovery, checks the missing path and
`EFI_NOT_FOUND`, then chainloads the child application and waits for the menu
to return. The serial transcripts are `missing-kernel.log` and
`missing-initramfs.log` in the log directory.

The [firmware test record](docs/ovmf-proof.txt) identifies the exact EFI files and Cascadia image.
