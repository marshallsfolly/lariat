"""Gateway configuration, read once from LARIAT_* environment variables.

The NixOS module sets every variable; for local development see README.md.
Paths to secrets are read lazily (they are runtime files, never env values).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from e


def _float(name: str, default: float) -> float:
    value = _env(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as e:
        raise ConfigError(f"{name} must be a number, got {value!r}") from e


def _list(name: str) -> list[str]:
    value = _env(name)
    if value is None:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Config:
    host: str
    origin: str
    bind: str = "127.0.0.1"
    port: int = 8795
    static_dir: Path | None = None
    state_dir: Path = field(default_factory=lambda: Path.home() / ".local/state/lariat")
    runtime_dir: Path = field(default_factory=lambda: Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "lariat")

    session_key_file: Path | None = None
    enroll_token_file: Path | None = None
    credentials_file: Path | None = None
    enrollment: bool = False

    idle_minutes: int = 30
    absolute_minutes: int = 480
    rp_name: str = "lariat"
    user_name: str = "owner"
    app_name: str = "lariat"

    shell_enable: bool = True
    shell_command: list[str] | None = None
    tmux: str | None = None
    tmux_default: str | None = None
    herdr: str | None = None
    herdr_sessions: list[str] = field(default_factory=lambda: ["default"])
    herdr_methods_file: Path | None = None
    herdr_prefix: str = "ctrl+b"
    agent_kinds: list[str] = field(default_factory=list)
    project_roots: list[Path] = field(default_factory=lambda: [Path.home()])

    ntfy_url: str | None = None
    ntfy_topic: str = "agents"

    dictate: bool = False
    whisper: str | None = None  # whisper.cpp's whisper-server
    whisper_model: str | None = None
    ffmpeg: str | None = None  # only for uploaded recordings (browsers without AudioWorklet)
    dictate_language: str = "en"
    dictate_threads: int = 4
    dictate_max_seconds: int = 60
    dictate_idle_minutes: float = 0.0  # stop the model after this long unused; 0 = keep it loaded
    dictate_prompt: str = ""
    dictate_args: list[str] = field(default_factory=list)
    upload_days: float = 7.0  # images from the phone are deleted after this long (0 = keep)
    push: bool = True  # Web Push to devices that turn it on
    tls_cert: Path | None = None  # serve HTTPS ourselves (own CA; tailscale serve forwards TCP)
    tls_key: Path | None = None
    notify_token_file: Path | None = None  # `lariat notify` (agent hooks) proves itself with this
    push_subject: str | None = None  # VAPID contact; default https://<host>

    cert_file: Path | None = None

    @property
    def secure_cookies(self) -> bool:
        return self.origin.startswith("https://")

    def login_shell(self) -> list[str]:
        if self.shell_command:
            return list(self.shell_command)
        if sys.platform == "win32":
            for name in ("pwsh", "powershell"):
                found = shutil.which(name)
                if found:
                    return [found, "-NoLogo"]
            return [os.environ.get("COMSPEC", "cmd.exe")]
        import pwd

        shell = os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"
        return [shell, "-l"]


def _path(name: str) -> Path | None:
    value = _env(name)
    return Path(value).expanduser() if value else None


def load() -> Config:
    host = _env("LARIAT_HOST")
    if not host:
        raise ConfigError("LARIAT_HOST is required (the WebAuthn RP ID, e.g. host.example.ts.net)")
    origin = _env("LARIAT_ORIGIN", f"https://{host}")
    assert origin is not None

    shell_command = None
    raw = _env("LARIAT_SHELL_COMMAND")
    if raw:
        try:
            shell_command = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ConfigError("LARIAT_SHELL_COMMAND must be a JSON list of strings") from e
        if not (isinstance(shell_command, list) and shell_command and all(isinstance(a, str) for a in shell_command)):
            raise ConfigError("LARIAT_SHELL_COMMAND must be a non-empty JSON list of strings")

    roots = [Path(p).expanduser() for p in _list("LARIAT_PROJECT_ROOTS")] or [Path.home()]

    dictate_args: list[str] = []
    raw = _env("LARIAT_DICTATE_ARGS")
    if raw:
        try:
            dictate_args = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ConfigError("LARIAT_DICTATE_ARGS must be a JSON list of strings") from e
        if not (isinstance(dictate_args, list) and all(isinstance(a, str) for a in dictate_args)):
            raise ConfigError("LARIAT_DICTATE_ARGS must be a JSON list of strings")

    cfg = Config(
        host=host,
        origin=origin.rstrip("/"),
        bind=_env("LARIAT_BIND", "127.0.0.1") or "127.0.0.1",
        port=_int("LARIAT_PORT", 8795),
        static_dir=_path("LARIAT_STATIC") or bundled_web(),
        session_key_file=_path("LARIAT_SESSION_KEY_FILE"),
        enroll_token_file=_path("LARIAT_ENROLL_TOKEN_FILE"),
        credentials_file=_path("LARIAT_CREDENTIALS_FILE"),
        enrollment=_bool("LARIAT_ENROLL", False),
        idle_minutes=_int("LARIAT_SESSION_IDLE_MINUTES", 30),
        absolute_minutes=_int("LARIAT_SESSION_ABSOLUTE_MINUTES", 480),
        rp_name=_env("LARIAT_RP_NAME", "lariat") or "lariat",
        user_name=_env("LARIAT_USER_NAME", "owner") or "owner",
        app_name=_env("LARIAT_APP_NAME", "lariat") or "lariat",
        shell_enable=_bool("LARIAT_SHELL", True),
        shell_command=shell_command,
        tmux=_env("LARIAT_TMUX"),
        tmux_default=_env("LARIAT_TMUX_DEFAULT"),
        herdr=_env("LARIAT_HERDR"),
        herdr_sessions=_list("LARIAT_HERDR_SESSIONS") or ["default"],
        herdr_methods_file=_path("LARIAT_HERDR_METHODS"),
        herdr_prefix=_env("LARIAT_HERDR_PREFIX", "ctrl+b") or "ctrl+b",
        agent_kinds=_list("LARIAT_AGENT_KINDS"),
        project_roots=roots,
        ntfy_url=(_env("LARIAT_NTFY") or "").rstrip("/") or None,
        ntfy_topic=_env("LARIAT_NTFY_TOPIC", "agents") or "agents",
        dictate=_bool("LARIAT_DICTATE", False),
        whisper=_env("LARIAT_DICTATE_SERVER"),
        whisper_model=_env("LARIAT_DICTATE_MODEL"),
        ffmpeg=_env("LARIAT_DICTATE_FFMPEG"),
        dictate_language=_env("LARIAT_DICTATE_LANGUAGE", "en") or "en",
        dictate_threads=_int("LARIAT_DICTATE_THREADS", 4),
        dictate_max_seconds=_int("LARIAT_DICTATE_MAX_SECONDS", 60),
        dictate_idle_minutes=_float("LARIAT_DICTATE_IDLE_MINUTES", 0.0),
        dictate_prompt=_env("LARIAT_DICTATE_PROMPT", "") or "",
        dictate_args=dictate_args,
        upload_days=_float("LARIAT_UPLOAD_DAYS", 7.0),
        push=_bool("LARIAT_PUSH", True),
        tls_cert=_path("LARIAT_TLS_CERT"),
        tls_key=_path("LARIAT_TLS_KEY"),
        push_subject=_env("LARIAT_PUSH_SUBJECT"),
        cert_file=_path("LARIAT_CERT_FILE"),
    )
    # systemd's StateDirectory=/RuntimeDirectory= export these.
    state = _path("LARIAT_STATE_DIR") or _path("STATE_DIRECTORY")
    if state:
        cfg.state_dir = state
    runtime = _path("LARIAT_RUNTIME_DIR") or _path("RUNTIME_DIRECTORY")
    if runtime:
        cfg.runtime_dir = runtime
    # Without secret paths the gateway keeps its own in the state dir: a session
    # key, and a one-time enrollment token that `lariat enroll` shows and renews.
    if cfg.session_key_file is None:
        cfg.session_key_file = ensure_secret(cfg.state_dir / "session-key", lambda: secrets.token_bytes(48))
    if cfg.enroll_token_file is None:
        cfg.enroll_token_file = ensure_secret(cfg.state_dir / "enroll-token", new_token)
    cfg.notify_token_file = ensure_secret(cfg.state_dir / "notify-token", new_token)
    if cfg.dictate and not (cfg.whisper and cfg.whisper_model):
        raise ConfigError("dictation needs LARIAT_DICTATE_SERVER (whisper-server) and LARIAT_DICTATE_MODEL")
    return cfg


def bundled_web() -> Path | None:
    """The web app shipped inside a wheel or a single-file binary, if any."""
    here = Path(__file__).parent / "web"
    return here if (here / "index.html").exists() else None


def new_token() -> bytes:
    return secrets.token_urlsafe(32).encode()


def ensure_secret(path: Path, make) -> Path:
    """Create a private file with make()'s bytes unless it exists. Returns the path."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path
    with os.fdopen(fd, "wb") as f:
        f.write(make())
    return path


def read_secret(path: Path | None) -> bytes:
    """Read a secret file; trailing whitespace is stripped. Missing file = empty."""
    if path is None:
        return b""
    try:
        return path.read_bytes().strip()
    except FileNotFoundError:
        return b""
