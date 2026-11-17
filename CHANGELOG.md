# Changelog

## 0.1.0 — 2026-11-15

First release.

- Bar button: public-host average (e.g. `84ms`) with state icon; amber when a
  threshold is exceeded, red only when the public host is unreachable.
- Panel: gateway and public host with avg / min / max / jitter / loss, plain
  reasons for the amber state, per-target history sparkline (last 40 samples),
  thresholds in plain words, measure-now button.
- Helper (stdlib-only Python): parallel 4-packet bursts to the default-route
  gateway and a configurable public host, RFC 3393-style jitter from ping's
  mdev, fixed documented thresholds, loss classification, file-locked checks,
  history state file, mock scenarios for every state.
- Settings: public host, per-target latency/jitter thresholds, poll interval,
  idle behaviour, theme-adaptable amber via `bar.degraded` in shell.toml.

## 0.1.1 — 2026-11-15

- Hardening: history.json temp file is now chmod 0600 before rename (umask-independent), matching the keybindings plugin's fix.

## 0.1.2 — 2026-11-15

- Security: temp file and lock file now created 0600 via os.open(O_NOFOLLOW), closing the symlink-following/truncation vector reported by HANCORE-linux.

## 0.1.3 — 2026-11-17

- Security: `O_NOFOLLOW` only protects the final path component, so a symlink planted in a parent of the nonsticky state hierarchy could still redirect the truncating `lock` / `history.json.tmp` opens onto an unrelated file. Added `open_dir_no_symlinks()`: it walks from `/` with openat-style `dir_fd` and `O_NOFOLLOW | O_DIRECTORY` at every component, refusing any symlink with no check-then-use window. Both the lock open and the temp-file open now go through it, and the final rename uses the same pinned directory fd. (issue #14996, HANCORE-linux)
- Hardening: `write_json_atomic` also fchmods the temp descriptor to 0600 before writing, so a leftover world-readable temp file from an interrupted older save cannot expose new contents.
- Tests: +4 hardening tests covering a symlinked parent of the state dir, a symlink higher in the hierarchy, and the reused-temp permission case.
