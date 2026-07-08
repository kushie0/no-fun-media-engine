#!/usr/bin/env python3
r"""dump-stack-scan.py — what is TD's wedged thread actually blocked on?

`dump-ip-delta.py` answered "wedged vs looping" from the top RIP alone. This goes
one level deeper on a single full dump: for the stuck thread it (1) prints the full
x64 register context, (2) does a heuristic stack scan — every 8-byte-aligned word on
the live stack (RSP → stack top) that points into a loaded module is reported as a
probable return address, giving a module-attributed caller chain **without symbols** —
and (3) lists which network / decode / streaming / sync DLLs are even loaded.

The point: distinguish the two hypotheses for the 2026-07-03 libTD wedge —
  * blocking network/decode I/O (ws2_32 / mswsock / avformat / NDI on the stack) vs
  * an internal lock/deadlock (ntdll RtlpWait + a critical section, many threads parked).

Heuristic, not a real unwinder (no TD symbols, no unwind info) — a stack scan over-
reports (stale frames, non-return pointers), so read the *ordered module chain* and the
*set of modules present*, not any single address. Pure read-only offline analysis; mmaps
the dump so it never loads the whole 3.8 GB into RAM.

Usage:
  python dump-stack-scan.py D:\tmp\td_hangwatch\hang_20260703_044139.dmp
  python dump-stack-scan.py <dump> --tid 12345      # scan a specific thread
  python dump-stack-scan.py <dump> --module libTD   # pick stuck thread by RIP module (default: libTD)
"""
from __future__ import annotations

import argparse
import bisect
import mmap
import pathlib
import struct
import sys

STREAM_THREAD_LIST = 3
STREAM_MODULE_LIST = 4

# x64 CONTEXT field offsets
CTX = {
    "Rax": 0x78, "Rcx": 0x80, "Rdx": 0x88, "Rbx": 0x90, "Rsp": 0x98,
    "Rbp": 0xA0, "Rsi": 0xA8, "Rdi": 0xB0, "R8": 0xB8, "R9": 0xC0,
    "R10": 0xC8, "R11": 0xD0, "R12": 0xD8, "R13": 0xE0, "R14": 0xE8,
    "R15": 0xF0, "Rip": 0xF8,
}

# Modules worth calling out if a wedged thread's stack touches them.
INTEREST = {
    "network/socket": ("ws2_32", "mswsock", "wship6", "winhttp", "wininet", "iphlpapi", "dnsapi", "rasadhlp", "nsi"),
    "video-decode":   ("avformat", "avcodec", "avutil", "swscale", "swresample", "mfplat", "mfreadwrite", "mf.dll", "mfcore"),
    "streaming-sdk":  ("processing.ndi", "ndi", "srt", "libsrt", "decklink", "blackmagic", "spout"),
    "gpu/gl":         ("nvcuda", "opengl32", "atio6axx", "amdxc", "nvoglv", "d3d11", "dxgi"),
    "sync/lock":      ("ntdll", "kernelbase", "kernel32", "ucrtbase", "vcruntime"),
}


