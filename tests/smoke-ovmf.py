#!/usr/bin/env python3
"""Exercise the firmware menu, external EFI app, and both real Cascadia init systems."""
import argparse
import contextlib
import hashlib
import os
from pathlib import Path
import re
import selectors
import shutil
import struct
import subprocess
import tempfile
import time

CHILD = "EFI/TOOLS/second.efi"
KERNEL = "boot/vmlinuz"
INITRAMFS = "boot/initramfs"
PAYLOAD = ("EFI/BOOT/BOOTX64.EFI", CHILD, KERNEL, INITRAMFS)
ARM64_PAYLOAD = ("EFI/BOOT/BOOTAA64.EFI", CHILD)


def fat_disk(path, esp, omit=None, payload=PAYLOAD):
    files = [name for name in payload if name != omit]
    payload_size = sum((esp / name).stat().st_size for name in files)
    mib = 1024 * 1024
    part_size = max(64, (payload_size + 8 * mib + mib - 1) // mib) * mib
    # The final 1 MiB leaves room for the backup GPT. mformat formats only the ESP.
    with path.open("xb") as f:
        f.truncate(part_size + 2 * mib)
    sectors = part_size // 512
    sfdisk = shutil.which("sfdisk") or shutil.which("sfdisk", path="/usr/sbin:/sbin")
    if not sfdisk:
        raise SystemExit("Install sfdisk (util-linux) to partition the temporary EFI disk.")
    subprocess.run([sfdisk, "--quiet", str(path)],
                   input=f"label: gpt\nstart=2048, size={sectors}, type=U\n".encode(), check=True)
    volume = f"{path}@@{mib}"
    subprocess.run(["mformat", "-i", volume, "-F", "-T", str(sectors), "-H", "2048", "::"], check=True)
    subprocess.run(["mmd", "-i", volume, "::EFI", "::EFI/BOOT", "::EFI/TOOLS", "::boot"], check=True)
    for name in files:
        subprocess.run(["mcopy", "-i", volume, esp / name, "::" + name], check=True)


class Console:
    def __init__(self, proc, log, selector):
        self.proc = proc
        self.log = log
        self.selector = selector
        self.pending = b""

    def expect(self, pattern, seconds):
        limit = time.monotonic() + seconds
        while True:
            match = re.search(pattern, self.pending)
            if match:
                self.pending = self.pending[match.end():]
                return match
            remaining = limit - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise RuntimeError(f"{self.log.name}: timed out waiting for {pattern!r}")
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError(f"{self.log.name}: QEMU exited before {pattern!r}")
            self.log.write(chunk)
            self.log.flush()
            self.pending += chunk

    def wait_exit(self, seconds):
        limit = time.monotonic() + seconds
        while self.proc.poll() is None:
            remaining = limit - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"{self.log.name}: QEMU did not power off")
            if not self.selector.select(remaining):
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                break
            self.log.write(chunk)
            self.log.flush()
        code = self.proc.wait(timeout=max(1, limit - time.monotonic()))
        while self.selector.select(0):
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                break
            self.log.write(chunk)
            self.log.flush()
        return code

    def send(self, data):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def expect_missing(self, path):
        firmware_path = ("\\" + path.replace("/", "\\")).encode("ascii")
        self.expect(b"Cannot open " + re.escape(firmware_path) + rb" \(EFI status 0x0*800000000000000[eE]\)", 30)

    def menu(self, seconds):
        self.expect(rb"Cascadia EFI menu", seconds)
        self.expect(rb"Q  Return to firmware", seconds)


def firmware_paths(code, nvram):
    if code and nvram:
        return Path(code).resolve(strict=True), Path(nvram).resolve(strict=True)
    if code or nvram:
        raise ValueError("supply both OVMF_CODE and OVMF_VARS")
    for base in (Path("/usr/share/OVMF"), Path("/usr/share/edk2/ovmf")):
        for variant in ("_4M.fd", ".fd", "_4M.qcow2"):
            pair = (base / ("OVMF_CODE" + variant), base / ("OVMF_VARS" + variant))
            if all(p.is_file() for p in pair):
                return pair
    raise ValueError("set OVMF_CODE and OVMF_VARS to a matching x86-64 OVMF firmware pair")


