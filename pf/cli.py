"""pf — SSH ControlMaster socket manager.

A thin wrapper around SSH's ControlMaster multiplexing that manages background
master connections so you authenticate once and every subsequent shell, VS Code
window, scp, or rsync to the same host reuses the authenticated socket.

Setup (pick one):
    # If saescoping conda env is active, pf is already on PATH after `pip install -e .`
    pf open align3

    # Without activating the env, add to ~/.bashrc / ~/.zshrc / ~/.profile:
    alias pf='/opt/miniconda3/envs/saescoping/bin/pf'

Failure modes & behavior:
    NORMAL EXIT (`exit`, Ctrl-D, closing terminal):
        The master is a separate background process (`ssh -fNM`). Exiting or
        closing any shell — including the one that ran `pf open` — has NO
        effect on the master or on other connections using the socket.

    MASTER CRASH / `kill -9`:
        The socket file becomes stale. All connections multiplexed through that
        socket die immediately. `pf list` will show the socket as "stale" and
        print a cleanup command. `pf reap` removes all stale sockets. Next
        `pf open` will detect and clean up a stale socket automatically before
        creating a fresh master (requiring re-authentication).

    NETWORK DROP / SERVER REBOOT:
        Same as a crash — socket becomes stale, same recovery path.

    SOCKET TIMEOUT:
        There is no built-in timeout on the master (unlike ControlPersist in
        SSH config). The master lives until explicitly closed, crashed, or the
        network drops. If you want auto-expiry, add `ControlPersist 4h` to
        your SSH config for the relevant hosts.

    PORT FORWARDING ON EXISTING MASTER (`pf forward`):
        Uses `ssh -O forward` to dynamically add tunnels without re-auth.
        Forwards die when the master dies.

Socket path convention:
    ~/.ssh/pf_sockets/{user}@{config_name}-{port}

    Uses the SSH config alias name (not the resolved hostname) so that
    different Host blocks pointing to the same machine get independent sockets.
"""

from pathlib import Path

import click
from tabulate import tabulate as _tabulate

from pf.ssh import (
    ResolvedHost,
    _DEFAULT_SOCKETS_DIR,
    _DEFAULT_SSH_CONFIG,
    add_forward,
    close_master,
    discover_sockets,
    ensure_sockets_dir,
    exec_command,
    format_duration,
    interactive_shell,
    is_master_alive,
    open_master,
    parse_ssh_config_blocks,
    parse_ssh_config_raw,
    resolve_host,
    socket_clients,
    socket_uptime,
)


class _PfContext:
    """Holds resolved config/sockets paths for the CLI session."""

    def __init__(self, sockets_dir: Path, config_path: Path | None):
        self.sockets_dir = sockets_dir
        self.config_path = config_path

    def resolve(self, host: str) -> ResolvedHost:
        return resolve_host(host, sockets_dir=self.sockets_dir, config_path=self.config_path)

    def require_alive(self, host: str, hint: str = "Use `pf open {host}` first.") -> ResolvedHost:
        """Resolve host and assert its master is alive, cleaning stale sockets."""
        resolved = self.resolve(host)
        suffix = hint.format(host=host) if hint else ""
        if not resolved.socket_path.exists():
            msg = f"No socket found for '{host}'."
            raise click.ClickException(f"{msg} {suffix}".strip())
        if not is_master_alive(resolved):
            resolved.socket_path.unlink()
            msg = f"Socket for '{host}' was stale (now removed)."
            raise click.ClickException(f"{msg} {suffix}".strip())
        return resolved


@click.group()
@click.option("--sockets-dir", envvar="PF_SOCKETS_DIR", type=click.Path(path_type=Path), default=None, hidden=True)
@click.option("--ssh-config", envvar="PF_SSH_CONFIG", type=click.Path(path_type=Path), default=None, hidden=True)
@click.pass_context
def main(ctx, sockets_dir: Path | None, ssh_config: Path | None):
    """SSH ControlMaster socket manager."""
    ctx.obj = _PfContext(
        sockets_dir=sockets_dir or _DEFAULT_SOCKETS_DIR,
        config_path=ssh_config,
    )


