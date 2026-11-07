"""`lariat hooks` and `lariat service`: wire agents to lariat and keep it running.

Hooks: Claude Code's ~/.claude/settings.json gets Notification and Stop hooks
that run `lariat notify --hook claude ...` (merged in; everything else in the
file is kept, and the first change leaves a backup next to it). Grok Build
gets ~/.grok/hooks/lariat.json.

Service: a systemd user unit (Linux), a launchd agent (macOS) or a Startup
folder entry (Windows) that runs `lariat serve`. The PATH and any LARIAT_*
settings of the shell that installs it are kept, so the service finds the
same tools (tmux, herdr, whisper-server).
"""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "dev.lariat"


def self_command() -> list[str]:
    """How to run this lariat again: the binary itself, or the installed command."""
    if getattr(sys, "frozen", False):  # a single-file release binary
        return [sys.executable]
    found = shutil.which("lariat")
    return [found] if found else [sys.executable, "-m", "lariat.cli"]


def command_line(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv) if sys.platform == "win32" else shlex.join(argv)


# -- agent hooks ----------------------------------------------------------------------


def claude_settings() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "settings.json"


def hook(agent: str, event: str, cmd: list[str] | None = None) -> dict:
    line = command_line([*(cmd or self_command()), "notify", "--hook", agent, event])
    return {"type": "command", "command": line, "async": True, "timeout": 10}


def claude_hooks(cmd: list[str] | None = None) -> dict:
    return {
        "Notification": [{"hooks": [hook("claude", "notification", cmd)]}],
        "Stop": [{"hooks": [hook("claude", "stop", cmd)]}],
    }


def _has_ours(entries: list) -> bool:
    return any("notify --hook claude" in str(h.get("command", "")) for e in entries for h in (e.get("hooks") or []))


MANAGED_HOOKS = Path("/etc/claude-code/managed-settings.d/50-lariat.json")  # NixOS: notify.claudeCode


def claude_hooks_installed(path: Path | None = None) -> bool:
    if path is None and MANAGED_HOOKS.exists():
        return True
    try:
        hooks = json.loads((path or claude_settings()).read_text()).get("hooks") or {}
    except (OSError, ValueError, AttributeError):
        return False
    return all(_has_ours(hooks.get(event) or []) for event in ("Notification", "Stop"))


def uploads_rule(uploads: Path) -> str | None:
    """A Claude Code permission rule that lets agents read images from the phone."""
    if sys.platform == "win32":
        return None
    try:
        return f"Read(~/{uploads.relative_to(Path.home()).as_posix()}/**)"
    except ValueError:
        return f"Read(/{uploads.as_posix()}/**)"  # "//" = an absolute path in Claude Code rules


def install_claude_hooks(path: Path | None = None, cmd: list[str] | None = None, uploads: Path | None = None) -> bool:
    """Merge our hooks (and the read permission for uploads) into Claude Code's
    settings. Returns False if everything was there."""
    path = path or claude_settings()
    text = path.read_text() if path.exists() else ""
    data = json.loads(text) if text.strip() else {}
    hooks = data.setdefault("hooks", {})
    changed = False
    for event, entries in claude_hooks(cmd).items():
        existing = hooks.setdefault(event, [])
        ours = [e for e in existing if _has_ours([e])]
        if ours != entries:  # missing, or pointing at an older lariat (a moved binary, a Nix update)
            existing[:] = [e for e in existing if not _has_ours([e])] + entries
            changed = True
    rule = uploads_rule(uploads) if uploads else None
    if rule:
        allow = data.setdefault("permissions", {}).setdefault("allow", [])
        if rule not in allow:
            allow.append(rule)
            changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = path.with_name(path.name + ".lariat-backup")
        if text and not backup.exists():
            backup.write_text(text)
        path.write_text(json.dumps(data, indent=2) + "\n")
    return changed


def grok_hooks_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".grok" / "hooks" / "lariat.json"


def install_grok_hooks(home: Path | None = None, cmd: list[str] | None = None) -> Path:
    path = grok_hooks_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Grok Build has no permission-prompt event, so only the end of a turn alerts.
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [hook("grok", "stop", cmd)]}]}}, indent=2) + "\n")
    return path


