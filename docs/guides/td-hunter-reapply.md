# Reapplying the HUNTER TD changes to a newer prod `.toe`

**Purpose.** The HUNTER effort adds a set of features to the TouchDesigner lighting
project (`VenueStandard.toe`) *programmatically*, so they can be reapplied to any newer
production version of the file without hand-building in the GUI. This guide is the durable
runbook: given a fresh prod `.toe`, it takes you from "open file" to "all changes applied +
verified + saved."

The changes are driven by re-runnable, **idempotent** Python scripts in
[`scripts/td/`](../../scripts/td/). Each script destroys-and-recreates its own operators, so
running it twice is safe. The scripts operate on **whatever project is open in TD** (via the
bridge), so they don't hard-code a filename and port straight to a new prod version.

> **Safety — never touch the live file.** Always work on a **copy** of the prod `.toe`. The
> live `C:\Users\NOFUNadmin\VenueLighting\VenueStandard.toe` (prod) / the TD engineer's file is
> never edited by this process. Save results under a new name (e.g. `..._HUNTER.toe`).

---

## The mechanism (M3 — MCP bridge)

CLI expand/collapse (`toeexpand`/`toecollapse`) can edit existing operators but **cannot add
new ones** (a hand-authored node is dropped on collapse). Every HUNTER change here *adds*
operators, so they're built through a running TD via the **TouchDesigner MCP bridge**
(`8beeeaaat/touchdesigner-mcp`, MIT): a `WebServer DAT` loaded into the project exposes an HTTP
API on `localhost:9981`. We drive it directly with `curl` — no Claude Code restart, no MCP tool
load needed. [`scripts/td/drive.py`](../../scripts/td/drive.py) is a 20-line client:
`td(script)` POSTs Python to `/api/td/server/exec` and returns the `result`.

Key API endpoints:
- `POST /api/td/server/exec` `{"script": "...; result = ..."}` — run arbitrary Python in TD.
  Inside, op-type classes are **not** bare names — do `import td; td.scriptCHOP`, or clone an
  existing op with `parent.copy(src, name=...)`.
- `POST /api/nodes` `{"parentPath","nodeType","nodeName"}` — create by class-name string
  (e.g. `nodeType:"videostreamoutTOP"`); creating a `scriptCHOP` auto-makes its `_callbacks` DAT.
- `DELETE /api/nodes?nodePath=...` — delete.
- `GET /api/td/server/td` — handshake / liveness.

> **Gotcha:** long string returns (e.g. a DAT's `.text`) are length-capped by the bridge. To
> read a full script body, `toeexpand` the saved `.toe` and read the `.text` file on disk.

> **TD BUILD MUST MATCH OR EXCEED the file's save build.** A newer prod `.toe` may have been
> saved in a newer TD build than your machine has. Opening a newer-build file in an older build
> throws a "Loading files into a build that is older than they were saved in" warning and, on a
> heavy project, **freezes/corrupts on load** — don't proceed past that dialog. As of 2026-06-28
> prod `VenueStandard.toe` was saved in **099 2025.32050**; the dev Mac had **099 2023.12480** and
> could not open it (froze every time). Fix: install a TD build **≥ the file's save build** (they
> install side-by-side; get it from derivative.ca/download). Check the Mac's build with
> `defaults read /Applications/TouchDesigner.app/Contents/Info.plist CFBundleVersion`; the file's
> save build is in that load-warning dialog (or `get_td_info` once the bridge is up).

### One-time bootstrap (the only human GUI step)
1. Get the bridge package: clone `8beeeaaat/touchdesigner-mcp`, use its `touchdesigner-mcp-td/`
   folder (contains `mcp_webserver_base.tox` + `modules/`). A copy is staged at
   `scratch/td/touchdesigner-mcp-td/` (gitignored/wipeable — re-fetch if gone).
