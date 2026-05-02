"""Pytest fixtures for pf integration tests.

SSHD SAFETY:
    The local sshd started by these tests is completely isolated from the host:
    - Runs on a random high port (not 22), as an unprivileged user (no root).
    - Uses `-f /dev/null` so it ignores the system sshd_config entirely.
    - Uses a throwaway host key and authorized_keys file in a temp directory.
    - StrictModes=no and UsePAM=no so it doesn't touch /etc/passwd, /etc/shadow,
      or PAM — it cannot authenticate real system users.
    - PidFile=none so it doesn't write to /var/run.
    - The only thing it can do is accept key-based connections from the test keypair
      and run commands as the current user. It has no more privileges than any other
      process you run in your terminal.
    - Torn down automatically at the end of the test session (SIGTERM + wait).

    In short: it's an echo server that speaks SSH. It does not modify any system
    state, does not interfere with the real sshd (if any), and cleans up after itself.

PARALLELISM:
    Each test run gets a unique sockets directory under /tmp/pf_test_XXXX/ via
    tempfile.mkdtemp. A lockfile (/tmp/pf_test.lock) serializes sshd startup to
    avoid port races when multiple test processes launch simultaneously.
"""

import fcntl
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class SSHDFixture:
    port: int
    tmpdir: Path
    config_path: Path
    sockets_dir: Path
    host_key: Path
    private_key: Path
    process: subprocess.Popen


@pytest.fixture(scope="session")
def sshd(tmp_path_factory):
    """Start a local sshd for the test session, yield fixture, tear down."""
    sshd_bin = shutil.which("sshd")
    if sshd_bin is None:
        pytest.skip("sshd not found on this system")

    tmpdir = tmp_path_factory.mktemp("pf_test")

    # Short unique path for sockets — Unix domain sockets have a 104-byte limit on macOS.
    short_tmpdir = Path(tempfile.mkdtemp(prefix="pf_", dir="/tmp"))
    sockets_dir = short_tmpdir / "s"
    sockets_dir.mkdir(mode=0o700)

    host_key = tmpdir / "host_key"
    private_key = tmpdir / "test_key"
    authorized_keys = tmpdir / "authorized_keys"
    ssh_config = tmpdir / "ssh_config"

    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(private_key), "-N", ""],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(host_key), "-N", ""],
        capture_output=True,
        check=True,
    )
    authorized_keys.write_text((private_key.with_suffix(".pub")).read_text())

    # Serialize sshd startup across parallel test processes to avoid port races.
    lockfile = open("/tmp/pf_test.lock", "w")
    fcntl.flock(lockfile, fcntl.LOCK_EX)
    try:
        port = _find_free_port()

        ssh_config.write_text(
            f"Host pf-test-host\n"
            f"    Hostname 127.0.0.1\n"
            f"    Port {port}\n"
            f"    User {os.environ['USER']}\n"
            f"    IdentityFile {private_key}\n"
            f"    StrictHostKeyChecking no\n"
            f"    UserKnownHostsFile /dev/null\n"
            f"    LogLevel ERROR\n"
        )

        sshd_cmd = [
            sshd_bin,
            "-D",
            "-p",
            str(port),
            "-f",
            "/dev/null",
            "-h",
            str(host_key),
            "-o",
            f"AuthorizedKeysFile={authorized_keys}",
            "-o",
            "StrictModes=no",
            "-o",
            "UsePAM=no",
            "-o",
            "PidFile=none",
        ]

        proc = subprocess.Popen(sshd_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.5)

        if proc.poll() is not None:
            stderr = proc.stderr.read().decode() if proc.stderr else ""
            pytest.fail(f"sshd failed to start: {stderr}")
    finally:
        fcntl.flock(lockfile, fcntl.LOCK_UN)
        lockfile.close()

    fixture = SSHDFixture(
        port=port,
        tmpdir=tmpdir,
        config_path=ssh_config,
        sockets_dir=sockets_dir,
        host_key=host_key,
        private_key=private_key,
        process=proc,
    )

    yield fixture

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)
    shutil.rmtree(short_tmpdir, ignore_errors=True)