def drive(path, interface, readonly=False):
    fmt = "qcow2" if path.suffix == ".qcow2" else "raw"
    filename = str(path).replace(",", ",,")
    return f"if={interface},format={fmt},file={filename}" + (",readonly=on" if readonly else "")


def command(args, disk, vars_template, code, name):
    nvram = disk.parent / (name + vars_template.suffix)
    shutil.copyfile(vars_template, nvram)
    if args.arm64_menu or args.arm64_linux:
        return [args.qemu, "-nodefaults", "-machine", "virt", "-accel", "tcg",
                "-cpu", "cortex-a57", "-m", "1024", "-no-reboot",
                "-display", "none", "-monitor", "none", "-serial", "stdio",
                "-drive", drive(code, "pflash", True), "-drive", drive(nvram, "pflash"),
                "-drive", drive(disk, "none") + ",id=esp",
                "-device", "virtio-blk-pci,drive=esp,bootindex=1"]
    return [args.qemu, "-nodefaults", "-machine", "q35", "-accel", args.accel,
           "-cpu", "host" if args.accel == "kvm" else "max", "-m", "2048" if args.interactive else "1024", "-no-reboot",
           "-display", "none", "-monitor", "none", "-serial", "mon:stdio" if args.interactive else "stdio",
           "-drive", drive(code, "pflash", True), "-drive", drive(nvram, "pflash"),
           "-device", "qemu-xhci,id=xhci", "-drive", drive(disk, "none") + ",id=esp",
           "-device", "usb-storage,bus=xhci.0,drive=esp,removable=on,bootindex=1",
           "-netdev", "user,id=net,ipv6=off", "-device", "e1000,netdev=net,mac=52:54:00:12:34:56"]


@contextlib.contextmanager
def console(args, disk, vars_template, code, name):
    cmd = command(args, disk, vars_template, code, name)
    logfile = args.logs / (name + ".log")
    with logfile.open("wb") as log, selectors.DefaultSelector() as selector:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            selector.register(proc.stdout, selectors.EVENT_READ)
            yield Console(proc, log, selector)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            proc.stdin.close()
            proc.stdout.close()


def preferred_image_base(path, machine=0x8664):
    # Firmware may update ImageBase in its in-memory PE header. Use the
    # original PE32+ file and check that the loader contains a DIR64 fixup.
    image = path.read_bytes()
    pe, = struct.unpack_from("<I", image, 0x3c)
    if image[:2] != b"MZ" or image[pe:pe + 6] != b"PE\0\0" + struct.pack("<H", machine):
        raise ValueError(f"{path}: expected PE machine {machine:#x}")
    count, = struct.unpack_from("<H", image, pe + 6)
    optlen, = struct.unpack_from("<H", image, pe + 20)
    opt = pe + 24
    if (struct.unpack_from("<H", image, opt)[0] != 0x20b or
            struct.unpack_from("<H", image, opt + 68)[0] != 10 or optlen < 160):
        raise ValueError(f"{path}: expected a PE32+ EFI application")
    base, = struct.unpack_from("<Q", image, opt + 24)
    rva, length = struct.unpack_from("<II", image, opt + 152)
    for n in range(count):
        row = opt + optlen + 40 * n
        virtual, raw_size, offset = struct.unpack_from("<III", image, row + 12)
        if not (virtual <= rva and rva + length <= virtual + raw_size):
            continue
        pos = offset + rva - virtual
        end = pos + length
        if end > len(image):
            break
        found = False
        while pos + 8 <= end:
            page, size = struct.unpack_from("<II", image, pos)
            if size < 8 or size % 4 or page % 4096 or pos + size > end:
                raise ValueError(f"{path}: invalid PE relocation block")
            entries = struct.iter_unpack("<H", image[pos + 8:pos + size])
            found |= any(entry >> 12 == 10 for entry, in entries)
            pos += size
        if found and pos == end:
            return base
    raise ValueError(f"{path}: missing PE DIR64 base relocation")


