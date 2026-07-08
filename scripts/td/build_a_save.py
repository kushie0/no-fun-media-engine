#!/usr/bin/env python3
"""A-save: one CHOP Execute DAT per lighting group that writes each FX toggle's
state into that group's own `presets` table (cols 3-6) for the active band row.
Cloned from the group's working `chopexec2` (brightness-save) so the value-change
config is inherited. Mapping: fx1->3 (rainbow), fx2->4 (osc), fx3->5 (strobe), fx4->6 (halve)."""
from drive import td

# The TD-side callback script (tabs = TD house indent).
SAVE_SCRIPT = "\n".join([
    "# A-save: FX toggles -> this group's presets cols 3-6 for the active band row.",
    "# Mirrors chopexec2 (brightness->col2). fx1=rainbow c3, fx2=osc c4, fx3=strobe c5, fx4=halve c6.",
    "_FXCOL = {'fx1': 3, 'fx2': 4, 'fx3': 5, 'fx4': 6}",
    "",
    "def onValueChange(channel, sampleIndex, val, prev):",
    "\tcol = _FXCOL.get(channel.name.split('_')[0])",
    "\tif col is None:",
    "\t\treturn",
    "\tsel = int(op('null1')['v'][0])",
    "\tif sel >= 0:",
    "\t\top('presets')[sel, col] = int(round(val))",
    "\treturn",
])

BUILD = r'''
groups = ['Backlights', 'Focus', 'SideFills', 'Wash']
script = %r
made = []
for g in groups:
    ctl = op('/project1/UI/Lighting/Groups/' + g + '/Controls')
    if ctl is None:
        made.append({'g': g, 'status': 'NO_CONTROLS'}); continue
    tmpl = ctl.op('chopexec2')
    fx = ctl.op('Fx')
    if tmpl is None or fx is None or fx.op('out1') is None or ctl.op('null1') is None or ctl.op('presets') is None:
        made.append({'g': g, 'status': 'MISSING_PARTS',
                     'tmpl': tmpl is not None, 'fxout': fx is not None and fx.op('out1') is not None,
                     'null1': ctl.op('null1') is not None, 'presets': ctl.op('presets') is not None})
        continue
    if ctl.op('fxsave'):
        ctl.op('fxsave').destroy()
    d = ctl.copy(tmpl, name='fxsave')
    d.text = script
    d.par.chop = 'Fx/out1'
    d.par.channel = '*'
    # ensure value-change fires; silence the others
    for pn, pv in [('valuechange', True), ('offtoon', False), ('ontooff', False),
                   ('whileon', False), ('whileoff', False), ('active', True)]:
        if hasattr(d.par, pn):
            setattr(getattr(d.par, pn), 'val', pv)
    d.nodeX = tmpl.nodeX + 200
    d.nodeY = tmpl.nodeY
    made.append({'g': g, 'status': 'OK', 'path': d.path,
                 'chop': d.par.chop.eval(), 'valuechange': d.par.valuechange.eval(),
                 'errors': d.errors()})
result = made
''' % SAVE_SCRIPT

if __name__ == "__main__":
    import json
    print(json.dumps(td(BUILD), indent=2))
