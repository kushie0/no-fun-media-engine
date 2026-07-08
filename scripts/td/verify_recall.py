#!/usr/bin/env python3
"""Verify FX recall round-trip on Wash: write two distinct FX patterns into
presets rows 4 and 3, invoke the real chopexec3.onValueChange (the exact code
TD runs on a band switch; the trigger parm is unchanged/stock), and confirm the
4 toggle buttons restore to match. Then clear the test cells to leave presets clean."""
from drive import td
import json

STEP1 = r'''
w = op('/project1/UI/Lighting/Groups/Wash/Controls')
pr = w.op('presets')
# distinct patterns in FX cols 3..6:  band4 = fx1,fx3 on ; band3 = fx2,fx4 on
for col, v in zip(range(3,7), [1,0,1,0]):
    pr[4, col] = v
for col, v in zip(range(3,7), [0,1,0,1]):
    pr[3, col] = v
# invoke the real recall for band 4 (val=4), then read the 4 buttons
w.op('chopexec3').module.onValueChange(None, 0, 4, 3)
btns = ['buttonToggle','buttonToggle1','buttonToggle2','buttonToggle3']
after4 = [int(w.op('Fx/'+b).par.Value0.eval()) for b in btns]
# recall band 3
w.op('chopexec3').module.onValueChange(None, 0, 3, 4)
after3 = [int(w.op('Fx/'+b).par.Value0.eval()) for b in btns]
result = {'recall_band4': after4, 'expect4': [1,0,1,0],
          'recall_band3': after3, 'expect3': [0,1,0,1]}
'''

CLEANUP = r'''
w = op('/project1/UI/Lighting/Groups/Wash/Controls')
pr = w.op('presets')
for r in (3,4):
    for col in range(3,7):
        pr[r, col] = ''
for b in ['buttonToggle','buttonToggle1','buttonToggle2','buttonToggle3']:
    w.op('Fx/'+b).par.Value0 = 0
result = 'cleaned test cells + reset buttons'
'''

if __name__ == "__main__":
    res = td(STEP1)
    print(json.dumps(res, indent=2))
    ok = res and res['recall_band4'] == res['expect4'] and res['recall_band3'] == res['expect3']
    print("ROUND-TRIP:", "PASS" if ok else "FAIL")
    print(td(CLEANUP))
