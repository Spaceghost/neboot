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

PAYLOAD = ("EFI/BOOT/BOOTX64.EFI", "EFI/TOOLS/second.efi", "boot/vmlinuz", "boot/initramfs")


def fat_disk(path, esp, missing_child):
    files = [name for name in PAYLOAD if not (missing_child and name == "EFI/TOOLS/second.efi")]
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

    def send(self, data):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

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


def preferred_image_base(path):
    # Firmware may update ImageBase in its in-memory PE header. Use the
    # original PE32+ file and check that the loader contains a DIR64 fixup.
    image = path.read_bytes()
    pe, = struct.unpack_from("<I", image, 0x3c)
    if image[:2] != b"MZ" or image[pe:pe + 6] != b"PE\0\0\x64\x86":
        raise ValueError(f"{path}: expected an AMD64 PE boot image")
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
    fat_disk(disk, args.esp, recovery)
    with console(args, disk, nvram, code, name) as con:
        report = con.expect(rb"neboot loaded 0x([0-9a-fA-F]+)\r?\n", args.timeout)
        actual = int(report[1], 16)
        if not actual or preferred == actual:
            raise RuntimeError(f"{name}: expected firmware relocation, saw {preferred:#x} -> {actual:#x}")
        con.menu(15)
        con.send(b"3")
        if recovery:
            con.expect(rb"Cannot open \\EFI\\TOOLS\\second\.efi \(EFI status 0x0*800000000000000[eE]\)", 30)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--esp", required=True, type=Path, help="directory populated by make stage")
    parser.add_argument("--logs", type=Path, help="directory for the smoke test's serial transcripts")
    parser.add_argument("--interactive", action="store_true", help="run the menu on this terminal (Ctrl-A X exits QEMU)")
    parser.add_argument("--check-internet", action="store_true", help="verify HTTPS from the running Theseus guest")
    parser.add_argument("--qemu", default=os.environ.get("QEMU", "qemu-system-x86_64"))
    parser.add_argument("--ovmf-code", default=os.environ.get("OVMF_CODE"))
    parser.add_argument("--ovmf-vars", default=os.environ.get("OVMF_VARS"))
    parser.add_argument("--accel", choices=("kvm", "tcg"), default=os.environ.get("QEMU_ACCEL") or
                        ("kvm" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "tcg"))
    parser.add_argument("--timeout", type=int, default=180, help="seconds per firmware or Linux boot")
    args = parser.parse_args()
    if args.interactive and args.check_internet:
        parser.error("For the automated HTTPS check, replace --interactive with --logs DIR --check-internet")
    code, nvram = firmware_paths(args.ovmf_code, args.ovmf_vars)
    if not args.interactive:
        if args.logs is None:
            parser.error("--logs is required for smoke tests")
        args.logs.mkdir(parents=True, exist_ok=True)
    for filename in PAYLOAD:
        with (args.esp / filename).open("rb") as f:
            digest = hashlib.sha256()
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
            print(f"{digest.hexdigest()}  {filename}", flush=True)
    print(f"OVMF {code}, {nvram}, acceleration {args.accel}", flush=True)
    with tempfile.TemporaryDirectory(prefix="neboot-ovmf-") as tmp:
        root = Path(tmp)
        if args.interactive:
            disk = root / "menu.img"
            fat_disk(disk, args.esp, False)
            print("Press Ctrl-A X to exit QEMU.", flush=True)
            subprocess.run(command(args, disk, nvram, code, "interactive"), check=True)
        else:
            preferred = preferred_image_base(args.esp / PAYLOAD[0])
            for recovery in (False, True):
                exercise(args, root, code, nvram, recovery, preferred)


if __name__ == "__main__":
    main()