@main.command()
@click.argument("host")
@click.option("-pf", "--port-from", type=int, default=None, help="Local port for forwarding.")
@click.option("-pt", "--port-to", type=int, default=None, help="Remote port for forwarding.")
@click.option("-u", "--user", default=None, help="Override the SSH config user.")
@click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_obj
def open(pf: _PfContext, host: str, port_from: int | None, port_to: int | None, user: str | None, extra_args: tuple[str, ...]):
    """Open a background master connection to HOST.

    HOST is an SSH config alias (e.g. 'align3'). All connection details
    (hostname, user, proxy, auth) are read from ~/.ssh/config.
    """
    ensure_sockets_dir(pf.sockets_dir)
    resolved = pf.resolve(host)

    if user:
        resolved = ResolvedHost(
            config_name=resolved.config_name,
            hostname=resolved.hostname,
            user=user,
            port=resolved.port,
            sockets_dir=resolved.sockets_dir,
            config_path=resolved.config_path,
        )

    if resolved.socket_path.exists() and is_master_alive(resolved):
        raise click.ClickException(
            f"Master for '{host}' is already running.\n"
            f"  Use `pf list` to see active connections.\n"
            f"  Use `pf forward {host} -pf <local> -pt <remote>` to add port forwarding."
        )

    if resolved.socket_path.exists() and not is_master_alive(resolved):
        resolved.socket_path.unlink()
        click.echo(f"Cleaned up stale socket for '{host}'.")

    forwards = [(port_from, port_to)] if port_from is not None and port_to is not None else None
    if (port_from is None) != (port_to is None):
        raise click.ClickException("Both --port-from and --port-to are required for forwarding.")

    extra = list(extra_args)
    if extra:
        click.echo(
            f"Warning: passing extra SSH arguments via -- is not the intended workflow. Got: {extra}",
            err=True,
        )

    open_master(resolved, local_forwards=forwards, extra_args=extra or None)
    click.echo(f"Master opened: {host} ({resolved.user}@{resolved.hostname}:{resolved.port})")
    if forwards:
        for lp, rp in forwards:
            click.echo(f"  Forward: localhost:{lp} -> {host}:{rp}")


@main.command()
@click.argument("host", required=False)
@click.option("--all", "close_all", is_flag=True, help="Close all active master connections.")
@click.option("--stale", "close_stale", is_flag=True, help="Remove all stale sockets.")
@click.pass_obj
def close(pf: _PfContext, host: str | None, close_all: bool, close_stale: bool):
    """Close a master connection for HOST, or --all/--stale."""
    if not host and not close_all and not close_stale:
        raise click.ClickException("Specify a host, --all, or --stale.")
    if host and (close_all or close_stale):
        raise click.ClickException("Cannot combine a host argument with --all or --stale.")
    if close_all and close_stale:
        raise click.ClickException("Cannot combine --all and --stale.")

    if close_stale:
        sockets = discover_sockets(pf.sockets_dir)
        removed = 0
        for resolved, alive in sockets:
            if not alive:
                resolved.socket_path.unlink()
                click.echo(f"Removed stale socket: {resolved.config_name}")
                removed += 1
        if removed == 0:
            click.echo("No stale sockets found.")
        return

    if close_all:
        sockets = discover_sockets(pf.sockets_dir)
        if not sockets:
            click.echo("No active sockets.")
            return
        for resolved, alive in sockets:
            if alive:
                close_master(resolved)
                click.echo(f"Closed: {resolved.config_name}")
            else:
                resolved.socket_path.unlink()
                click.echo(f"Removed stale socket: {resolved.config_name}")
        return

    resolved = pf.require_alive(host, hint="")
    close_master(resolved)
    click.echo(f"Closed: {host}")


