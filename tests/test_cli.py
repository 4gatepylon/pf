"""Integration tests for pf CLI.

These tests spin up a local sshd (see conftest.py for safety details) and
exercise all pf commands against it using key-based auth. No real remote
machines are contacted, no system config is read or modified.

What we test:
    - open: creates socket, master process is alive
    - open (duplicate): raises with helpful message
    - open (stale cleanup): auto-cleans stale socket on re-open
    - open with port forwarding (-pf/-pt)
    - open with partial port args (error)
    - close: removes socket, kills master
    - close --all: closes all masters
    - close --stale / reap: removes only stale sockets
    - forward: adds port forward to existing master
    - forward (no master): raises
    - forward (stale socket): raises with cleanup
    - exec: runs command through socket
    - exec (stale socket): raises with cleanup
    - list / info --short: one-line-per-host output
    - info / info --full: detailed output
    - info <host>: single-host detail
    - config --short / --full / --name: SSH config display
    - socket bypass: second connection needs no credentials

What we do NOT test (requires real infrastructure):
    - ProxyJump / bastion host behavior
    - Kerberos / DUO / password authentication
    - Network drop recovery
    - VS Code Remote-SSH integration
"""

import os
import signal
import subprocess
import time

import pytest
from click.testing import CliRunner

from pf.cli import main
from pf.ssh import (
    ResolvedHost,
    add_forward,
    close_master,
    discover_sockets,
    exec_command,
    is_master_alive,
    open_master,
    parse_ssh_config_blocks,
    parse_ssh_config_raw,
    resolve_host,
)


def _resolve(sshd) -> ResolvedHost:
    return resolve_host("pf-test-host", sockets_dir=sshd.sockets_dir, config_path=sshd.config_path)


def _invoke(sshd, args: list[str], catch_exceptions: bool = False):
    """Run a pf CLI command with the test SSH config and sockets dir injected via env vars."""
    runner = CliRunner()
    env = {"PF_SSH_CONFIG": str(sshd.config_path), "PF_SOCKETS_DIR": str(sshd.sockets_dir)}
    return runner.invoke(main, args, catch_exceptions=catch_exceptions, env=env)


def _cleanup(sshd):
    """Force-close any leftover master for test isolation."""
    host = _resolve(sshd)
    if host.socket_path.exists():
        if is_master_alive(host):
            close_master(host)
        else:
            host.socket_path.unlink()


def _make_stale_socket(sshd):
    """Create a genuine stale socket by opening a master and killing its process."""
    host = _resolve(sshd)
    open_master(host)
    assert host.socket_path.is_socket()

    result = subprocess.run(
        host._ssh_base() + ["-O", "check", host.config_name],
        capture_output=True,
        text=True,
        timeout=5,
    )
    pid_str = result.stderr.strip()
    # Parse "Master running (pid=12345)"
    pid = int(pid_str.split("pid=")[1].rstrip(")"))
    os.kill(pid, signal.SIGKILL)
    # SIGKILL delivery is async; give the kernel time to reap the process
    time.sleep(0.2)

    assert host.socket_path.exists()
    assert not is_master_alive(host)
    return host


@pytest.fixture(autouse=True)
def clean_sockets(sshd):
    """Ensure no leftover sockets before/after each test."""
    _cleanup(sshd)
    yield
    _cleanup(sshd)


# ---------------------------------------------------------------------------
# Library-level tests
# ---------------------------------------------------------------------------


class TestOpenLib:
    def test_open_creates_master(self, sshd):
        host = _resolve(sshd)
        open_master(host)
        assert host.socket_path.exists()
        assert is_master_alive(host)


class TestCloseLib:
    def test_close_kills_master(self, sshd):
        host = _resolve(sshd)
        open_master(host)
        assert is_master_alive(host)
        close_master(host)
        assert not is_master_alive(host)


class TestExecLib:
    def test_exec_runs_command(self, sshd):
        host = _resolve(sshd)
        open_master(host)
        result = exec_command(host, ["echo", "hello"])
        assert result.returncode == 0
        assert result.stdout.strip() == "hello"


class TestSocketBypass:
    """Verify that a connection through the socket does not need credentials."""

    def test_no_auth_needed_through_socket(self, sshd):
        host = _resolve(sshd)
        open_master(host)
        result = exec_command(host, ["echo", "hello"], extra_ssh_args=["-o", "BatchMode=yes", "-o", "IdentityFile=/dev/null"])
        assert result.returncode == 0
        assert result.stdout.strip() == "hello"

    def test_no_socket_no_key_fails(self, sshd):
        host = _resolve(sshd)
        assert not is_master_alive(host)

        result = exec_command(
            host,
            ["echo", "hello"],
            extra_ssh_args=["-o", "BatchMode=yes", "-o", "PubkeyAuthentication=no"],
        )
        assert result.returncode != 0


