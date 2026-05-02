"""Low-level SSH helpers: config resolution, socket management, master lifecycle.

All subprocess calls to ssh live here so the CLI layer stays purely declarative.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import click

_DEFAULT_SOCKETS_DIR = Path.home() / ".ssh" / "pf_sockets"
_DEFAULT_SSH_CONFIG = Path.home() / ".ssh" / "config"
_SSH_G_TIMEOUT = 10
_SSH_CONTROL_TIMEOUT = 10
_SSH_OPEN_TIMEOUT = 120


def _raise(msg: str) -> NoReturn:
    raise click.ClickException(msg)


def _run_ssh(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _raise(f"SSH command timed out after {timeout}s: {' '.join(cmd[:4])}...")


@dataclass(frozen=True)
class ResolvedHost:
    """Result of resolving an SSH config alias via `ssh -G`."""

    config_name: str
    hostname: str
    user: str
    port: str
    sockets_dir: Path = _DEFAULT_SOCKETS_DIR
    config_path: Path | None = None

    @property
    def socket_path(self) -> Path:
        return self.sockets_dir / f"{self.user}@{self.config_name}-{self.port}"

    def _ssh_base(self) -> list[str]:
        """Common ssh prefix: optional -F for config, always -S for socket."""
        cmd = ["ssh"]
        if self.config_path is not None:
            cmd += ["-F", str(self.config_path)]
        cmd += ["-S", str(self.socket_path)]
        return cmd


def resolve_host(config_name: str, sockets_dir: Path = _DEFAULT_SOCKETS_DIR, config_path: Path | None = None) -> ResolvedHost:
    """Run `ssh -G <config_name>` and parse the resolved config."""
    cmd = ["ssh", "-G"]
    if config_path is not None:
        cmd += ["-F", str(config_path)]
    cmd.append(config_name)

    result = _run_ssh(cmd, _SSH_G_TIMEOUT)
    if result.returncode != 0:
        _raise(f"ssh -G {config_name} failed: {result.stderr.strip()}")

    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            fields[parts[0].lower()] = parts[1]

    return ResolvedHost(
        config_name=config_name,
        hostname=fields.get("hostname", config_name),
        user=fields.get("user", ""),
        port=fields.get("port", "22"),
        sockets_dir=sockets_dir,
        config_path=config_path,
    )


def ensure_sockets_dir(sockets_dir: Path = _DEFAULT_SOCKETS_DIR) -> None:
    sockets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)


def is_master_alive(host: ResolvedHost) -> bool:
    if not host.socket_path.exists():
        return False
    result = _run_ssh(host._ssh_base() + ["-O", "check", host.config_name], _SSH_CONTROL_TIMEOUT)
    return result.returncode == 0


def open_master(
    host: ResolvedHost,
    local_forwards: list[tuple[int, int]] | None = None,
    extra_args: list[str] | None = None,
) -> None:
    cmd = host._ssh_base() + ["-fNM"]
    for local_port, remote_port in local_forwards or []:
        cmd += ["-L", f"{local_port}:localhost:{remote_port}"]
    cmd += extra_args or []
    cmd.append(host.config_name)

    result = _run_ssh(cmd, _SSH_OPEN_TIMEOUT)
    if result.returncode != 0:
        _raise(f"Failed to open master: {result.stderr.strip()}")


def close_master(host: ResolvedHost) -> None:
    result = _run_ssh(host._ssh_base() + ["-O", "exit", host.config_name], _SSH_CONTROL_TIMEOUT)
    if result.returncode != 0:
        _raise(f"Failed to close master: {result.stderr.strip()}")


def add_forward(host: ResolvedHost, local_port: int, remote_port: int) -> None:
    result = _run_ssh(
        host._ssh_base() + ["-O", "forward", "-L", f"{local_port}:localhost:{remote_port}", host.config_name],
        _SSH_CONTROL_TIMEOUT,
    )
    if result.returncode != 0:
        _raise(f"Failed to add forward: {result.stderr.strip()}")


def exec_command(host: ResolvedHost, remote_cmd: list[str], extra_ssh_args: list[str] | None = None) -> subprocess.CompletedProcess:
    cmd = host._ssh_base() + (extra_ssh_args or []) + [host.config_name] + remote_cmd
    return _run_ssh(cmd, _SSH_OPEN_TIMEOUT)


def discover_sockets(sockets_dir: Path = _DEFAULT_SOCKETS_DIR) -> list[tuple[ResolvedHost, bool]]:
    """Scan the sockets directory and return (host, alive) pairs.

    Parses socket filenames matching the pattern `user@config_name-port`.
    """
    if not sockets_dir.exists():
        return []

    results: list[tuple[ResolvedHost, bool]] = []
    for path in sorted(sockets_dir.iterdir()):
        if not path.is_socket():
            continue
        name = path.name
        try:
            user_part, rest = name.split("@", 1)
            config_name, port = rest.rsplit("-", 1)
        except ValueError:
            continue
        # TODO(hadriano): hostname and config_path are unknown here — consider calling
        # resolve_host() to fill them in, but that adds ssh -G calls and can fail for
        # hosts removed from config. Review whether callers need complete objects.
        host = ResolvedHost(config_name=config_name, hostname="", user=user_part, port=port, sockets_dir=sockets_dir)
        alive = is_master_alive(host)
        results.append((host, alive))
    return results


def parse_ssh_config_raw(config_path: Path = _DEFAULT_SSH_CONFIG) -> str:
    if not config_path.exists():
        _raise(f"No SSH config found at {config_path}")
    return config_path.read_text()


@dataclass
class SSHConfigBlock:
    name: str
    lines: list[str]

    @property
    def body(self) -> str:
        return "\n".join(self.lines)


def parse_ssh_config_blocks(config_path: Path = _DEFAULT_SSH_CONFIG) -> list[SSHConfigBlock]:
    raw = parse_ssh_config_raw(config_path)
    blocks: list[SSHConfigBlock] = []
    current: SSHConfigBlock | None = None

    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("host "):
            if current:
                blocks.append(current)
            name = stripped.split(None, 1)[1]
            current = SSHConfigBlock(name=name, lines=[line])
        elif current:
            current.lines.append(line)

    if current:
        blocks.append(current)
    return blocks
