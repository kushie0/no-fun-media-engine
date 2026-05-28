#!/usr/bin/env python3
"""DyERS-style dynamic resonance suppressor — CLI wrapper.

The WOLA core lives in nofun.mastering.dyers_suppress_mono (shared with the master pipeline).
This script applies it to a standalone audio file for offline A/B. numpy only, no librosa/scipy.

  uv run python scripts/dyers_suppress.py in.wav out.wav [--band 1500 6000] [--sensitivity 0.6] ...
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import numpy as np
import soundfile as sf

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from nofun.mastering import dyers_suppress_mono


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('infile')
    ap.add_argument('outfile')
    ap.add_argument('--fft-size', type=int, default=4096, choices=[512, 1024, 2048, 4096, 8192])
    ap.add_argument('--band', nargs=2, type=float, default=[1500.0, 6000.0], metavar=('LO', 'HI'))
    ap.add_argument('--sensitivity', type=float, default=0.6, help='0-1, higher = more peaks')
    ap.add_argument('--sharpness', type=float, default=0.8, help='0-1, higher = narrower notch')
    ap.add_argument('--speed', type=float, default=0.5, help='0-1, higher = faster attack/release')
    ap.add_argument('--resonance-gain', type=float, default=-24.0, metavar='DB')
    ap.add_argument('--max-peaks', type=int, default=4)
    ap.add_argument('--env-bins', type=int, default=65)
    ap.add_argument('--makeup', type=float, default=0.0, metavar='DB')
    ap.add_argument('--wet', type=float, default=1.0, help='0-1 wet/dry mix')
    args = ap.parse_args()

    x, sr = sf.read(args.infile, dtype='float32', always_2d=True)
    band = (args.band[0], args.band[1])
    chans = []
    for c in range(x.shape[1]):
        wet = dyers_suppress_mono(
            x[:, c], sr, fft_size=args.fft_size, sensitivity=args.sensitivity,
            sharpness=args.sharpness, speed=args.speed, resonance_gain_db=args.resonance_gain,
            band=band, max_peaks=args.max_peaks, env_bins=args.env_bins)
        dry = x[:, c]
        mixed = args.wet * wet + (1.0 - args.wet) * dry
        chans.append(mixed * (10.0 ** (args.makeup / 20.0)))
    sf.write(args.outfile, np.stack(chans, axis=1).astype(np.float32), sr)
    print(f'DyERS: {args.infile} -> {args.outfile}  (band {band[0]:.0f}-{band[1]:.0f}Hz, '
          f'sens {args.sensitivity}, sharp {args.sharpness}, res {args.resonance_gain}dB)')


if __name__ == '__main__':
    main()