def check_guest_https(con):
    con.send(b"neboot_http=$(curl -4 -fsS --max-time 25 -o /dev/null -w '%{http_code}' https://example.com/); "
             b"""printf '\\nNEBOOT_HTTPS=%s CURL_EXIT=%s\\n' "$neboot_http" "$?"\r""")
    result = con.expect(rb"\r?\nNEBOOT_HTTPS=([0-9]{3}|) CURL_EXIT=([0-9]+)\r?\n", 35)
    http_code = result[1].decode() or "unavailable"
    curl_exit = int(result[2])
    if curl_exit or not http_code.startswith("2"):
        raise RuntimeError(f"guest HTTPS example.com failed (curl exit {curl_exit}, HTTP {http_code}). "
                           f"See {con.log.name}")
    print(f"PASS guest HTTPS example.com status {http_code}", flush=True)


def exercise(args, root, code, nvram, recovery, preferred):
    name = "openrc-missing" if recovery else "theseus-chainload"
    disk = root / (name + ".img")
    fat_disk(disk, args.esp, omit=CHILD if recovery else None)
    with console(args, disk, nvram, code, name) as con:
        report = con.expect(rb"neboot loaded 0x([0-9a-fA-F]+)\r?\n", args.timeout)
        actual = int(report[1], 16)
        if not actual or preferred == actual:
            raise RuntimeError(f"{name}: expected firmware relocation, saw {preferred:#x} -> {actual:#x}")
        con.menu(15)
        con.send(b"3")
        if recovery:
            con.expect_missing(CHILD)
        else:
            con.expect(rb"neboot child EFI application ran", 30)
        con.menu(15)
        con.send(b"2" if recovery else b"1")
        con.expect(rb"Starting \\boot\\vmlinuz", 15)
        con.expect(rb"cascadia# ", args.timeout)
        con.send(b"""printf '\\nPID1=%s\\n' "$(cat /proc/1/comm)"\r""")
        pid1 = b"openrc-init" if recovery else b"theseus-init"
        con.expect(rb"\r?\nPID1=" + pid1 + rb"\r?\n", 30)
        if args.check_internet and not recovery:
            check_guest_https(con)
        print(f"PASS {name}: {preferred:#x} -> {actual:#x}, PID 1 {pid1.decode()}", flush=True)


def exercise_core(args, root, code, nvram, preferred):
    name = "core-positive"
    disk = root / (name + ".img")
    fat_disk(disk, args.esp)
    if args.keep_disk:
        args.keep_disk.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(disk, args.keep_disk)
    with console(args, disk, nvram, code, name) as con:
        report = con.expect(rb"neboot loaded 0x([0-9a-fA-F]+)\r?\n", args.timeout)
        actual = int(report[1], 16)
        if not actual or preferred == actual:
            raise RuntimeError(f"{name}: expected firmware relocation, saw {preferred:#x} -> {actual:#x}")
        con.expect(rb"Cascadia Core EFI menu", 30)
        con.expect(rb"1  Boot Cascadia Core \(Theseus\)", 15)
        con.expect(rb"2  Chainload", 15)
        con.expect(rb"Q  Return to firmware", 15)
        if b"OpenRC" in Path(con.log.name).read_bytes():
            raise RuntimeError(f"{name}: Core menu unexpectedly exposes OpenRC")
        con.send(b"1")
        con.expect(rb"Starting \\boot\\vmlinuz", 30)
        con.expect(rb"[$#] ", args.timeout)
        con.send(b"printf '\\nCORE_PID1='; readlink /proc/1/exe; printf 'FOUNDATION='; the state of foundation; printf 'SERIAL_CONSOLE='; the state of serial-console\r")
        con.expect(rb"CORE_PID1=/bin/theseus-init", 30)
        con.expect(rb"FOUNDATION=RUNNING", 30)
        con.expect(rb"SERIAL_CONSOLE=RUNNING", 30)
        con.send(b"kill -USR2 1\r")
        code = con.wait_exit(args.timeout)
        transcript = con.log.name
        data = Path(transcript).read_bytes()
        if code != 0 or any(marker not in data for marker in (
                b"TI1\tsupervisor-exit\t0\t0", b"TI1\tshutdown-ready", b"reboot: Power down")):
            raise RuntimeError(f"{name}: expected orderly kernel poweroff (QEMU status {code}); see {transcript}")
        print(f"PASS {name}: relocated EFI, Theseus PID 1, foundation and serial-console RUNNING, kernel poweroff", flush=True)


