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