# TODO(adriano): reap is untested manually
@main.command()
@click.pass_context
def reap(ctx):
    """Remove all stale sockets (alias for `close --stale`)."""
    ctx.invoke(close, close_stale=True)


@main.command("list")
@click.pass_obj
def list_cmd(pf: _PfContext):
    """List all sockets with their status (alias for `info --short`)."""
    _print_sockets(pf, full=False)


@main.command()
@click.argument("host", required=False)
@click.option("--short", "mode", flag_value="short", help="Brief one-line-per-host view.")
@click.option("--full", "mode", flag_value="full", help="Detailed view of all hosts.")
@click.option("--name", "named_host", default=None, help="Show detailed info for a specific host.")
@click.pass_obj
def info(pf: _PfContext, host: str | None, mode: str | None, named_host: str | None):
    """Show status of active master connections.

    Defaults to --full (detailed view of all hosts).
    With a HOST argument or --name, shows detailed info for that host.
    """
    target = host or named_host
    if target:
        _print_info_host(pf, target)
        return

    if mode == "short":
        _print_sockets(pf, full=False)
    else:
        _print_sockets(pf, full=True)


def _resolve_controlpersist(pf: _PfContext, config_name: str) -> str | None:
    try:
        return resolve_host(config_name, sockets_dir=pf.sockets_dir, config_path=pf.config_path).controlpersist
    except Exception:
        return None


def _uptime_cell(resolved: ResolvedHost, alive: bool, controlpersist: str | None) -> str:
    uptime = socket_uptime(resolved)
    if not alive or uptime is None:
        return ""
    up = format_duration(uptime)
    if controlpersist:
        return f"{up} / {controlpersist} persist"
    return up


def _print_sockets(pf: _PfContext, full: bool):
    sockets = discover_sockets(pf.sockets_dir)
    if not sockets:
        click.echo("No sockets found.")
        return

    has_stale = False
    rows: list[list[str]] = []
    for resolved, alive in sockets:
        status = click.style("alive", fg="green") if alive else click.style("stale", fg="red")
        if not alive:
            has_stale = True
        cp = _resolve_controlpersist(pf, resolved.config_name)
        uptime_str = _uptime_cell(resolved, alive, cp)
        clients = socket_clients(resolved) if alive else None
        clients_str = str(clients) if clients is not None else ""

        if full:
            row = [resolved.config_name, resolved.user, resolved.port, status, uptime_str, clients_str, str(resolved.socket_path)]
        else:
            row = [resolved.config_name, status, uptime_str, clients_str]
        rows.append(row)

    if full:
        headers = ["Host", "User", "Port", "Status", "Uptime", "Clients", "Socket"]
    else:
        headers = ["Host", "Status", "Uptime", "Clients"]

    click.echo(_tabulate(rows, headers=headers, tablefmt="plain"))

    if has_stale:
        click.echo()
        click.secho("Stale sockets detected. Clean up with:", fg="yellow")
        click.echo("  pf reap")


def _print_info_host(pf: _PfContext, host: str):
    resolved = pf.resolve(host)
    if not resolved.socket_path.exists():
        raise click.ClickException(f"No socket found for '{host}'. Use `pf open {host}` first.")

    alive = is_master_alive(resolved)
    status = click.style("alive", fg="green") if alive else click.style("stale", fg="red")
    uptime = socket_uptime(resolved)
    clients = socket_clients(resolved) if alive else None
    click.echo(f"  Host:            {resolved.config_name}")
    click.echo(f"  Hostname:        {resolved.hostname}")
    click.echo(f"  User:            {resolved.user}")
    click.echo(f"  Port:            {resolved.port}")
    click.echo(f"  Socket:          {resolved.socket_path}")
    click.echo(f"  Status:          {status}")
    if alive and uptime is not None:
        click.echo(f"  Uptime:          {format_duration(uptime)} (since socket was created)")
    if resolved.controlpersist:
        click.echo(f"  ControlPersist:  {resolved.controlpersist} (idle timeout, resets on each disconnect)")
    if clients is not None:
        click.echo(f"  Clients:         {clients}")

    if not alive:
        click.echo()
        click.secho("Socket is stale. Clean up with:", fg="yellow")
        click.echo("  pf reap")