class Dump:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        f = open(path, "rb")
        self._f = f
        self.mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        self.modules: list[tuple[int, int, str]] = []  # (base, size, name)
        self.threads: list[dict] = []
        self._parse()
        self._mbase = [m[0] for m in sorted(self.modules)]
        self._msorted = sorted(self.modules)

    def _parse(self) -> None:
        mm = self.mm
        if mm[:4] != b"MDMP":
            raise ValueError(f"{self.path.name}: not a minidump")
        nstreams, dir_rva = struct.unpack_from("<II", mm, 8)
        for i in range(nstreams):
            stype, srva = struct.unpack_from("<I4xI", mm, dir_rva + i * 12)
            if stype == STREAM_MODULE_LIST:
                self._parse_modules(srva)
            elif stype == STREAM_THREAD_LIST:
                self._parse_threads(srva)

    def _parse_modules(self, rva: int) -> None:
        mm = self.mm
        (count,) = struct.unpack_from("<I", mm, rva)
        off = rva + 4
        for _ in range(count):
            base, size = struct.unpack_from("<QI", mm, off)
            (name_rva,) = struct.unpack_from("<I", mm, off + 20)
            off += 108
            (nlen,) = struct.unpack_from("<I", mm, name_rva)
            name = mm[name_rva + 4: name_rva + 4 + nlen].decode("utf-16-le", "replace")
            self.modules.append((base, size, pathlib.PurePath(name).name))

    def _parse_threads(self, rva: int) -> None:
        mm = self.mm
        (count,) = struct.unpack_from("<I", mm, rva)
        off = rva + 4
        for _ in range(count):
            tid, = struct.unpack_from("<I", mm, off)
            stack_start, stack_size, stack_rva = struct.unpack_from("<QII", mm, off + 24)
            ctx_size, ctx_rva = struct.unpack_from("<II", mm, off + 40)
            off += 48
            regs = {}
            if ctx_size >= 0x100:
                for name, o in CTX.items():
                    (regs[name],) = struct.unpack_from("<Q", mm, ctx_rva + o)
            self.threads.append({
                "tid": tid, "regs": regs,
                "stack_start": stack_start, "stack_size": stack_size, "stack_rva": stack_rva,
            })

    def attribute(self, addr: int) -> tuple[str, int] | None:
        i = bisect.bisect_right(self._mbase, addr) - 1
        if i < 0:
            return None
        base, size, name = self._msorted[i]
        if base <= addr < base + size:
            return name, addr - base
        return None

    def stack_scan(self, th: dict, limit: int = 4000) -> list[tuple[int, str, int]]:
        """Return [(stack_addr, module, off)] for in-module pointers from RSP upward."""
        rsp = th["regs"].get("Rsp", 0)
        start, size, rva = th["stack_start"], th["stack_size"], th["stack_rva"]
        if not (start <= rsp < start + size):
            return []
        begin = rva + (rsp - start)
        end = rva + size
        hits = []
        pos = begin - (begin % 8)  # 8-byte align
        while pos + 8 <= end and len(hits) < limit:
            (val,) = struct.unpack_from("<Q", self.mm, pos)
            attr = self.attribute(val)
            if attr:
                stack_addr = start + (pos - rva)
                hits.append((stack_addr, attr[0], attr[1]))
            pos += 8
        return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump")
    ap.add_argument("--tid", type=lambda x: int(x, 0), default=None)
    ap.add_argument("--module", default="libtd", help="pick stuck thread by RIP module substring")
    ap.add_argument("--frames", type=int, default=60, help="max stack frames to print")
    args = ap.parse_args()

    d = Dump(pathlib.Path(args.dump))
    print(f"{d.path.name}: {len(d.modules)} modules, {len(d.threads)} threads\n")

    # RIP module histogram across all threads (wait-state breadth)
    from collections import Counter
    rip_mod = Counter()
    target = None
    for th in d.threads:
        rip = th["regs"].get("Rip", 0)
        a = d.attribute(rip)
        modname = a[0] if a else "?"
        rip_mod[modname] += 1
        if args.tid is not None and th["tid"] == args.tid:
            target = th
        elif args.tid is None and target is None and a and args.module.lower() in a[0].lower():
            target = th

    print("threads by RIP module (0% CPU → most are parked in a wait):")
    for mod, c in rip_mod.most_common():
        print(f"  {c:>4}  {mod}")

    if target is None:
        print(f"\nno thread selected (module~='{args.module}', tid={args.tid}). "
              f"Pass --tid or --module.", file=sys.stderr)
        return 2

    r = target["regs"]
    ripa = d.attribute(r.get("Rip", 0))
    rip_s = f"{ripa[0]}+0x{ripa[1]:x}" if ripa else f"0x{r.get('Rip',0):x}"
    print(f"\n=== stuck thread tid={target['tid']}  RIP={rip_s} ===")
    print("registers:")
    for name in ("Rip", "Rsp", "Rbp", "Rcx", "Rdx", "R8", "R9", "Rax", "Rbx"):
        v = r.get(name, 0)
        a = d.attribute(v)
        tag = f"  -> {a[0]}+0x{a[1]:x}" if a else ""
        print(f"  {name:<4} 0x{v:016x}{tag}")

    hits = d.stack_scan(target)
    print(f"\nheuristic stack scan (RSP↑, {len(hits)} in-module pointers; innermost first):")
    # ordered chain, collapse consecutive same-module runs
    chain = []
    for _, mod, off in hits:
        if not chain or chain[-1][0] != mod:
            chain.append([mod, 1, off])
        else:
            chain[-1][1] += 1
    for mod, n, first_off in chain[:args.frames]:
        print(f"  {mod:<20} x{n:<3} (first +0x{first_off:x})")

    present = {m[2].lower() for m in d.modules}
    onstack = {mod.lower() for _, mod, _ in hits}
    print("\ninterest modules — L=loaded, S=on stuck thread's stack:")
    for cat, pats in INTEREST.items():
        rows = []
        for p in pats:
            L = any(p in m for m in present)
            S = any(p in m for m in onstack)
            if L or S:
                rows.append(f"{p}[{'L' if L else '-'}{'S' if S else '-'}]")
        if rows:
            print(f"  {cat:<14} {'  '.join(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
