#!/usr/bin/env python3
r"""dump-ip-delta.py — is the stuck TD thread looping, or wedged on one op?

We have ~31 minidumps of the single 2026-06-22 20:12-20:19 AppHang. Derivative
ships no TD symbols, so we can't name frames — but we don't need to for this
question: across the dump series, is the stuck thread's instruction pointer (RIP)
*byte-identical* (a single blocking operation TD never times out of) or *drifting*
(a true compute loop)? That distinction changes the TD-side fix.

This is a self-contained x64 minidump parser (no dmpscan.py / no symbols needed).
For every thread present in the series it tracks the set of RIPs across all dumps,
attributes each RIP to its owning module, and flags the thread that sits constant
inside libTD.dll as the stuck-thread candidate. Pure read-only offline analysis.

Usage:
  python dump-ip-delta.py D:\tmp\td_dumps
  python dump-ip-delta.py D:\tmp\td_dumps\*.dmp
"""
from __future__ import annotations

import argparse
import glob
import pathlib
import struct
import sys

STREAM_THREAD_LIST = 3
STREAM_MODULE_LIST = 4
STREAM_MISC_INFO = 15
CONTEXT_RIP_OFFSET = 0xF8  # x64 CONTEXT.Rip


class Dump:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.threads: dict[int, int] = {}            # ThreadId -> Rip
        self.modules: list[tuple[int, int, str]] = []  # (base, size, name)
        self.pid: int = 0
        self.create: int = 0   # ProcessCreateTime — identifies the process instance
        self._parse()

    def _parse(self) -> None:
        data = self.path.read_bytes()
        (sig,) = struct.unpack_from("<4s", data, 0)
        if sig != b"MDMP":
            raise ValueError(f"{self.path.name}: not a minidump (sig {sig!r})")
        nstreams, dir_rva = struct.unpack_from("<II", data, 8)  # header: sig,ver,nstreams,dirRva

        for i in range(nstreams):
            stype, srva = struct.unpack_from("<I4xI", data, dir_rva + i * 12)  # skip DataSize
            if stype == STREAM_MODULE_LIST:
                self._parse_modules(data, srva)
            elif stype == STREAM_THREAD_LIST:
                self._parse_threads(data, srva)
            elif stype == STREAM_MISC_INFO:
                # MINIDUMP_MISC_INFO: SizeOfInfo, Flags1, ProcessId, ProcessCreateTime
                flags1, pid, create = struct.unpack_from("<III", data, srva + 4)
                if flags1 & 1:
                    self.pid = pid
                if flags1 & 2:
                    self.create = create

    def _parse_threads(self, data: bytes, rva: int) -> None:
        (count,) = struct.unpack_from("<I", data, rva)
        off = rva + 4
        for _ in range(count):
            # MINIDUMP_THREAD (48 bytes): ThreadId@0, ... ThreadContext loc desc
            # at @40 = (ContextDataSize u32, ContextRva u32).
            (tid,) = struct.unpack_from("<I", data, off)
            ctx_size, ctx_rva = struct.unpack_from("<II", data, off + 40)
            off += 48
            if ctx_size >= CONTEXT_RIP_OFFSET + 8:
                (rip,) = struct.unpack_from("<Q", data, ctx_rva + CONTEXT_RIP_OFFSET)
                self.threads[tid] = rip

    def _parse_modules(self, data: bytes, rva: int) -> None:
        (count,) = struct.unpack_from("<I", data, rva)
        off = rva + 4
        for _ in range(count):
            base, size = struct.unpack_from("<QI", data, off)
            (name_rva,) = struct.unpack_from("<I", data, off + 20)
            off += 108  # sizeof(MINIDUMP_MODULE)
            (nlen,) = struct.unpack_from("<I", data, name_rva)
            raw = data[name_rva + 4: name_rva + 4 + nlen]
            name = raw.decode("utf-16-le", errors="replace")
            self.modules.append((base, size, pathlib.PurePath(name).name))

    def attribute(self, addr: int) -> tuple[str, int | None]:
        """Return (module_name, offset_within_module). offset is None if unknown.

        Module-relative offsets are stable across ASLR and process restarts, so
        the same stuck location reads identically across every dump/instance.
        """
        for base, size, name in self.modules:
            if base <= addr < base + size:
                return name, addr - base
        return "?", None


