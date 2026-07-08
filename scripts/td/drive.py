#!/usr/bin/env python3
"""Tiny driver for the TouchDesigner MCP WebServer bridge (localhost:9981).
Usage: from drive import td;  td(script_string) -> parsed result dict.
Bypasses the MCP layer, talks straight to /api/td/server/exec.
"""
import json, sys, urllib.request

BASE = "http://localhost:9981"

def td(script, timeout=30):
    body = json.dumps({"script": script}).encode()
    req = urllib.request.Request(BASE + "/api/td/server/exec", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)
    if not out.get("success"):
        print("ERROR:", out.get("error"), file=sys.stderr)
        return out
    return out["data"]["result"] if out.get("data") else None

if __name__ == "__main__":
    src = sys.stdin.read() if len(sys.argv) < 2 else open(sys.argv[1]).read()
    print(json.dumps(td(src), indent=2))
