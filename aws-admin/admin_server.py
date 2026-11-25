"""
aws-admin/admin_server.py
-------------------------
admin server bridge for opentelemetry demo.
provides a simple REST API to safely control services on AWS:
- POST /set-flag: Updates a feature flag in demo.flagd.json
- POST /restart-service: Restarts an allowed container via docker compose
- GET /: Status and available endpoints
"""

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Tuple

PORT = int(os.getenv("AWS_ADMIN_PORT", "8085"))
SSH_TARGET = os.getenv("SSH_TARGET", "otel-demo")
FLAGS_PATH = os.getenv("FLAGS_PATH", "/home/ubuntu/opentelemetry-demo/src/flagd/demo.flagd.json")
DEMO_DIR = os.getenv("DEMO_DIR", "/home/ubuntu/opentelemetry-demo")

# stateless services allowed to be restarted (databases/monitoring excluded for safety)
RESTARTABLE = frozenset({
    "frontend", "cart", "checkout", "payment", "email", "shipping",
    "currency", "ad", "product-catalog", "recommendation", "quote", "flagd",
})

# script executed on the remote AWS instance to validate and update the flag file
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


def run_ssh(args: list, input_text: Optional[str] = None, timeout: int = 30) -> subprocess.CompletedProcess:
    """helper to run commands on the remote server over ssh"""
    cmd = ["ssh"]
    key = os.getenv("SSH_KEY")
    if key:
        cmd.extend(["-i", key, "-o", "StrictHostKeyChecking=accept-new"])
    cmd.extend([SSH_TARGET] + args)
    return subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout)


def set_flag(flag: str, variant: str) -> Dict[str, Any]:
    """validates and changes a feature flag on the remote instance"""
    if not flag or not variant:
        return {"success": False, "error": "flag and variant are required"}

    try:
        proc = run_ssh(["python3", "-", FLAGS_PATH, flag, variant], input_text=_REMOTE, timeout=30)
    except Exception as e:
        return {"success": False, "error": f"ssh to {SSH_TARGET} failed: {e}"}

    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return {"success": False, "error": f"remote write failed rc={proc.returncode}: {(proc.stderr or out)[:200]}"}

    try:
        return json.loads(out.splitlines()[-1])
    except Exception:
        return {"success": False, "error": f"unparseable remote response: {out[:200]}"}


def restart_service(service: str) -> Dict[str, Any]:
    """restarts a container service on the remote instance if it is in the allowlist."""
    if service not in RESTARTABLE:
        return {
            "success": False,
            "error": f"service '{service}' is not on the restart allowlist (stateless apps only)",
        }

    try:
        script = f"cd {DEMO_DIR} && docker compose restart {service}\n"
        proc = run_ssh(["sh"], input_text=script, timeout=120)
    except Exception as e:
        return {"success": False, "error": f"ssh to {SSH_TARGET} failed: {e}"}

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {"success": False, "error": f"remote restart failed rc={proc.returncode}: {(err or out)[:200]}"}

    detail = (err or out).splitlines()[-1][:160] if (err or out) else "restarted"
    return {"success": True, "service": service, "detail": detail}


class Handler(BaseHTTPRequestHandler):
    """http request handler for the admin bridge API."""

    def _send_json(self, data: Dict[str, Any], status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Tuple[Dict[str, Any], Optional[str]]:
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}"), None
        except Exception as e:
            return {}, f"Invalid JSON body: {e}"

    def do_GET(self):
        self._send_json({
            "service": "aws-admin-bridge",
            "endpoints": ["POST /set-flag {flag, variant}", "POST /restart-service {service}"],
        })

    def do_POST(self):
        body, err = self._read_body()
        if err:
            self._send_json({"success": False, "error": err}, status=400)
            return

        if self.path == "/set-flag":
            flag = str(body.get("flag", ""))
            variant = str(body.get("variant", ""))
            result = set_flag(flag, variant)
            print(f"[admin-bridge] set-flag {flag} -> {variant}: {'OK' if result.get('success') else result.get('error')}")
            self._send_json(result)

        elif self.path == "/restart-service":
            service = str(body.get("service", ""))
            result = restart_service(service)
            print(f"[admin-bridge] restart-service {service}: {'OK' if result.get('success') else result.get('error')}")
            self._send_json(result)

        else:
            self._send_json({"success": False, "error": "Not Found"}, status=404)

    def log_message(self, *args):
        # silence default request logging to keep console clean
        pass


if __name__ == "__main__":
    print(f"[admin-bridge] listening on port {PORT} -> ssh://{SSH_TARGET}{FLAGS_PATH}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
