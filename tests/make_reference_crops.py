"""Generate near-lossless MJPEG reference crops for SSIM quality tests.

Run once per machine after adding .mov files to test_files/:
    uv run python tests/make_reference_crops.py
    uv run python tests/make_reference_crops.py --source-dir /path/to/movs

The crops are saved to <source-dir>/reference/ and are gitignored.
After generating, calibrate thresholds with:
    uv run pytest --quality -v -s
Then update QUAD_SSIM_MIN and CLIP_SSIM_MIN in tests/test_quality.py.
"""

import argparse
import pathlib
import subprocess
import sys

# Must match QUAD_FILTER in nofun/video.py exactly so crops align with pipeline output
QUAD_FILTER = (
    "[0:v]scale=out_range=limited:in_range=full,format=yuv420p,split=4[v1][v2][v3][v4];"
    "[v1]crop=iw/2:ih/2:0:0[ul];"
    "[v2]crop=iw/2:ih/2:iw/2:0[ur];"
    "[v3]crop=iw/2:ih/2:0:ih/2[ll];"
    "[v4]crop=iw/2:ih/2:iw/2:ih/2[lr]"
)


def make_crops(source_dir: pathlib.Path) -> None:
    ref_dir = source_dir / 'reference'
    ref_dir.mkdir(exist_ok=True)

    movs = sorted(source_dir.glob('*.mov'))
    if not movs:
        print(f"No .mov files found in {source_dir}")
        sys.exit(1)

    created = skipped = 0
    for mov in movs:
        quads = {q: ref_dir / f'{mov.stem}_{q}.mov' for q in ('UL', 'UR', 'LL', 'LR')}

        if all(p.exists() for p in quads.values()):
            print(f"SKIP    {mov.name}  (all 4 reference crops exist)")
            skipped += 1
            continue

        print(f"CREATE  {mov.name}  → reference crops")
        cmd = [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
            '-i', str(mov),
            '-filter_complex', QUAD_FILTER,
            '-map', '[ul]', '-c:v', 'mjpeg', '-q:v', '2', str(quads['UL']),
            '-map', '[ur]', '-c:v', 'mjpeg', '-q:v', '2', str(quads['UR']),
            '-map', '[ll]', '-c:v', 'mjpeg', '-q:v', '2', str(quads['LL']),
            '-map', '[lr]', '-c:v', 'mjpeg', '-q:v', '2', str(quads['LR']),
        ]
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"ERROR   ffmpeg failed for {mov.name}", file=sys.stderr)
            sys.exit(result.returncode)

        names = '  '.join(p.name for p in quads.values())
        print(f"        {names}")
        created += 1

    print(f"\n{created} created, {skipped} skipped.")
    if created:
        print("Run quality tests with:  uv run pytest --quality -v -s")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--source-dir', type=pathlib.Path,
        default=pathlib.Path(__file__).parent.parent / 'test_files',
        help='Directory containing .mov source files (default: test_files/)',
    )
    make_crops(parser.parse_args().source_dir)