# -- background service ---------------------------------------------------------------------


def service_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.startswith("LARIAT_")}
    env["LARIAT_SUPERVISED"] = "1"  # it is started again after exiting for an update
    if "PATH" in os.environ and sys.platform != "win32":
        env["PATH"] = os.environ["PATH"]
    return env


def systemd_unit(argv: list[str], env: dict[str, str]) -> str:
    lines = [
        "[Unit]",
        "Description=lariat: your coding agents on your phone",
        "After=network-online.target",
        "",
        "[Service]",
        f"ExecStart={command_line(argv)}",
        *[f'Environment="{k}={v}"' for k, v in sorted(env.items())],
        "Restart=on-failure",
        "RestartSec=5",
        "KillMode=control-group",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def launchd_plist(argv: list[str], env: dict[str, str], log: Path) -> bytes:
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": argv,
            "EnvironmentVariables": env,
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
        }
    )


def windows_startup(argv: list[str], env: dict[str, str]) -> str:
    """Runs itself again in a minimized window, then keeps lariat running
    across updates (it exits with 75 to be started again)."""
    sets = [f'set "{k}={v}"' for k, v in sorted(env.items())]
    return "\r\n".join(
        [
            "@echo off",
            'if not "%~1"=="run" (',
            '  start "lariat" /min "%~f0" run',
            "  exit /b",
            ")",
            *sets,
            ":loop",
            command_line(argv),
            "if %errorlevel%==75 goto loop",
            "",
        ]
    )


def service_paths() -> dict[str, Path]:
    home = Path.home()
    if sys.platform == "darwin":
        return {"file": home / "Library" / "LaunchAgents" / f"{LABEL}.plist", "log": home / "Library" / "Logs" / "lariat.log"}
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return {"file": appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "lariat.cmd"}
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return {"file": config / "systemd" / "user" / "lariat.service"}


def service_installed() -> bool:
    return service_paths()["file"].exists()


def _run(cmd: list[str], dry_run: bool, check: bool = True, quiet: bool = False) -> None:
    if not quiet:
        print(f"$ {command_line(cmd)}")
    if not dry_run:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise SystemExit(f"lariat: `{command_line(cmd)}` failed:\n{(r.stderr or r.stdout).strip()}")


def install_service(serve_args: list[str], dry_run: bool = False, quiet: bool = False) -> None:
    argv = [*self_command(), "serve", *serve_args]
    env = service_env()
    paths = service_paths()
    target = paths["file"]
    if sys.platform == "darwin":
        content: bytes | str = launchd_plist(argv, env, paths["log"])
    elif sys.platform == "win32":
        content = windows_startup(argv, env)
    else:
        content = systemd_unit(argv, env)
    if not quiet:
        print(f"# {target}")
        print(content.decode() if isinstance(content, bytes) else content)
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
    if sys.platform == "darwin":
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", domain, str(target)], dry_run, check=False, quiet=quiet)
        _run(["launchctl", "bootstrap", domain, str(target)], dry_run, quiet=quiet)
        if not quiet:
            print(f"Logs: {paths['log']}")
    elif sys.platform == "win32":
        if not quiet:
            print("It starts when you log in; starting it now.")
        if not dry_run:
            os.startfile(target)  # type: ignore[attr-defined]  # Windows only
    else:
        _run(["systemctl", "--user", "daemon-reload"], dry_run, quiet=quiet)
        _run(["systemctl", "--user", "enable", "lariat.service"], dry_run, quiet=quiet)
        _run(["systemctl", "--user", "restart", "lariat.service"], dry_run, quiet=quiet)
        if not quiet:
            print("Logs: journalctl --user -u lariat -f")
            print("To keep it running while you are logged out: loginctl enable-linger $USER")


def uninstall_service(dry_run: bool = False) -> None:
    target = service_paths()["file"]
    if sys.platform == "darwin":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(target)], dry_run, check=False)
    elif sys.platform != "win32":
        _run(["systemctl", "--user", "disable", "--now", "lariat.service"], dry_run, check=False)
    print(f"# remove {target}")
    if not dry_run and target.exists():
        target.unlink()
        if sys.platform not in ("darwin", "win32"):
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