def exercise_missing_boot_file(args, root, code, nvram, name, missing):
    disk = root / (name + ".img")
    fat_disk(disk, args.esp, omit=missing)
    with console(args, disk, nvram, code, name) as con:
        con.menu(args.timeout)
        for key in (b"1", b"2"):
            con.send(key)
            con.expect_missing(missing)
            con.menu(15)

        con.send(b"3")
        con.expect(rb"neboot child EFI application ran", 30)
        con.menu(15)
        print(f"PASS {name}: Theseus and OpenRC reported EFI_NOT_FOUND for /{missing}; "
              "child EFI ran and returned to menu", flush=True)


def exercise_arm64_menu(args, root, code, nvram):
    preferred = preferred_image_base(args.esp / ARM64_PAYLOAD[0], 0xaa64)
    disk = root / "aarch64-menu.img"
    fat_disk(disk, args.esp, payload=ARM64_PAYLOAD)
    if args.keep_disk:
        args.keep_disk.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(disk, args.keep_disk)
    with console(args, disk, nvram, code, "aarch64-menu") as con:
        loaded = con.expect(rb"neboot loaded 0x([0-9a-fA-F]+)\r?\n", args.timeout)
        actual = int(loaded[1], 16)
        if not actual or actual == preferred:
            raise RuntimeError(f"expected EFI relocation, saw {preferred:#x} -> {actual:#x}")
        con.menu(15)
        con.send(b"1")
        con.expect_missing(KERNEL)
        con.menu(15)
        con.send(b"3")
        con.expect(rb"neboot child EFI application ran", 30)
        con.menu(15)
        print(f"PASS AArch64 EFI relocation {preferred:#x} -> {actual:#x}, missing kernel, child and menu return", flush=True)