2. Open the target `.toe` copy in TouchDesigner.
3. **Drag `mcp_webserver_base.tox` into `/project1`.** In TD, *dragging the `.tox` in **is** the
   import* — there is no import button. It lands as `/project1/mcp_webserver_base` and starts
   the WebServer DAT. Import it from inside the `touchdesigner-mcp-td/` folder and don't move the
   `.tox` out — it loads `modules/` by relative path.
4. Confirm liveness: `curl -s http://localhost:9981/api/td/server/td` → JSON with a TD version.

---

## Structural preconditions (RE-VERIFY on a new prod version first)

The scripts assume the prod project's layout below. A newer prod file *should* still match, but
**check before running** — if the TD engineer renamed/moved things, update the paths in the
scripts. Quick check via the bridge:

```bash
cd scripts/td
python3 drive.py <<'PY'
root = op('/project1')
ok = {}
for g in ['Backlights','Focus','SideFills','Wash']:
    c = op('/project1/UI/Lighting/Groups/%s/Controls' % g)
    ok[g] = bool(c and c.op('presets') and c.op('null1') and c.op('Fx') and
                 c.op('Fx/out1') and c.op('color') and c.op('out1') and
                 c.op('chopexec2') and c.op('chopexec3'))
ok['STREAM_OUT'] = bool(op('/project1/UI/Video/Feed/STREAM_OUT/ndiout1'))
ok['STREAMS_IN'] = bool(op('/project1/UI/Video/Feed/STREAMS_IN/videostreamin1'))
result = ok
PY
```