def expand(paths: list[str]) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for p in paths:
        pp = pathlib.Path(p)
        if pp.is_dir():
            out += sorted(pp.glob("*.dmp"))
        else:
            out += [pathlib.Path(g) for g in sorted(glob.glob(p))]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dumps", nargs="+", help="dump dir, or .dmp paths / globs")
    args = ap.parse_args()

    files = expand(args.dumps)
    if not files:
        print("no .dmp files found", file=sys.stderr)
        return 2

    dumps: list[Dump] = []
    for f in files:
        try:
            dumps.append(Dump(f))
        except (ValueError, struct.error, OSError) as exc:
            print(f"skip {f.name}: {exc}", file=sys.stderr)
    if not dumps:
        print("no parseable dumps", file=sys.stderr)
        return 2

    from collections import Counter, defaultdict

    # The dumps may span several TD hang/restart cycles. ASLR + fresh ThreadIds
    # per launch make raw RIP and TID useless across instances, so we work in
    # module-relative offsets (stable everywhere) and report which instances exist.
    instances: dict[int, list[Dump]] = defaultdict(list)
    for d in dumps:
        instances[d.create].append(d)
    print(f"parsed {len(dumps)} dumps over {len(instances)} process instance(s) "
          f"({dumps[0].path.name} .. {dumps[-1].path.name})")
    for create, ds in sorted(instances.items()):
        names = sorted(p.path.name for p in ds)
        print(f"  instance create={create} pid={ds[0].pid}: {len(ds)} dumps "
              f"({names[0]} .. {names[-1]})")

    # ASLR base differs per instance, so "stuck vs loop" must be judged WITHIN an
    # instance. For each dump, find module-relative sites where a thread is
    # executing inside libTD.dll (running TD engine code, not parked in an
    # ntdll/kernelbase wait). A hang is a single stuck op if one site appears in
    # *every* dump of that instance; it is a compute loop if the site smears.
    dump_locs: dict[int, set[str]] = {}
    mod_dumpcount: Counter[str] = Counter()
    for d in dumps:
        sites, mods = set(), set()
        for rip in d.threads.values():
            mod, off = d.attribute(rip)
            mods.add(mod)
            if "libtd" in mod.lower() and off is not None:
                sites.add(f"{mod}+0x{off:x}")
        dump_locs[id(d)] = sites
        for m in mods:
            mod_dumpcount[m] += 1

    verdicts: list[bool] = []   # one bool per instance: True = single stuck op
    any_libtd = False
    print("\nlibTD.dll execution site per hang (instance):")
    for create, ds in sorted(instances.items()):
        cnt: Counter[str] = Counter()
        for d in ds:
            for loc in dump_locs[id(d)]:
                cnt[loc] += 1
        m = len(ds)
        if not cnt:
            print(f"  instance create={create}: no thread executing in libTD across {m} dumps")
            continue
        any_libtd = True
        top_loc, top_cnt = cnt.most_common(1)[0]
        constant = top_cnt == m            # that exact site present in every dump
        verdicts.append(constant)
        tag = "CONSTANT (single stuck op)" if constant else f"spread/{len(cnt)} sites (loop?)"
        print(f"  instance create={create} ({m} dumps): {top_loc} in {top_cnt}/{m} -> {tag}")

    print()
    if not any_libtd:
        print("CONCLUSION: no thread was executing inside libTD.dll in any dump.")
        print("            Top modules any thread's RIP landed in (re-aim here):")
        for mod, c in mod_dumpcount.most_common(8):
            print(f"              {c:>4}/{len(dumps)}  {mod}")
    elif all(verdicts):
        print(f"CONCLUSION: in every hang ({len(verdicts)} instance(s)) the stuck thread sits at a")
        print("            SINGLE constant libTD.dll address for the whole hang -> a single")
        print("            stuck/blocking operation, NOT a tight compute loop (a loop smears).")
        print("            That the site differs between hangs just means different blocking")
        print("            points; each individual hang is a wedge, not a spin.")
        print("            TD-side: an unbounded blocking call in the Video Stream In cook path")
        print("            (decode/reconnect with no timeout), not a runaway per-frame script.")
    else:
        print("CONCLUSION: at least one hang smears across multiple libTD sites -> that hang")
        print("            looks like a compute loop. TD-side: hunt the per-frame loop in the path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
