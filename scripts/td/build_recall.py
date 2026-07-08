#!/usr/bin/env python3
"""Finish per-band FX memory: extend each group's `chopexec3` (recall, fires on
null1['v'] band-switch) so it restores the 4 FX toggles from presets cols 3-6 -
the mirror of `fxsave` (write). Keeps the original brightness recall untouched.
Map (same as fxsave): col3->buttonToggle(fx1), c4->buttonToggle1(fx2),
c5->buttonToggle2(fx3), c6->buttonToggle3(fx4). Reads presets[int(val)] directly
so it's correct regardless of chopexec3/chopexec4 ordering."""
from drive import td

RECALL = "\n".join([
    "# recall: on band switch (null1['v'] change) restore this group's saved state.",
    "# brightness from presets col2 (via select1); FX toggles from presets cols 3-6.",
    "# Pairs with fxsave (write) for per-band FX memory. fx1=c3 fx2=c4 fx3=c5 fx4=c6.",
    "_FXBTN = {3: 'buttonToggle', 4: 'buttonToggle1', 5: 'buttonToggle2', 6: 'buttonToggle3'}",
    "",
    "def onValueChange(channel, sampleIndex, val, prev):",
    "\top('sliderVert').par.Value0 = op('select1')[0, 2]   # brightness (original behaviour)",
    "\tsel = int(val)",
    "\tpr = op('presets')",
    "\tif sel < 0 or sel >= pr.numRows:",
    "\t\treturn",
    "\tfor col, btn in _FXBTN.items():",
    "\t\ts = str(pr[sel, col].val).strip()",
    "\t\top('Fx/' + btn).par.Value0 = 1 if s not in ('', '0', '0.0') else 0",
    "\treturn",
])

BUILD = r'''
script = %r
groups = ['Backlights', 'Focus', 'SideFills', 'Wash']
done = []
for g in groups:
    ctl = op('/project1/UI/Lighting/Groups/' + g + '/Controls')
    if ctl is None or ctl.op('chopexec3') is None:
        done.append(g + ':MISSING'); continue
    ce = ctl.op('chopexec3')
    ce.text = script
    # ensure it still fires on value change (parm unchanged, but assert)
    if hasattr(ce.par, 'valuechange'):
        ce.par.valuechange = True
    done.append(g + ':OK errors=' + repr(ce.errors()))
result = done
''' % RECALL

if __name__ == "__main__":
    import json
    print(json.dumps(td(BUILD), indent=2))
