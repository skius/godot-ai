"""Project-scoped MCP entry point for Godot AI.

A project's `.mcp.json` runs this script from the Godot project root:

    {"mcpServers": {"godot-ai": {"type": "stdio", "command": "python",
                                 "args": ["addons/godot_ai/mcp_launcher.py"]}}}

It resolves this checkout's endpoint ports from `.godot/godot_ai/ports.json`
(allocating a free pair when the editor has not run here yet; the plugin reads
the same file) and runs `godot-ai attach` on them with stdio passed straight
through. Every clone or git worktree therefore reaches only its own editor, and
`.mcp.json` holds no ports or machine paths, so it can be committed.

Keep this compatible with old Pythons (3.9+): it runs on whatever `python`
the MCP client finds on PATH, not in the godot-ai environment.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import zlib
from pathlib import Path

MIN_PORT = 1024
MAX_PORT = 65535
# Same range as client_configurator.gd (PROJECT_PORT_BASE / PROJECT_PORT_SLOTS).
PORT_BASE = 20000
PORT_SLOTS = 10000
PORT_PROBES = 512
PORTS_FILE = Path(".godot") / "godot_ai" / "ports.json"
CREATE_NO_WINDOW = 0x08000000


def fail(message):
    sys.stderr.write("godot-ai launcher: %s\n" % message)
    sys.exit(1)


def project_root():
    # abspath, not resolve(): a junctioned/symlinked addon must still map to
    # the checkout that contains it.
    script_dir = Path(os.path.abspath(__file__)).parent
    for candidate in [Path.cwd()] + list(script_dir.parents):
        if (candidate / "project.godot").is_file():
            return candidate
    fail("no project.godot found above %s" % script_dir)


def normalized(path):
    text = str(path).replace("\\", "/").rstrip("/")
    return text.lower() if os.name == "nt" else text


def read_ports(ports_file, root):
    try:
        data = json.loads(ports_file.read_text(encoding="utf-8"))
        http, ws = int(data["http_port"]), int(data["ws_port"])
        checkout = str(data.get("checkout", ""))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if normalized(checkout) != normalized(root):
        return None
    if not (MIN_PORT <= http <= MAX_PORT and MIN_PORT <= ws <= MAX_PORT) or http == ws:
        return None
    return http, ws


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def allocate_ports(root):
    start = PORT_BASE + (zlib.crc32(normalized(root).encode("utf-8")) % PORT_SLOTS) * 2
    found = []
    for port in range(start, min(start + PORT_PROBES, MAX_PORT + 1)):
        if port_free(port):
            found.append(port)
            if len(found) == 2:
                return found[0], found[1]
    fail("no free port pair between %d and %d" % (start, start + PORT_PROBES))


def create_ports_file(ports_file, root, http, ws):
    """Create the file only if nobody else did; False means another writer won."""
    ports_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(ports_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"http_port": http, "ws_port": ws, "checkout": root.as_posix()}, handle, indent=2)
    return True


def resolve_ports(root):
    ports_file = root / PORTS_FILE
    ports = read_ports(ports_file, root)
    if ports is not None:
        return ports
    if ports_file.exists():
        # Stale or copied from another checkout: replace it.
        ports_file.unlink()
    http, ws = allocate_ports(root)
    if create_ports_file(ports_file, root, http, ws):
        return http, ws
    ports = read_ports(ports_file, root)
    if ports is None:
        fail("could not read %s" % ports_file)
    return ports


def plugin_version():
    cfg = Path(os.path.abspath(__file__)).parent / "plugin.cfg"
    for line in cfg.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "version":
            return value.strip().strip('"')
    fail("no version in %s" % cfg)


def main():
    root = project_root()
    http, ws = resolve_ports(root)
    uvx = shutil.which("uvx")
    if uvx is None:
        fail("uvx not found on PATH; install uv (https://docs.astral.sh/uv/)")
    command = [
        uvx, "--from", "godot-ai==%s" % plugin_version(), "godot-ai", "attach",
        "--port", str(http), "--ws-port", str(ws), "--disable-telemetry",
    ]
    # Explicit std handles: subprocess defaults do not reliably forward pipes to
    # a child on Windows. CREATE_NO_WINDOW keeps uvx from opening a console.
    return subprocess.call(
        command,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


if __name__ == "__main__":
    sys.exit(main())
