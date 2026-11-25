"""Install the per-user dashboard and Fini Vids intake services on this Mac."""

import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path

from scc import config
from scc.fini import STATE, baseline_file, folder

cfg = config.load()
FOLDER = folder(cfg)
PREFIX = cfg.get("general", "label_prefix")

root = Path(__file__).resolve().parents[1]
state_bin = STATE / "bin"
state_bin.mkdir(parents=True, exist_ok=True)
if not FOLDER.is_dir():
    raise SystemExit(f"Set fini.folder in config.toml to your editor's export folder ({FOLDER} not found)")
baseline_file(FOLDER, STATE)
subprocess.run(
    ["swiftc", "-O", str(root / "macos/StudioAX.swift"), "-o", str(state_bin / "studio-ax.next")],
    check=True,
)
(state_bin / "studio-ax.next").replace(state_bin / "studio-ax")
agent_dir = Path.home() / "Library/LaunchAgents"
agent_dir.mkdir(exist_ok=True, parents=True)
gui = f"gui/{os.getuid()}"
bin_path = ":".join(
    [
        str(Path.home() / ".local/bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
)
node = shutil.which("node", path=bin_path)
if not node:
    raise SystemExit("Install Node for the authorized macOS background launcher")
bridge = """
const cp=require('node:child_process');
const child=cp.spawn(process.argv[1],process.argv.slice(2),{stdio:'inherit',cwd:process.env.SCC_HOME});
for(const sig of ['SIGTERM','SIGINT']) process.on(sig,()=>child.kill(sig));
child.on('error',e=>{console.error(e.message);process.exit(1)});
child.on('exit',(code)=>process.exit(code??1));
"""
for label, args, options in [
    (
        f"{PREFIX}.social-command-center.dashboard",
        ["-m", "scc", "serve", "--no-open"],
        {"KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 15},
    ),
    (f"{PREFIX}.fini-intake", ["-m", "scc.fini"], {"StartInterval": 60}),
]:
    log = Path.home() / "Library/Logs" / (label + ".log")
    plist = {
        "Label": label,
        "ProgramArguments": [node, "-e", bridge, str(root / ".venv/bin/python"), *args],
        "RunAtLoad": True,
        "EnvironmentVariables": {"PATH": bin_path, "SCC_HOME": str(root)},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        **options,
    }
    target = agent_dir / (label + ".plist")
    subprocess.run(["launchctl", "bootout", f"{gui}/{label}"], check=False, capture_output=True)
    target.write_bytes(plistlib.dumps(plist))
    for attempt in range(12):
        result = subprocess.run(
            ["launchctl", "bootstrap", gui, str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            break
        if attempt == 11:
            raise SystemExit(result.stderr.strip())
        time.sleep(1)
    print(label)