# TODO(adriano): forward is untested manually
@main.command()
@click.argument("host")
@click.option("-pf", "--port-from", type=int, required=True, help="Local port.")
@click.option("-pt", "--port-to", type=int, required=True, help="Remote port.")
@click.pass_obj
def forward(pf: _PfContext, host: str, port_from: int, port_to: int):
    """Add a local port forward to an existing master connection for HOST."""
    resolved = pf.require_alive(host)
    add_forward(resolved, port_from, port_to)
    click.echo(f"Forward added: localhost:{port_from} -> {host}:{port_to}")


@main.command("exec")
@click.argument("host")
@click.argument("remote_cmd", nargs=-1, required=True)
@click.pass_obj
def exec_cmd(pf: _PfContext, host: str, remote_cmd: tuple[str, ...]):
    """Run a command on HOST through the existing master socket."""
    resolved = pf.require_alive(host)
    result = exec_command(resolved, list(remote_cmd))
    if result.stdout:
        click.echo(result.stdout, nl=False)
    if result.stderr:
        click.echo(result.stderr, nl=False, err=True)
    raise SystemExit(result.returncode)


@main.command("ssh")
@click.argument("host")
@click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_obj
def ssh_cmd(pf: _PfContext, host: str, extra_args: tuple[str, ...]):
    """Open an interactive shell on HOST through the existing master socket."""
    resolved = pf.require_alive(host)
    interactive_shell(resolved, extra_ssh_args=list(extra_args) or None)


@main.command("connect")
@click.argument("host")
@click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_obj
def connect_cmd(pf: _PfContext, host: str, extra_args: tuple[str, ...]):
    """Alias for `pf ssh`."""
    resolved = pf.require_alive(host)
    interactive_shell(resolved, extra_ssh_args=list(extra_args) or None)


@main.command()
@click.option("--short", "mode", flag_value="short", default=True, help="Show only host names (default).")
@click.option("--full", "mode", flag_value="full", help="Show full SSH config with socket status.")
@click.option("--no-pager", is_flag=True, help="Dump output to stdout instead of pager.")
@click.option("--name", "named_host", default=None, help="Show config for a specific host.")
@click.pass_obj
def config(pf: _PfContext, mode: str | None, no_pager: bool, named_host: str | None):
    """View SSH config host definitions (read-only)."""
    config_path = pf.config_path or _DEFAULT_SSH_CONFIG

    if named_host:
        blocks = parse_ssh_config_blocks(config_path)
        matches = [b for b in blocks if b.name == named_host]
        if not matches:
            raise click.ClickException(f"No Host block named '{named_host}' in SSH config.")
        for block in matches:
            click.echo(block.body)
        return

    if mode == "full":
        raw = parse_ssh_config_raw(config_path)
        sockets = discover_sockets(pf.sockets_dir)
        socket_status = {h.config_name: alive for h, alive in sockets}

        lines = [raw, "", "# --- pf socket status ---"]
        if socket_status:
            for name, alive in sorted(socket_status.items()):
                status = "alive" if alive else "stale"
                lines.append(f"# {name}: {status}")
        else:
            lines.append("# (no active sockets)")

        text = "\n".join(lines)
        if no_pager:
            click.echo(text)
        else:
            click.echo_via_pager(text)
        return

    blocks = parse_ssh_config_blocks(config_path)
    if no_pager:
        for block in blocks:
            click.echo(block.name)
    else:
        for block in blocks:
            click.echo(f"  {block.name}")


if __name__ == "__main__":
    main()
