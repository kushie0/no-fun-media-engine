#!/usr/bin/env python3
"""A-DSP: per-group Script CHOP `fxdsp` inserted between `color` and `out1` that
scales the intensity channel `i` by an envelope gated on the FX toggles:
  fx4 (halve)    -> x0.5
  fx2 (oscillate)-> x(50..100%) sine @0.5Hz
  fx3 (strobe)   -> x(50..100%) square @8Hz  (low-amplitude, not harsh)
fx1 (rainbow) is a hue effect, out of the brightness DSP scope. All other channels
pass through untouched, so the fx gates still reach downstream."""
from drive import td

CALLBACK = "\n".join([
    "# A-DSP: intensity envelope gated by FX toggles. Passes all channels; scales 'i'.",
    "import math",
    "",
    "def onCook(scriptOp):",
    "\tsrc = scriptOp.inputs[0] if scriptOp.inputs else None",
    "\tscriptOp.clear()",
    "\tif src is None:",
    "\t\treturn",
    "\tdef on(pfx):",
    "\t\tcs = src.chans(pfx + '*')",
    "\t\treturn bool(cs) and cs[0].vals[0] > 0.5",
    "\tt = absTime.seconds",
    "\tm = 1.0",
    "\tif on('fx4'):",
    "\t\tm *= 0.5                                             # halve",
    "\tif on('fx2'):",
    "\t\tm *= 0.75 + 0.25 * math.sin(2 * math.pi * 0.5 * t)  # oscillate 50-100% @0.5Hz",
    "\tif on('fx3'):",
    "\t\tm *= 0.75 + 0.25 * (1.0 if math.sin(2*math.pi*8*t) >= 0 else -1.0)  # strobe 50-100% @8Hz",
    "\tscriptOp.numSamples = 1",
    "\tfor c in src.chans():",
    "\t\tnc = scriptOp.appendChan(c.name)",
    "\t\tnc.vals = [c.vals[0] * m if c.name == 'i' else c.vals[0]]",
    "\treturn",
])

BUILD = r'''
import td
cb = %r
groups = ['Backlights', 'Focus', 'SideFills', 'Wash']
made = []
for g in groups:
    ctl = op('/project1/UI/Lighting/Groups/' + g + '/Controls')
    if ctl is None:
        made.append({'g': g, 'status': 'NO_CONTROLS'}); continue
    color = ctl.op('color'); out1 = ctl.op('out1')
    if color is None or out1 is None:
        made.append({'g': g, 'status': 'MISSING', 'color': color is not None, 'out1': out1 is not None}); continue
    if ctl.op('fxdsp'):
        ctl.op('fxdsp').destroy()
    if ctl.op('fxdsp_callbacks'):
        ctl.op('fxdsp_callbacks').destroy()
    d = ctl.create(td.scriptCHOP, 'fxdsp')
    cbdat = op(d.par.callbacks.eval()) if d.par.callbacks.eval() else ctl.op('fxdsp_callbacks')
    cbdat.text = cb
    # insert: color -> fxdsp -> out1
    d.inputConnectors[0].connect(color)
    out1.inputConnectors[0].connect(d)
    d.nodeX = color.nodeX; d.nodeY = color.nodeY - 160
    made.append({'g': g, 'status': 'OK', 'path': d.path, 'cb': cbdat.path,
                 'out1_inputs': [i.path for i in out1.inputs],
                 'dsp_chans': [c.name for c in d.chans()], 'errors': d.errors()})
result = made
''' % CALLBACK

if __name__ == "__main__":
    import json
    print(json.dumps(td(BUILD), indent=2))
