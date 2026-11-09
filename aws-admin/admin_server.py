"""
aws-admin/admin_server.py
-------------------------
authorized remediation bridge for the AWS otel-demo regime. the agent's
set_feature_flag actions route here (AWS_ADMIN_URL, default
http://aws-tunnel:8085 -> POST /set-flag {flag, variant}); this server flips
the flag in the demo's flagd file ON THE INSTANCE over ssh and returns
{success, flag, before, after}.

authorization model: the bridge does NOT trust alert labels (the alert carries
no answer) — it validates the agent-proposed flag/variant against the demo's
own flag schema (flag must exist; variant must exist in that flag's variants
map). unknown flags/variants are rejected.

run: python3 aws-admin/admin_server.py   (env: AWS_ADMIN_PORT=8085,
SSH_TARGET=otel-demo, FLAGS_PATH=/home/ubuntu/opentelemetry-demo/src/flagd/demo.flagd.json)
"""

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.getenv("AWS_ADMIN_PORT", "8085"))
SSH_TARGET = os.getenv("SSH_TARGET", "otel-demo")
FLAGS_PATH = os.getenv("FLAGS_PATH",
                       "/home/ubuntu/opentelemetry-demo/src/flagd/demo.flagd.json")

# the remote python that does the validated write; runs ON the instance so the
# check and the write are one atomic step against the live file
_REMOTE = r"""
import json, sys
path, flag, variant = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    d = json.load(open(path))
except Exception as e:
    print(json.dumps({"success": False, "error": f"unreadable flag file: {e}"})); raise SystemExit
f = (d.get("flags") or {}).get(flag)
if f is None:
    print(json.dumps({"success": False, "error": f"unknown flag '{flag}'"})); raise SystemExit
if variant not in (f.get("variants") or {}):
    print(json.dumps({"success": False,
                      "error": f"unknown variant '{variant}' for flag '{flag}'",
                      "allowed": sorted((f.get("variants") or {}).keys())})); raise SystemExit
before = f.get("defaultVariant")
f["defaultVariant"] = variant
json.dump(d, open(path, "w"), indent=2)
print(json.dumps({"success": True, "flag": flag, "before": before, "after": variant}))
"""


def set_flag(flag: str, variant: str) -> dict:
    if not flag or not variant:
        return {"success": False, "error": "flag and variant are required"}
    try:
        ssh_argv = ["ssh"]
        # in-container bridge (aws-tunnel): no ~/.ssh/config exists there
        # compose passes an explicit key path; the alias 'otel-demo' only
        # resolves when running from a host with a real ssh config
        key = os.getenv("SSH_KEY")
        if key:
            ssh_argv += ["-i", key, "-o", "StrictHostKeyChecking=accept-new"]
        ssh_argv += [SSH_TARGET, "python3", "-", FLAGS_PATH, flag, variant]
        proc = subprocess.run(
            ssh_argv,
            input=_REMOTE, capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        return {"success": False, "error": f"ssh to {SSH_TARGET} failed: {e}"}
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return {"success": False,
                "error": f"remote write failed rc={proc.returncode}: {(proc.stderr or out)[:200]}"}
    try:
        return json.loads(out.splitlines()[-1])
    except Exception:
        return {"success": False, "error": f"unparseable remote response: {out[:200]}"}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/set-flag":
            self.send_response(404); self.end_headers(); return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            body, err = {}, str(e)
            self._json({"success": False, "error": f"bad request body: {err}"}); return
        result = set_flag(str(body.get("flag", "")), str(body.get("variant", "")))
        print(f"[admin-bridge] set-flag {body.get('flag')} -> {body.get('variant')}: "
              f"{'OK' if result.get('success') else result.get('error')}")
        self._json(result)

    def do_GET(self):
        self._json({"service": "aws-admin-bridge", "endpoints": ["POST /set-flag {flag, variant}"]})

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"[admin-bridge] listening :{PORT} -> ssh://{SSH_TARGET}{FLAGS_PATH}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