class TestForwardLib:
    def test_forward_on_existing_master(self, sshd):
        host = _resolve(sshd)
        open_master(host)
        add_forward(host, 18222, 22)


class TestDiscoverLib:
    def test_discover_finds_sockets(self, sshd):
        host = _resolve(sshd)
        open_master(host)
        sockets = discover_sockets(sshd.sockets_dir)
        assert len(sockets) >= 1
        names = [h.config_name for h, _ in sockets]
        assert "pf-test-host" in names


class TestConfigLib:
    def test_parse_blocks(self, sshd):
        blocks = parse_ssh_config_blocks(sshd.config_path)
        names = [b.name for b in blocks]
        assert "pf-test-host" in names

    def test_parse_raw(self, sshd):
        raw = parse_ssh_config_raw(sshd.config_path)
        assert "pf-test-host" in raw


# ---------------------------------------------------------------------------
# CLI-level tests (via CliRunner + PF_SSH_CONFIG / PF_SOCKETS_DIR env vars)
# ---------------------------------------------------------------------------


class TestOpenCLI:
    def test_open_via_cli(self, sshd):
        result = _invoke(sshd, ["open", "pf-test-host"])
        assert result.exit_code == 0, result.output
        assert "Master opened" in result.output

    def test_open_duplicate_via_cli(self, sshd):
        r1 = _invoke(sshd, ["open", "pf-test-host"])
        assert r1.exit_code == 0, r1.output
        result = _invoke(sshd, ["open", "pf-test-host"])
        assert result.exit_code != 0
        assert "already running" in result.output
        assert "pf forward" in result.output

    def test_open_stale_cleanup_via_cli(self, sshd):
        _make_stale_socket(sshd)
        result = _invoke(sshd, ["open", "pf-test-host"])
        assert result.exit_code == 0, result.output
        assert "stale" in result.output.lower()

    def test_open_with_port_forward(self, sshd):
        result = _invoke(sshd, ["open", "pf-test-host", "-pf", "18444", "-pt", "22"])
        assert result.exit_code == 0, result.output
        assert "Master opened" in result.output
        assert "Forward" in result.output
        assert "18444" in result.output

    def test_open_partial_port_args_errors(self, sshd):
        result = _invoke(sshd, ["open", "pf-test-host", "-pf", "8080"])
        assert result.exit_code != 0
        assert "Both --port-from and --port-to" in result.output


class TestCloseCLI:
    def test_close_via_cli(self, sshd):
        r = _invoke(sshd, ["open", "pf-test-host"])
        assert r.exit_code == 0, r.output
        result = _invoke(sshd, ["close", "pf-test-host"])
        assert result.exit_code == 0
        assert "Closed" in result.output

    def test_close_all_via_cli(self, sshd):
        r = _invoke(sshd, ["open", "pf-test-host"])
        assert r.exit_code == 0, r.output
        result = _invoke(sshd, ["close", "--all"])
        assert result.exit_code == 0
        assert "Closed" in result.output or "Removed" in result.output

    def test_close_stale_via_cli(self, sshd):
        _make_stale_socket(sshd)
        result = _invoke(sshd, ["close", "--stale"])
        assert result.exit_code == 0
        assert "Removed stale socket" in result.output

    def test_close_no_args_errors(self, sshd):
        result = _invoke(sshd, ["close"])
        assert result.exit_code != 0

    def test_close_contradictory_host_and_all(self, sshd):
        result = _invoke(sshd, ["close", "pf-test-host", "--all"])
        assert result.exit_code != 0
        assert "Cannot combine" in result.output

    def test_close_contradictory_host_and_stale(self, sshd):
        result = _invoke(sshd, ["close", "pf-test-host", "--stale"])
        assert result.exit_code != 0
        assert "Cannot combine" in result.output

    def test_reap_via_cli(self, sshd):
        _make_stale_socket(sshd)
        result = _invoke(sshd, ["reap"])
        assert result.exit_code == 0
        assert "Removed stale socket" in result.output


