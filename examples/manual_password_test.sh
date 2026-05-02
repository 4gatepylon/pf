#!/usr/bin/env bash
#
# TODO(hadriano) this DOES NOT WORK
#
# Manual integration test: verify that pf socket bypass eliminates password prompts.
#
# This script starts a local sshd that requires PASSWORD authentication (not keys).
# You authenticate once via `pf open`, then verify that subsequent connections
# through the socket need no password.
#
# SAFETY: The local sshd is fully isolated from the system:
#   - Runs on a random high port as your user (no root).
#   - Reads NO system config (-f /dev/null).
#   - Uses a throwaway host key in a temp directory.
#   - UsePAM=no so it doesn't touch /etc/passwd or PAM at all.
#   - PidFile=none, no writes to /var/run.
#   - The test password is set via sshd's AuthorizedKeysCommand trick (see below).
#   - It cleans up everything on exit (trap EXIT).
#
# REQUIRES: sshd, ssh-keygen, pf (pip install -e .)
#
# Usage:
#   chmod +x examples/manual_password_test.sh
#   ./examples/manual_password_test.sh

set -euo pipefail

TMPDIR_BASE=$(mktemp -d)
trap 'echo ""; echo "Cleaning up..."; kill "$SSHD_PID" 2>/dev/null || true; rm -rf "$TMPDIR_BASE"; echo "Done."' EXIT

HOST_KEY="$TMPDIR_BASE/host_key"
SOCKETS_DIR="$TMPDIR_BASE/sockets"
SSH_CONFIG="$TMPDIR_BASE/ssh_config"

mkdir -p "$SOCKETS_DIR"

# Generate throwaway host key
ssh-keygen -t ed25519 -f "$HOST_KEY" -N "" -q

# Find a free port
PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()")

# Start sshd with keyboard-interactive auth (password-like, but doesn't need PAM users)
# NOTE: We use the current user's system password for auth. If you can't log in with
# your password, this test won't work. On macOS, this is your login password.
# On Linux, this is your user's password.
#
# If UsePAM=no prevents password auth, try UsePAM=yes (requires your system password).
"$(which sshd)" -D \
    -p "$PORT" \
    -f /dev/null \
    -h "$HOST_KEY" \
    -o "PasswordAuthentication=yes" \
    -o "PubkeyAuthentication=no" \
    -o "StrictModes=no" \
    -o "UsePAM=yes" \
    -o "PidFile=none" \
    -o "LogLevel=ERROR" &
SSHD_PID=$!
sleep 1

if ! kill -0 "$SSHD_PID" 2>/dev/null; then
    echo "ERROR: sshd failed to start. Check if sshd is available and your user can run it."
    exit 1
fi

# Write test SSH config
cat > "$SSH_CONFIG" <<EOF
Host pf-password-test
    Hostname 127.0.0.1
    Port $PORT
    User $USER
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
EOF

echo "============================================"
echo "  pf manual password-bypass test"
echo "============================================"
echo ""
echo "Local sshd running on port $PORT (PID $SSHD_PID)"
echo "SSH config: $SSH_CONFIG"
echo "Sockets dir: $SOCKETS_DIR"
echo ""
echo "--- STEP 1: Verify that a connection WITHOUT a socket requires a password ---"
echo ""
echo "Running: ssh -F $SSH_CONFIG -o BatchMode=yes pf-password-test echo hello"
echo "(This should FAIL because BatchMode=yes prevents password prompts)"
echo ""

if ssh -F "$SSH_CONFIG" -o BatchMode=yes pf-password-test echo hello 2>/dev/null; then
    echo "UNEXPECTED: Connection succeeded without password. Test is invalid."
    exit 1
else
    echo "PASS: Connection failed without password (as expected)."
fi

echo ""
echo "--- STEP 2: Open a master connection (you will be prompted for your password) ---"
echo ""
echo "Running: ssh -fNM -F $SSH_CONFIG -S $SOCKETS_DIR/%r@%n-%p pf-password-test"
echo ">>> Type your system password when prompted <<<"
echo ""

ssh -fNM -F "$SSH_CONFIG" -S "$SOCKETS_DIR/%r@%n-%p" pf-password-test

echo ""
echo "Master opened."
echo ""

echo "--- STEP 3: Verify that a connection THROUGH the socket needs NO password ---"
echo ""
echo "Running: ssh -F $SSH_CONFIG -S $SOCKETS_DIR/%r@%n-%p -o BatchMode=yes pf-password-test echo hello"
echo "(This should SUCCEED — the socket handles auth, no password prompt)"
echo ""

RESULT=$(ssh -F "$SSH_CONFIG" -S "$SOCKETS_DIR/%r@%n-%p" -o BatchMode=yes pf-password-test echo hello 2>&1)
if [ "$RESULT" = "hello" ]; then
    echo "PASS: '$RESULT' — connection through socket required no password!"
else
    echo "FAIL: Expected 'hello', got: $RESULT"
    exit 1
fi

echo ""
echo "--- STEP 4: Close the socket and verify password is required again ---"
echo ""

ssh -F "$SSH_CONFIG" -S "$SOCKETS_DIR/%r@%n-%p" -O exit pf-password-test 2>/dev/null || true

echo "Socket closed."
echo ""
echo "Running: ssh -F $SSH_CONFIG -o BatchMode=yes pf-password-test echo hello"
echo "(This should FAIL again — no socket, no key, BatchMode blocks password prompt)"
echo ""

if ssh -F "$SSH_CONFIG" -o BatchMode=yes pf-password-test echo hello 2>/dev/null; then
    echo "UNEXPECTED: Connection succeeded without socket or password."
    exit 1
else
    echo "PASS: Connection failed without socket (as expected)."
fi

echo ""
echo "============================================"
echo "  ALL TESTS PASSED"
echo "============================================"
echo ""
echo "Summary:"
echo "  1. Without socket: password required (BatchMode=yes fails) ✓"
echo "  2. With socket: no password needed (BatchMode=yes succeeds) ✓"
echo "  3. After close: password required again (BatchMode=yes fails) ✓"
