# neboot

Boot Cascadia in x86-64 UEFI from a kernel and initramfs on the EFI System Partition.
Choose **1** for Theseus, **2** for OpenRC recovery, or **3** to start a second
EFI application. A missing file produces a firmware status and returns you to
the menu. **Q** returns to firmware.

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

`make` writes `build/BOOTX64.EFI` and `build/second.efi`. The compiler and
linker are selected with `NELUA`, `NECC` and `NELINKER`. Set `NECC_SYSROOT`
to the staged necc SDK if it is installed outside necc's default location.

1. The [Nelua fork](https://github.com/Spaceghost/nelua/commit/d5be57955fe8551934db403e5faed3852536ab7a) emits freestanding C with `ms_abi` on the entry point and firmware function types.
2. [necc](https://github.com/Spaceghost/necc/commit/96de5c958caaf9774ab3b4452b65cdd7812f61dc) compiles that source to x86-64 ELF relocatable objects. Its SDK supplies TinyCC for preprocessing and assembly.
3. [nelinker](https://github.com/Spaceghost/nelinker/commit/55202d63a6e8e3982f36e90c409c272181f6a9de) links the objects as PE32+ EFI applications with `efi_main` as the entry point.

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

The smoke test reads the preferred base and DIR64 fixups from the file. It
compares the preferred base with the address reported by the running loader
through the EFI loaded-image protocol. Both guest boots exercise the relocated
menu banner. One run chainloads the second application and starts Theseus.
The other omits that application, checks the error, and starts OpenRC. Both
runs read PID 1 from the live guest. Add `--check-internet` to test guest HTTPS.
 The [firmware test record](docs/ovmf-proof.txt) identifies the exact EFI files and Cascadia image.
