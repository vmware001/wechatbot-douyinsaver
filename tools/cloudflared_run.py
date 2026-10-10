"""Run the cloudflared tunnel for the public labeling page.

Reads the tunnel token from config/cloudflare.json so it never sits in a
.bat file. Runs under _svc_loop.bat like every other component, so crashes
are retried automatically. Console output is English-only.
"""
import json
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def main():
    cfg = json.loads((BASE_DIR / "config" / "cloudflare.json")
                     .read_text(encoding="utf-8"))
    token = cfg.get("tunnel_token")
    if not token:
        print("no tunnel_token in config/cloudflare.json — run the tunnel "
              "setup first", flush=True)
        sys.exit(1)
    print(f"cloudflared tunnel run -> {cfg.get('hostname')}", flush=True)
    sys.exit(subprocess.call([
        str(BASE_DIR / "bin" / "cloudflared.exe"),
        "tunnel", "run", "--token", token]))


if __name__ == "__main__":
    main()