Expected layout:
- **Lighting groups:** `/project1/UI/Lighting/Groups/{Backlights,Focus,SideFills,Wash}/Controls`.
  Each `Controls` contains:
  - `presets` — Table DAT, 7 columns: **col 0–2 = RGB base**, **col 3–6 = FX state**; one row
    per band. Row index of the active band = `null1['v']`.
  - `null1` — Null CHOP; channel **`v`** = active band-row index (also `num_rows`, `lock`,
    `blackout`).
  - `Fx` — container: `buttonToggle`+`buttonToggle1/2/3` (widget COMPs) → `rename1` → `merge1` →
    `out1`. `Fx/out1` carries 4 channels `fx1_gN … fx4_gN` (the `_gN` suffix is per-group; the
    scripts key off the `fx1..fx4` prefix, so the suffix doesn't matter).
  - `color` — Null CHOP; output channels `r,g,b,i,fx1..4`. **`i` = intensity/brightness.**
  - `out1` — Out CHOP, fed by `color`.
  - `chopexec2` — brightness-save (writes `presets[sel,2]`); used as the clone template for save.
  - `chopexec3` — recall, fires on `null1['v']` change (restores brightness; extended to restore FX).
- **Stream out:** `/project1/UI/Video/Feed/STREAM_OUT` with `ndiout1` (`name=quad`, ← `IN_QUAD`).
- **Streams in:** `/project1/UI/Video/Feed/STREAMS_IN` with `videostreamin1..4` + `logo`/`switch`
  built by the E script.

**Canonical FX map** (save, recall and DSP all agree):

| Button | Fx channel | presets col | Effect |
|---|---|---|---|
| `buttonToggle`  | fx1 | 3 | rainbow (hue — color path, not brightness DSP) |
| `buttonToggle1` | fx2 | 4 | oscillate |
| `buttonToggle2` | fx3 | 5 | strobe |
| `buttonToggle3` | fx4 | 6 | halve |

---

## The changes and run order

Run from `scripts/td/` with the bridge live. Order matters only in that all are independent
except **recall pairs with save** (recall reads the cells save writes).

| # | Script | What it builds | Verifies |
|---|---|---|---|
| 1 | `build_a_save.py` | Per group: `fxsave` CHOP Execute DAT (clone of `chopexec2`) watching `Fx/out1`, writing `presets[sel, 3–6]` on toggle. `sel = int(null1['v'][0])`. | Toggle a button, confirm the right `presets` cell flips. |
| 2 | `build_a_dsp.py` | Per group: `fxdsp` Script CHOP inserted `color → fxdsp → out1`, scaling channel `i` by a toggle-gated envelope: **halve** ×0.5, **oscillate** ×(0.75+0.25·sin, 0.5 Hz → 50–100%), **strobe** ×(0.75+0.25·square, 8 Hz → 50–100%). All other channels pass through. | Sample `out1['i']` over ~1 s with each effect on. |
| 3 | `build_recall.py` | Per group: replaces `chopexec3` so that on a band switch it also restores the 4 toggles from `presets[sel, 3–6]` (keeps original brightness recall). | `verify_recall.py`. |
| 4 | `build_e_selector.py` | `STREAMS_IN/logo` (Text TOP) + `stream_switch` (Switch TOP over cam1-4 + logo) + a `Source` menu par on `STREAMS_IN` (cam1-4 / auto-rotate / logo). Plus `STREAM_OUT/rtsp1` (videostreamout, `rtspserver`:8554, `active=False`) + `RTSP_IN` — the RTSP send-side reference. | Set `Source`, read `stream_switch.par.index`. |

Optional / already covered elsewhere:
- **Extra NDI output** (the feasibility proof): clone `ndiout1 → ndiout3`, set a unique `name`,
  wire ← `IN_QUAD`. One-liner via the bridge; not in a script (was a spike).
- **D — record at 30 fps:** a CLI-only edit (set `cookrate` in the root `.start`), *not* a
  bridge change — see `docs/active/2026-06_td-hunter-full-attempt.md` Phase 1. Reapply with
  `toeexpand`/`toecollapse`, no TD launch.

### Verify + save
```bash
cd scripts/td
python3 build_a_save.py && python3 build_a_dsp.py && python3 build_recall.py && python3 build_e_selector.py
python3 verify_recall.py          # expect ROUND-TRIP: PASS
python3 drive.py <<'PY'
result = {'saved': project.save('/ABSOLUTE/PATH/TO/NEWNAME_HUNTER.toe')}
PY
```

> **Save gotcha (learned the hard way):** `project.save()` onto the **currently-open** path can
> pop a modal overwrite dialog that blocks TD's main thread — the bridge then goes silent
> (`curl` returns HTTP 000) until someone dismisses the dialog in the GUI. **Always save to a
> new filename.** If the bridge does go unresponsive while TD is still running, check the TD
> window for a dialog.

Confirm persistence by expanding the saved file and checking the new operators survive:
`toeexpand NEWNAME_HUNTER.toe` → look under
`…/Lighting/Groups/*/Controls/{fxsave,fxdsp,fxdsp_callbacks}.*` and
`…/Video/Feed/STREAMS_IN/{logo,stream_switch}.*`.

---

## The hardware gate — E's native RTSP

`videostreamoutTOP` exposes `mode=rtspserver` (native RTSP server mode is present), **but its
`videocodec` menu offers only `h264nvgpu` / `h265nvgpu`** — NVIDIA NVENC only, no software or
Apple/AMD codec. So RTSP **cannot encode/serve on a Mac or on the current AMD prod box**; the
`rtsp1` operator is built as a wired, configured *structural reference* (`active=False`) that
will serve once the planned **NVIDIA card** is installed. Until then, the non-NVIDIA lanes are
**NDI out** (works today: `ndiout*`) and **MediaMTX** re-publishing (`test_files/mediamtx/`,
SRT/RTMP in → RTSP out). The selector graph itself is codec-independent and works now.

---

## Related docs
- `docs/active/2026-06_td-hunter-full-attempt.md` — the full effort plan + execution log
  (D/30fps, CLI recon, Open Questions, per-change build results).
- `docs/active/td-ndi-feasibility-test.md` — the CLI-add-vs-bridge feasibility spike.
- Memory: `project_td_toe_editing_methods` (mechanisms), `project_td_hunter_compare`
  (diffing our build vs the engineer's), `project_touchdesigner` (rig context).