class TestExecCLI:
    def test_exec_via_cli(self, sshd):
        r = _invoke(sshd, ["open", "pf-test-host"])
        assert r.exit_code == 0, r.output
        result = _invoke(sshd, ["exec", "pf-test-host", "echo", "hello"])
        assert "hello" in result.output

    def test_exec_no_socket_errors(self, sshd):
        result = _invoke(sshd, ["exec", "pf-test-host", "echo", "hello"])
        assert result.exit_code != 0
        assert "No socket found" in result.output

    def test_exec_stale_socket_errors(self, sshd):
        _make_stale_socket(sshd)
        result = _invoke(sshd, ["exec", "pf-test-host", "echo", "hello"])
        assert result.exit_code != 0
        assert "stale" in result.output.lower()


class TestForwardCLI:
    def test_forward_via_cli(self, sshd):
        r = _invoke(sshd, ["open", "pf-test-host"])
        assert r.exit_code == 0, r.output
        result = _invoke(sshd, ["forward", "pf-test-host", "-pf", "18333", "-pt", "22"])
        assert result.exit_code == 0
        assert "Forward added" in result.output

    def test_forward_no_socket_errors(self, sshd):
        result = _invoke(sshd, ["forward", "pf-test-host", "-pf", "18333", "-pt", "22"])
        assert result.exit_code != 0
        assert "No socket found" in result.output

    def test_forward_stale_socket_errors(self, sshd):
        _make_stale_socket(sshd)
        result = _invoke(sshd, ["forward", "pf-test-host", "-pf", "18333", "-pt", "22"])
        assert result.exit_code != 0
        assert "stale" in result.output.lower()


class TestListCLI:
    def test_list_shows_alive(self, sshd):
        r = _invoke(sshd, ["open", "pf-test-host"])
        assert r.exit_code == 0, r.output
        result = _invoke(sshd, ["list"])
        assert result.exit_code == 0
        assert "pf-test-host" in result.output

    def test_list_empty(self, sshd):
        result = _invoke(sshd, ["list"])
        assert result.exit_code == 0
        assert "No sockets found" in result.output


class TestInfoCLI:
    def test_info_full_default(self, sshd):
        r = _invoke(sshd, ["open", "pf-test-host"])
        assert r.exit_code == 0, r.output
        result = _invoke(sshd, ["info"])
        assert result.exit_code == 0
        assert "Host:" in result.output
        assert "Socket:" in result.output

    def test_info_short(self, sshd):
        r = _invoke(sshd, ["open", "pf-test-host"])
        assert r.exit_code == 0, r.output
        result = _invoke(sshd, ["info", "--short"])
        assert result.exit_code == 0
        assert "pf-test-host" in result.output
        assert "Socket:" not in result.output

    def test_info_named_host(self, sshd):
        r = _invoke(sshd, ["open", "pf-test-host"])
        assert r.exit_code == 0, r.output
        result = _invoke(sshd, ["info", "pf-test-host"])
        assert result.exit_code == 0
        assert "Hostname:" in result.output

    def test_info_no_socket_errors(self, sshd):
        result = _invoke(sshd, ["info", "pf-test-host"])
        assert result.exit_code != 0
        assert "No socket found" in result.output


class TestConfigCLI:
    def test_config_short_default(self, sshd):
        result = _invoke(sshd, ["config"])
        assert result.exit_code == 0
        assert "pf-test-host" in result.output

    def test_config_short_no_pager(self, sshd):
        result = _invoke(sshd, ["config", "--no-pager"])
        assert result.exit_code == 0
        # --no-pager in short mode outputs bare names (no leading spaces)
        lines = [line for line in result.output.splitlines() if line.strip()]
        assert "pf-test-host" in lines

    def test_config_full_no_pager(self, sshd):
        result = _invoke(sshd, ["config", "--full", "--no-pager"])
        assert result.exit_code == 0
        assert "Hostname" in result.output
        assert "pf-test-host" in result.output
        assert "pf socket status" in result.output

    def test_config_full_shows_socket_status(self, sshd):
        _invoke(sshd, ["open", "pf-test-host"])
        result = _invoke(sshd, ["config", "--full", "--no-pager"])
        assert result.exit_code == 0
        assert "pf-test-host: alive" in result.output

    def test_config_name(self, sshd):
        result = _invoke(sshd, ["config", "--name", "pf-test-host"])
        assert result.exit_code == 0
        assert "Hostname" in result.output

    def test_config_name_not_found(self, sshd):
        result = _invoke(sshd, ["config", "--name", "nonexistent-host"])
        assert result.exit_code != 0
        assert "No Host block" in result.output
