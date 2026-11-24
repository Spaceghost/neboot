#!/usr/bin/env python3
"""Write a deterministic newc archive for the AArch64 Linux handoff test."""
from pathlib import Path
import stat
import sys


def write_entry(out, inode, name, mode, data=b"", rdev=(0, 0)):
    path = name.encode("ascii") + b"\0"
    fields = (inode, mode, 0, 0, 1, 0, len(data), 0, 0, *rdev, len(path), 0)
    header = b"070701" + "".join(f"{value:08x}" for value in fields).encode("ascii")
    out.write(header + path)
    out.write(b"\0" * (-(len(header) + len(path)) % 4))
    out.write(data)
    out.write(b"\0" * (-len(data) % 4))


def main():
    init, archive = map(Path, sys.argv[1:])
    with archive.open("wb") as out:
        write_entry(out, 1, "dev", stat.S_IFDIR | 0o755)
        write_entry(out, 2, "dev/console", stat.S_IFCHR | 0o600, rdev=(5, 1))
        write_entry(out, 3, "init", stat.S_IFREG | 0o755, init.read_bytes())
        write_entry(out, 4, "TRAILER!!!", 0)


if __name__ == "__main__":
    main()
