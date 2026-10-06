# Windows password pushes: a ConPTY password mode, and the mod_sftp case — design

Date: 2026-10-06
Status: design written; awaiting review before implementation
Release target: 2026.10.12
Builds on: `2026-09-29-keepwatch-design.md` (transfers) and 2026.10.8 (`resume`, `protocol` on the pull recipe)

## 1. Purpose

Issue #6: on a corporate Windows machine the password destination (mod_sftp, nonstandard port) refuses every
programmatic password path keepwatch has. `SSH_ASKPASS` output is rejected by the server while the **same bytes
typed into a console** succeed — three scp builds, password and keyboard-interactive, LF/no-LF/UTF-16, all
refused; ConPTY keystrokes work. Today the workaround is a ~100-line `watch.py` using pywinpty, which is a lot
of machinery for "type the password".

This design turns that workaround into a setting, and documents the mod_sftp case so the next person spends
minutes rather than hours on it.

Facts from the issue (2026-10-06):

- `identity` and SFTP on Windows already work; the gap is a destination where keys are impossible and askpass
  is refused.
- A server-side `authorized_keys` may be ignored by mod_sftp unless admins set `SFTPAuthorizedUserKeys`, so a
  key path that "should" work may not.

**Non-goals:** changing the askpass path (it works where it works), SSH library support (`asyncssh` is a test
dependency, not a runtime one), and anything for POSIX — a pty on POSIX is `pty.openpty`, and the same
`password_mode = "conpty"` could serve it, but nobody has asked and it cannot be tested here.

## 2. The setting

`password_mode = "askpass" | "conpty"` alongside `password_env`, on every transfer that takes `password_env`
(`ctx.transfer.copy/pull/push`, `keepwatch kit copy/pull/push`, and the `push` recipe's `[settings]`).
Default `"askpass"`: exactly today's behaviour, byte for byte.

Rules, checked where the other transfer options are checked (`ScpOptions.__post_init__` and the recipe's
cross-checks):

- `password_mode = "conpty"` needs `password_env` (there is no password to type otherwise).
- It is refused on POSIX **for now** with a message saying so, rather than silently doing something untested.
- It does not care about `protocol`: the pty runs whatever scp command the options already built.

## 3. How the ConPTY path works

One new module, `src/keepwatch/conpty.py` (Windows only, imported lazily):

- `run(argv, password, *, deadline, env) -> (exit_code, stdout, stderr)`: spawn `argv` with
  `winpty.PtyProcess.spawn(argv, env=...)`, read the pty until the output contains a password prompt
  (`password:` / `Password for` / `'s password:`, case-insensitive), write `password + "\r"`, then keep reading
  until the process exits or the deadline passes (then kill it and raise). Everything read is returned, so the
  transfer still logs one `command` record like it does today.
- **The password never reaches the log**: even though ssh turns the tty's echo off before reading a password,
  the captured output is scrubbed (every occurrence of the password replaced with `***`) before it is returned.
  This is belt and braces, and it is tested.
- The environment is the caller's (`_transfer_env`), so `password_env`'s variable and everything else is
  unchanged; only *how* the password reaches scp differs.
- If `pywinpty` is not installed, the failure is one clear line naming the extra to install — never an
  ImportError traceback in a hook's log.

`transfer._run` gains the branch: when `options.password_mode == "conpty"`, it calls `conpty.run` instead of
`subprocess` and returns the same 4-tuple `(returncode, stdout, stderr, timed_out)`, so every caller (`copy`,
`_sftp_reget`, `push`, and the recipes on top of them) is untouched.

## 4. The dependency

`pywinpty` ships Windows wheels only, so it is **an optional extra**, not a hard dependency (a hard one would
break every Linux install):

```toml
[project.optional-dependencies]
conpty = ["pywinpty>=2; sys_platform == 'win32'"]
```

`uv tool install 'keepwatch[conpty]'` on the corporate Windows machine; nothing changes elsewhere. The docs say
exactly this, next to the `password_env` documentation.

## 5. Tests

- `tests/test_conpty.py` (runs anywhere, with a fake `winpty` module injected): the prompt is detected and the
  password is written with a carriage return; output that contains the password comes back scrubbed; no prompt
  inside the deadline raises a clear `TransferFailed`; a pty that dies raises `CommandFailed` with its output;
  a missing `pywinpty` says which extra to install.
- `tests/test_transfer.py`: `ScpOptions` refuses `password_mode = "conpty"` without `password_env` and on POSIX;
  the `askpass` default is byte-for-byte today's argv and environment (already covered, kept as the guard).
- `tests/test_config_recipe.py` (or wherever the recipe cross-checks live): `password_mode = "conpty"` needs
  `password_env` in a recipe too.
- **What CI cannot do**: type a password at a real server over ConPTY. The Windows runner has no such server,
  so the integration proof is the user's own machine — the release notes will say so, and `keepwatch kit copy`
  with `--password-mode conpty` is the one-line way to try it.

## 6. Documentation

- `keepwatch docs transfers`: a new subsection — *passwords that the server refuses*: some servers (mod_sftp)
  reject askpass-delivered passwords while accepting typed ones; `password_mode = "conpty"` (and its extra) is
  the workaround; a server-side `authorized_keys` may be ignored unless `SFTPAuthorizedUserKeys` is set; and a
  reminder that a password in an environment variable is the only thing keepwatch stores.
- `keepwatch docs recipes`: the `push` settings table gains `password_mode` (generated from the config keys).
- README: one line under Install for the extra.

## 7. Decisions taken (say if you disagree)

1. **Optional extra `keepwatch[conpty]`** rather than a hard dependency: pywinpty has no Linux wheels, so a hard
   dependency would break every non-Windows install.
2. **POSIX is refused for now**, with a message; the same design would work through `pty.openpty`, but it is
   untested here and nobody asked.
3. **The captured output is always scrubbed** of the password before it is logged or raised in an error.
4. `password_mode` goes on all three surfaces (`ctx.transfer`, `keepwatch kit`, the `push` recipe) rather than
   only the recipe, because the issue's own workaround is a `watch.py` using `ctx.transfer`.
