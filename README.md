# Latency Monitor

An Omarchy bar widget that answers *"how is my connection doing right now?"* —
ping and jitter to your **default-route gateway** and a **public host**
(default `1.1.1.1`), a bar number for the connection, amber the moment it
degrades, red only when it is truly down, and per-target history sparklines.

Strictly read-only: it runs the system `ping` and reads the routing table.
It never elevates, never writes outside its own state directory, and never
touches your network configuration.

## What you see

**On the bar** (one button): the public host's average round-trip, e.g. `84ms`,
next to an icon whose shape and colour carry the state:

| State | Icon | Colour | Meaning |
|---|---|---|---|
| `ok` | wifi | normal (dimmed when idle) | everything under the thresholds |
| `degraded` | warning triangle | **amber** | a threshold was exceeded — latency, jitter or any packet loss |
| `down` | times-circle | red | the public host does not answer at all |
| `unavailable` | info-circle | dim | could not measure (no `ping`, no default route, helper error) |

The amber colour is theme-adaptable: set `bar.degraded = "#rrggbb"` in your
`shell.toml` to use any colour you like; the fallback is a plain amber.

**In the panel** (click): both targets with avg / min / max / jitter / loss,
colour-coded per target, the degraded reasons in plain words
(`avg 185 ms > 150 ms`), a **history sparkline** per target (last 40 samples,
~40 minutes), the thresholds in plain words, and a measure-now button.

**Right-click on the bar**: measure immediately.

## Why amber, and why a gateway "down" is not red

A gateway that ignores ICMP while traffic flows is a configuration reality —
many routers throttle or drop ping. The widget therefore reports such a
gateway as amber with a note, and reserves red for the one thing it actually
means: the public host is unreachable (your connection is down).

Amber thresholds (defaults, calibrated so a healthy link never shows amber):

- gateway: average > **25 ms**, jitter > **10 ms**
- public host: average > **150 ms**, jitter > **40 ms**
- any packet loss on either target

Jitter is `ping`'s `mdev` — the mean deviation of round-trip times that
iputils prints, the standard RFC 3393-style measure.

## Measurement

Every minute (configurable, floor 1 minute) the helper runs one 4-packet
burst against the gateway (read from `ip route show default`) and one against
the public host, **in parallel**, ~1 s wall time. It parses min / avg / max /
mdev and the loss percentage, classifies against your thresholds, appends one
sample to its history, and prints one JSON document for the panel.

The system `ping` runs unprivileged on Omarchy/Arch (no setuid; kernel ICMP
datagram sockets). The plugin spawns no privileged process and nothing else.

## Install

From Omarchy's plugin marketplace, or:

```
omarchy-plugin install https://github.com/manish0467/omarchy-latency-monitor
```

Then add the widget id to your bar layout if it is not appended automatically:

```json
{ "id": "io.github.manish0467.latency-monitor" }
```

## Configuration

In the widget's entry in `~/.config/omarchy/shell.json` (see
`config.example.json`):

| Key | Default | Meaning |
|---|---|---|
| `publicHost` | `1.1.1.1` | the target the bar's number shows; hostname or IPv4/IPv6 literal |
| `gatewayMs` | `25` | amber threshold for the gateway's average (ms) |
| `gatewayJitterMs` | `10` | amber threshold for the gateway's jitter (ms) |
| `publicMs` | `150` | amber threshold for the public host's average (ms) |
| `publicJitterMs` | `40` | amber threshold for the public host's jitter (ms) |
| `pollMinutes` | `1` | re-measure every N minutes (floor 1) |
| `idleBehavior` | `dim` | `dim` / `hide` / `show` the widget while everything is fine |
| `mock` | `""` | mock scenario name for testing (`normal`, `degraded`, `lossy`, `down`, `gateway-down`, `no-route`, `ping-missing`) |

Invalid settings fall back to their defaults with a note in the panel —
an invalid `publicHost` never reaches a command line.

## Keyboard

- `r` — measure now
- `Esc` — close the panel

## State

`~/.local/state/omarchy/settings/io.github.manish0467.latency-monitor/history.json`
— schema, thresholds and the last 40 samples. Delete the file any time; it is
recreated on the next measurement. Mock runs never touch it.

## Privacy

The helper talks to exactly two addresses (your gateway and the configured
public host) via ICMP, and nowhere else. No HTTP, no telemetry, no DNS
queries beyond resolving your configured host, no files read outside its own
state directory.

## Development

```
python3 -m unittest discover -s tests     # unit + hardening tests
tests/lint.sh                             # qmllint on the QML files
helper/latency.py check --mock normal     # run the helper against a scenario
```

See `docs/DESIGN.md` for the full design document.

## License

MIT — see [LICENSE](LICENSE).