def exercise_arm64_linux(args, root, code, nvram):
    preferred = preferred_image_base(args.esp / ARM64_PAYLOAD[0], 0xaa64)
    disk = root / "aarch64-linux.img"
    fat_disk(disk, args.esp, payload=ARM64_PAYLOAD + (KERNEL, INITRAMFS))
    if args.keep_disk:
        args.keep_disk.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(disk, args.keep_disk)
    with console(args, disk, nvram, code, "aarch64-linux") as con:
        loaded = con.expect(rb"neboot loaded 0x([0-9a-fA-F]+)\r?\n", args.timeout)
        actual = int(loaded[1], 16)
        if not actual or actual == preferred:
            raise RuntimeError(f"expected EFI relocation, saw {preferred:#x} -> {actual:#x}")
        con.menu(15)
        con.send(b"1")
        con.expect(rb"Starting \\boot\\vmlinuz", 30)
        con.expect(rb"NEBOOT_ARM64_PID1=1\r?\n", args.timeout)
        con.expect(rb"reboot: Power down", args.timeout)
        if con.wait_exit(args.timeout):
            raise RuntimeError("AArch64 Linux guest did not power off cleanly")
        print(f"PASS AArch64 EFI relocation {preferred:#x} -> {actual:#x}, Linux PID 1 and poweroff", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--esp", required=True, type=Path, help="directory populated by make stage")
    parser.add_argument("--logs", type=Path, help="directory for the smoke test's serial transcripts")
    parser.add_argument("--interactive", action="store_true", help="run the menu on this terminal (Ctrl-A X exits QEMU)")
    arm64 = parser.add_mutually_exclusive_group()
    arm64.add_argument("--arm64-menu", action="store_true", help="test the AArch64 EFI menu and chainloader in QEMU TCG")
    arm64.add_argument("--arm64-linux", action="store_true", help="boot an AArch64 EFI-stub kernel with the test initramfs")
    parser.add_argument("--core", action="store_true", help="test the opt-in Core-only EFI menu and orderly shutdown")
    parser.add_argument("--keep-disk", type=Path, help="copy the generated GPT/FAT test disk to this path")
    parser.add_argument("--check-internet", action="store_true", help="verify HTTPS from the running Theseus guest")
    parser.add_argument("--qemu", default=os.environ.get("QEMU"))
    parser.add_argument("--ovmf-code", default=os.environ.get("OVMF_CODE"))
    parser.add_argument("--ovmf-vars", default=os.environ.get("OVMF_VARS"))
    parser.add_argument("--accel", choices=("kvm", "tcg"), default=os.environ.get("QEMU_ACCEL") or
                        ("kvm" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "tcg"))
    parser.add_argument("--timeout", type=int, default=180, help="seconds per firmware or Linux boot")
    args = parser.parse_args()
    if args.interactive and args.check_internet:
        parser.error("For the automated HTTPS check, replace --interactive with --logs DIR --check-internet")
    if args.arm64_menu or args.arm64_linux:
        if args.core or args.interactive or args.check_internet:
            parser.error("AArch64 modes are automated QEMU TCG tests")
        args.qemu = args.qemu or "qemu-system-aarch64"
        args.accel = "tcg"
        if not args.ovmf_code and not args.ovmf_vars:
            base = Path("/usr/share/edk2/aarch64")
            args.ovmf_code = base / "QEMU_EFI-pflash.raw"
            args.ovmf_vars = base / "vars-template-pflash.raw"
        if not args.ovmf_code or not args.ovmf_vars:
            parser.error("supply both AArch64 firmware and vars paths")
        code = Path(args.ovmf_code).resolve(strict=True)
        nvram = Path(args.ovmf_vars).resolve(strict=True)
    else:
        args.qemu = args.qemu or "qemu-system-x86_64"
        code, nvram = firmware_paths(args.ovmf_code, args.ovmf_vars)
    if not args.interactive:
        if args.logs is None:
            parser.error("--logs is required for smoke tests")
        args.logs.mkdir(parents=True, exist_ok=True)
    payload = ARM64_PAYLOAD if args.arm64_menu else PAYLOAD
    if args.arm64_linux:
        payload = ARM64_PAYLOAD + (KERNEL, INITRAMFS)
    for filename in payload:
        with (args.esp / filename).open("rb") as f:
            digest = hashlib.sha256()
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
            print(f"{digest.hexdigest()}  {filename}", flush=True)
    print(f"OVMF {code}, {nvram}, acceleration {args.accel}", flush=True)
    with tempfile.TemporaryDirectory(prefix="neboot-ovmf-") as tmp:
        root = Path(tmp)
        if args.arm64_linux:
            exercise_arm64_linux(args, root, code, nvram)
        elif args.arm64_menu:
            exercise_arm64_menu(args, root, code, nvram)
        elif args.core:
            exercise_core(args, root, code, nvram, preferred_image_base(args.esp / PAYLOAD[0]))
        elif args.interactive:
            disk = root / "menu.img"
            fat_disk(disk, args.esp)
            print("Press Ctrl-A X to exit QEMU.", flush=True)
            subprocess.run(command(args, disk, nvram, code, "interactive"), check=True)
        else:
            preferred = preferred_image_base(args.esp / PAYLOAD[0])
            for recovery in (False, True):
                exercise(args, root, code, nvram, recovery, preferred)
            for name, missing in (("missing-kernel", KERNEL), ("missing-initramfs", INITRAMFS)):
                exercise_missing_boot_file(args, root, code, nvram, name, missing)


if __name__ == "__main__":
    main()
