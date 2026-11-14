# Design: io.github.manish0467.latency-monitor

Status: **SHIPPED (2026-11-15) — published, installed, submitted to marketplace.**

Published: github.com/manish0467/omarchy-latency-monitor (default `main`; commit `820dab7`), annotated tag + release `v0.1.0`.
Installed live on this machine (enabled, bar button at end of `bar.layout.right`).
Marketplace submission: omacom/omarchy-plugin-marketplace#14996 (Category System, Tags System/Bar/Quickshell, full maintainer notes, 5 checklist boxes); OPEN awaiting triage.
Design doc: `~/latency-monitor-DESIGN.md` (also repo `docs/DESIGN.md`).
Target: Omarchy 4.5.0-3, Quickshell "Quattro" shell. Same architecture and
standards as the published btrfs-snapshots and keybindings-cheatsheet plugins
(thin BarWidget, data-owning Panel, stdlib-only Python helper, mocks, tests).

The gap: Omarchy's shell shows network *connectivity* (Wi-Fi panel, speed
test) but never **latency/jitter health** — the thing that makes calls,
SSH and games feel bad long before the link "fails". This plugin answers
"how is my connection doing right now?" at a glance in the bar.

---

## 1. Verified measurement surface (live on this machine, 2026-11-15)

- `/usr/bin/ping` (iputils 20250605) runs **unprivileged** on this system
  (plain `-rwxr-xr-x`, no setuid; kernel ICMP datagram sockets). The plugin
  therefore spawns no privileged anything — it just runs the system `ping`
  the same way a user would.
- A burst `ping -c 4 -i 0.2 -w 2 <host>` takes ~0.6 s and prints everything
  needed in two summary lines:
  - `4 packets transmitted, 4 received, 0% packet loss, time 601ms`
  - `rtt min/avg/max/mdev = 76.053/83.781/94.306/6.715 ms`
  We use **avg** (latency) and **mdev** (jitter — iputils' mean deviation,
  the standard RFC 3393-style measure) plus the **loss percentage**.
- Failure shapes (all verified): 100% loss prints the loss line but no rtt
  line (exit 1); unknown hostname prints a stderr error and no stats
  (exit 2); exit 0 only when every packet came back. We parse the output and
  treat exit codes as secondary.
- Default route (gateway) comes from `ip route show default` —
  `default via <ip> dev <iface> …`, one regex, read-only. On this machine:
  `via 10.63.16.28 dev wlan0`.
- Calibration on this very link (feeds the default thresholds):
  gateway ≈ 6.2 ms avg / 0.7 ms jitter (Wi-Fi), 1.1.1.1 ≈ 84 ms avg /
  6.7 ms jitter. Defaults must sit well above these so a healthy link never
  shows amber.

## 2. What the helper does (stdlib only)

Per refresh (`check`), for **two targets** — the default-route gateway and a
public host (default `1.1.1.1`, user-configurable):

1. Resolve the gateway IP from `ip route show default` (no default route →
   honest "unavailable" per-target state, not an error).
2. Run both ping bursts **in parallel threads** (~1 s wall time total) with
   fixed argv: `ping -c N -i 0.2 -w <N*0.2+1.5> <host>`; N=4.
   Hard timeout via the same `run_cmd` as the sibling helpers (kills the
   process group; never hangs the shell).
3. Parse min/avg/max/mdev/loss per target. Host strings from settings are
   validated against `^[A-Za-z0-9._:-]+$` (covers IPv6 literals); numbers are
   clamped. Output lines are clipped like every other untrusted text.
4. Classify:
   - **ok** — both targets replied, everything under thresholds.
   - **degraded (amber)** — any threshold exceeded on either target:
     avg latency, jitter, or any packet loss (loss > 0 % on the public host
     is the early "connection degrades" signal).
   - **down (red)** — the public host has 100 % loss (connection down).
     A gateway that ignores ICMP while the public host still answers is a
     *configuration reality* (routers throttle ICMP), so it shows amber
     "gateway not answering" + a panel note — never a false red bar.
   - **unavailable (dim)** — helper could not run (ping missing, no route
     at all).
5. Append the sample to a **history** in the state file
   (`~/.local/state/omarchy/settings/io.github.manish0467.latency-monitor/history.json`,
   schema 1, last ~40 samples per target, clipped) — powers the panel
   sparkline and survives shell restarts.
6. Emit ONE JSON document (same shape discipline as the sibling helpers:
   `state`, `checked_at`, `errors[]`, `gateway{}`, `public{}`, `history`,
   `counts`).

## 3. Bar widget

`BarIconButton` + label, exactly like the sibling widgets after the
slot-overflow fix: hidden `Text` measures the label, `slotSize` widens to
fit, label hidden on vertical bars. Label = the **connection gauge**
(see Q1: which ms the bar shows). Colour states:

- ok → normal colour, dim icon
- degraded → amber (Commons amber/urgent tone, mild — matches btrfs's
  "warn" styling discipline: informative, not alarming)
- down → red
- unavailable → dim, tooltip explains

Tooltip: gateway + public summary. Right-click: refresh now.

## 4. Panel

Same skeleton as the sibling panels (KeyboardPanel, PanelKeyCatcher, search
not needed here — it's a status panel, not a list):

- Two target rows: **Gateway** and **Public host** — each with
  avg / min / max / jitter / loss, colour-coded per its own thresholds.
- A small **sparkline** per target drawn from the history (simple
  rectangles, no dependency) — see Q3.
- Footer: thresholds in plain words ("amber when avg > 25 ms, jitter > 10 ms
  or any loss") so the user can reason about why it's amber, plus the last
  check time and a manual refresh button.
- Empty/honest states: no default route, ping missing, busy.

Settings (all optional, defaults documented in `config.example.json`):
`publicHost` (default `1.1.1.1`), `gatewayMs` (25), `gatewayJitterMs` (10),
`publicMs` (150), `publicJitterMs` (40), `pollMinutes` (1, floor 1), `mock`.

## 5. Security / privacy posture

- Runs as the user; spawns only the system `ping` (unprivileged on this
  system) and nothing else. No sudo/pkexec/polkit, ever.
- No writes outside the plugin's own state dir.
- Host strings from settings are the only user input into a command line —
  they are regex-validated and passed as an argv element (no shell).
- No network access by the helper itself (ping is the network access; no
  HTTP, no telemetry).

## 6. Out of scope (v0.1)

- Continuous sub-minute pings (poll floor 1 min; a monitor, not a traffic
  generator).
- Bandwidth/speed measurement (the shell's network panel has a speed test).
- Per-host dashboards, multiple public targets, DNS-resolve metrics.
- Configurable ping count/interval (fixed 4-ping burst; documented).

## 7. Naming

- Plugin id: `io.github.manish0467.latency-monitor`
- Repo: `github.com/manish0467/omarchy-latency-monitor`
- Same git identity ("Manish", `252238345+manish0467@users.noreply.github.com`),
  MIT, v0.1.0.
