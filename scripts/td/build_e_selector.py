#!/usr/bin/env python3
"""E (RTSP) — verifiable core: a source SELECTOR for the stream output.
  - `logo` Text TOP (NO FUN placeholder, option 7)
  - `stream_switch` Switch TOP over cam1-4 + logo
  - a custom Menu par `Source` on STREAMS_IN driving the switch index, with an
    auto-rotate mode (option 6) that cycles cams every 2s.
  - `rtsp1` videostreamout (mode=rtspserver, port 8554) wired via an In TOP — the
    RTSP send-side reference. NOTE: videostreamout only exposes NVENC codecs
    (h264nvgpu/h265nvgpu), so it CANNOT cook/serve on this Mac or the AMD prod box;
    it is a structural reference until the planned NVIDIA card lands.
Menu index map: 0-3 = cam1-4, 4 = rotate, 5 = logo(switch input 4)."""
from drive import td

BUILD = r'''
import td
si = op('/project1/UI/Video/Feed/STREAMS_IN')
so = op('/project1/UI/Video/Feed/STREAM_OUT')
log = []

# 1. logo Text TOP
if si.op('logo'): si.op('logo').destroy()
logo = si.create(td.textTOP, 'logo')
logo.par.text = 'NO FUN'
logo.par.fontsizex = 48
try:
    logo.par.resolutionw = 1280; logo.par.resolutionh = 720
except Exception: pass
logo.nodeX = -200; logo.nodeY = -400

# 2. custom Menu par `Source` on STREAMS_IN
pg = None
for p in si.customPages:
    if p.name == 'Stream': pg = p
if pg is None:
    pg = si.appendCustomPage('Stream')
if not hasattr(si.par, 'Source'):
    m = pg.appendMenu('Source', label='Stream Source')
    par = m[0]
else:
    par = si.par.Source
par.menuNames = ['cam1','cam2','cam3','cam4','rotate','logo']
par.menuLabels = ['Cam 1','Cam 2','Cam 3','Cam 4','Auto-rotate','NO FUN logo']

# 3. Switch TOP over cam1-4 + logo
if si.op('stream_switch'): si.op('stream_switch').destroy()
sw = si.create(td.switchTOP, 'stream_switch')
srcs = [si.op('videostreamin1'), si.op('videostreamin2'), si.op('videostreamin3'),
        si.op('videostreamin4'), logo]
for i, s in enumerate(srcs):
    if s is not None:
        sw.inputConnectors[i].connect(s)
sw.nodeX = 100; sw.nodeY = -400
# index expression: menu 0-3 -> that cam; 4 -> rotate 0..3 @2s; 5 -> logo(4)
sw.par.index.expr = ("(lambda s: s if s < 4 else (int(absTime.seconds/2)%4 if s==4 else 4))"
                     "(int(me.parent().par.Source.menuIndex))")

# 4. out null for the selected source
if si.op('stream_out_sel'): si.op('stream_out_sel').destroy()
nz = si.create(td.nullTOP, 'stream_out_sel')
nz.inputConnectors[0].connect(sw)
nz.nodeX = 300; nz.nodeY = -400

# 5. RTSP send-side reference (NVENC-gated; structural only on this box)
if so.op('RTSP_IN'): so.op('RTSP_IN').destroy()
rin = so.create(td.inTOP, 'RTSP_IN')
rin.nodeX = -450; rin.nodeY = -600
if so.op('rtsp1'): so.op('rtsp1').destroy()
vso = so.create(td.videostreamoutTOP, 'rtsp1')
vso.inputConnectors[0].connect(rin)
vso.par.mode = 'rtspserver'
vso.par.port = 8554
vso.par.streamname = 'nofun_quad'
vso.par.active = False   # leave inert: cannot NVENC-encode on this hardware
vso.nodeX = -250; vso.nodeY = -600

result = {
  'logo': logo.path,
  'source_menu': list(si.par.Source.menuNames),
  'switch': sw.path,
  'switch_inputs': [ (i.path if i else None) for i in sw.inputs ],
  'switch_index_expr': sw.par.index.expr,
  'rtsp_out': vso.path, 'rtsp_mode': vso.par.mode.eval(), 'rtsp_port': vso.par.port.eval(),
  'rtsp_codec_options': list(vso.par.videocodec.menuNames),
  'rtsp_errors': vso.errors(),
}
'''

if __name__ == "__main__":
    import json
    # cleanup the earlier probe node first
    td("n=op('/project1/UI/Video/Feed/STREAM_OUT/__vso')\nresult = n.destroy() if n else 'already gone'")
    print(json.dumps(td(BUILD), indent=2))
