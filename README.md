# pf

SSH ControlMaster socket manager. Authenticate once, connect instantly from every terminal pane, VS Code window, scp, and rsync. The only downside is that if you do a lot of IO, some of your connections might be slower since they share the same socket.

NOTE: This is implemented entirely by Claude Code w/ Opus 4.7 thinking xhigh and only integration tested with minimal code review.

## Install

```bash
pip install -e .

# If you want pf available without activating the env:
# alias pf='/opt/miniconda3/envs/saescoping/bin/pf'   # add to ~/.bashrc / ~/.zshrc
```

## Commands

```bash
# Open a master connection (authenticates once)
pf open align3
pf open align3 -pf 8080 -pt 8080          # with local port forward
pf open align3 -u otheruser               # override SSH config user

# Add port forward to existing master (no re-auth)
pf forward align3 -pf 9090 -pt 9090

# Run a command through the socket
pf exec align3 echo hello
pf exec align3 nvidia-smi

# List active sockets
pf list                                     # host + status (alive/stale)

# Detailed info
pf info                                     # full details for all sockets
pf info align3                              # full details for one host
pf info --short                             # same as `pf list`

# Close connections
pf close align3                             # close one
pf close --all                              # close everything
pf close --stale                            # remove only stale sockets
pf reap                                     # alias for close --stale

# View SSH config
pf config                                   # list host names
pf config --full                            # full config in pager
pf config --name align3                     # config for one host
```

## How it works

`pf open` runs `ssh -fNM` to create a background master process with a Unix socket at `~/.ssh/pf_sockets/`. Subsequent SSH connections to the same host reuse this socket — no authentication, instant connect.

The master process is independent of any terminal. Closing shells, tabs, or terminal windows does not affect it. It persists until `pf close`, a network drop, or `kill -9`.

Socket naming uses the SSH config alias (`%n`), not the resolved hostname, so `align3` and `align3-jupyter` get independent sockets even if they point to the same machine.

## Testing

### Automated tests (key-based auth, no password)

```bash
pytest tests/ -v
```

Starts a local sshd on a random port with a throwaway keypair. Fully isolated — no system config read or modified, no root needed, works on macOS and Linux. See `tests/conftest.py` docstring for safety details.

### Manual password-bypass test
 > WARNING: this does not work right now. It should be fixed in a future version.

```bash
chmod +x examples/manual_password_test.sh
./examples/manual_password_test.sh
```

Starts a local sshd requiring password auth, then walks you through:
1. Verify connection fails without password (BatchMode=yes)
2. Open master (type your password once)
3. Verify connection succeeds through socket with no password
4. Close socket, verify password is required again

Requires your system user password. Cleans up automatically on exit.

## Side effects and manual cleanup

Things `pf` creates on your system:

- **`~/.ssh/pf_sockets/`** — directory (mode 0700) where Unix domain socket files live. Created on first `pf open`. One socket file per active connection, named `{user}@{config_name}-{port}`.
- **Background `ssh` processes** — each `pf open` spawns a detached `ssh -fNM` process. These survive shell exits, terminal closes, and logout. They persist until `pf close`, `kill`, network drop, or system reboot.
- **No config files are modified** — `pf` never writes to `~/.ssh/config` or any system file. It only reads SSH config.

If you need to manually nuke everything (assumes you don't run `ssh -fNM` manually outside of `pf` — if you do, use the targeted variant):

```bash
# 1. Kill all pf master processes (targets only sockets in the pf directory)
pkill -f 'ssh.*-S.*pf_sockets'

# 2. Remove all socket files
rm -rf ~/.ssh/pf_sockets

# 3. Verify nothing is left
ps aux | grep '[s]sh.*pf_sockets'
```

If a socket file exists but the master process is dead (stale socket), `pf open` will auto-clean it. You can also run `pf reap` to remove all stale sockets without killing live ones.
