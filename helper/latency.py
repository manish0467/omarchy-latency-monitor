#!/usr/bin/env python3
"""Read-only latency helper for the io.github.manish0467.latency-monitor widget.

Architecture (docs/DESIGN.md):
  * The panel (QML) runs this script with `check` and reads ONE JSON document
    from stdout. All parsing, classification and history logic is testable
    outside the shell.
  * Measurements: one ping burst (4 packets) to the default-route gateway and
    one to a public host (default 1.1.1.1), run in parallel threads. The
    system `ping` (iputils) runs UNPRIVILEGED on Omarchy/Arch (no setuid;
    kernel ICMP datagram sockets) - the helper elevates nothing and spawns
    nothing else.
  * Jitter is ping's mdev (mean deviation of round-trip times, the standard
    RFC 3393-style measure iputils prints). Amber classification uses fixed,
    documented thresholds (avg, jitter, loss per target); red means the
    public host is down (100% loss).
  * PRIVILEGES: runs as the plain user. The only child processes are `ip
    route show default` (fixed argv, no arguments that vary) and `ping` with
    a fixed flag set and ONE host element - the host comes from settings and
    is validated against ^[A-Za-z0-9._:-]+$ before it enters argv. No shell,
    no sudo/pkexec/polkit, no network use beyond the pings themselves, and
    no writes outside this plugin's state directory.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCHEMA = 1
VERSION = "0.1.2"
PLUGIN_ID = "io.github.manish0467.latency-monitor"

# ---------------------------------------------------------------------------
# Measurement constants (fixed burst; see DESIGN.md §6)
# ---------------------------------------------------------------------------
PING_COUNT = 4
PING_INTERVAL = "0.2"
PING_DEADLINE = "3"                    # integer seconds; this iputils build
                                       # rejects fractional -w values
PING_TIMEOUT = 10.0                    # hard timeout in run_cmd
ROUTE_TIMEOUT = 5.0
HISTORY_LEN = 40                       # samples kept per state file (~40 min)
MAX_ERRORS = 8
MAX_REASON = 120
MAX_LINE = 200                         # any single parsed line is clipped
MAX_BYTES = 64 * 1024                  # ignore ping output beyond this
STATE_FILE = "history.json"

DEFAULT_CONFIG = {
    "publicHost": "1.1.1.1",
    "gatewayMs": 25.0,
    "gatewayJitterMs": 10.0,
    "publicMs": 150.0,
    "publicJitterMs": 40.0,
}

MOCK_DIR = Path(__file__).resolve().parent.parent / "mock"

# A host element in argv: hostnames, IPv4, IPv6 literals. The first
# character must be alphanumeric or ':' (covers '::1') so nothing can ever
# start with '-' and be parsed as a ping flag. Anything else (spaces, shell
# metacharacters) is rejected before it can reach a command.
VALID_HOST = re.compile(r"^[A-Za-z0-9:][A-Za-z0-9._:-]*$")

# --- ping output lines (iputils; EN locale) --------------------------------
RE_LOSS = re.compile(
    r"(\d+) packets transmitted, (\d+) (?:received|errors), "
    r"(\d+(?:\.\d+)?)% packet loss")
RE_RTT = re.compile(
    r"rtt min/avg/max/mdev = "
    r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?) ms")
# --- ip route ---------------------------------------------------------------
RE_VIA = re.compile(r"\bvia\s+([0-9A-Fa-f:.]+)")


# ---------------------------------------------------------------------------
# Subprocess, JSON I/O, text sanitisation
# ---------------------------------------------------------------------------
def run_cmd(argv, timeout):
    """Run argv with a hard timeout, killing the whole process group.

    `start_new_session=True` puts the child in its own process group, so
    `os.killpg` takes down grandchildren too. Returns (stdout, stderr, kind,
    code): kind is "" (ran), "missing" (could not start) or "timeout"; code
    is the exit status (-1 when unknown). Never raises.
    """
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, start_new_session=True)
    except (OSError, ValueError):
        return "", "", "missing", -1
    try:
        out, err = proc.communicate(timeout=timeout)
        return (out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
                "", proc.returncode)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError, TypeError):
            os.killpg(proc.pid, 15)  # SIGTERM to the group
        try:
            out, err = proc.communicate(timeout=2.0)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError, TypeError):
                os.killpg(proc.pid, 9)  # SIGKILL
            out, err, code = b"", b"", -1
            with contextlib.suppress(Exception):
                proc.wait(timeout=2.0)  # reap; never leave a zombie
        return (out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
                "timeout", code)


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def write_json_atomic(path, data):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
                fh.write("\n")
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        os.replace(str(tmp), str(path))
    except OSError:
        pass


def clip(text, limit):
    """Sanitise untrusted text for JSON transport: str, control characters
    collapsed to spaces, stripped, truncated. Returns \"\" for None/non-text."""
    if text is None:
        return ""
    text = "".join(" " if c < " " else c for c in str(text))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "\u2026"
    return text


def _num(value, limit=100000.0):
    """Clamp any parsed float to a sane range; non-numbers -> None."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value < 0 or value != value:      # negative or NaN
        return None
    return min(value, limit)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def parse_route(text):
    """Parse `ip route show default` text. Returns ({via, dev} | None, error)."""
    match = RE_VIA.search(text or "")
    if not match:
        return None, "no default route"
    dev = ""
    m = re.search(r"\bdev\s+(\S+)", text)
    if m:
        dev = clip(m.group(1), 32)
    return {"via": match.group(1), "dev": dev}, None


def default_route():
    """The default-route gateway as an IP literal, or (None, reason).

    `ip route show default` runs with a FIXED argv; nothing user-controlled
    enters it. Returns ({via, dev} | None, error | None).
    """
    out, err, kind, code = run_cmd(["ip", "route", "show", "default"],
                                   ROUTE_TIMEOUT)
    if kind == "missing":
        return None, "ip (iproute2) is not installed"
    if kind == "timeout":
        return None, "ip route timed out"
    if code != 0:
        return None, clip(err or out, MAX_REASON) or "ip route failed"
    return parse_route(out)


def parse_ping_output(text):
    """Parse one `ping` burst. Returns (result, errors) where result has
    loss, min, avg, max, mdev, replied. Missing/blank fields stay None."""
    result = {"loss": 100.0, "min": None, "avg": None, "max": None,
              "mdev": None, "replied": False}
    errors = 0
    out = text[:MAX_BYTES]
    for line in out.split("\n"):
        line = line[:MAX_LINE]
        m = RE_LOSS.search(line)
        if m:
            loss = _num(m.group(3))
            result["loss"] = loss if loss is not None else 100.0
            continue
        m = RE_RTT.search(line)
        if m:
            result["min"], result["avg"], result["max"], result["mdev"] = (
                _num(m.group(1)), _num(m.group(2)),
                _num(m.group(3)), _num(m.group(4)))
            result["replied"] = result["avg"] is not None
    if not RE_LOSS.search(out):
        errors += 1                      # no summary line at all
    return result, errors


def ping_target(host):
    """One burst against one host. Returns (result, error).

    `host` MUST already be validated (VALID_HOST); it is passed as a single
    argv element, never through a shell.
    """
    argv = ["ping", "-c", str(PING_COUNT), "-i", PING_INTERVAL,
            "-w", PING_DEADLINE, host]
    out, err, kind, _code = run_cmd(argv, PING_TIMEOUT)
    if kind == "missing":
        return None, "ping is not installed"
    if kind == "timeout":
        return None, "ping timed out"
    result, errors = parse_ping_output(out)
    if errors and not out.strip():
        # No output at all: resolve failure, permission error, ...
        return None, clip(err.split("\n")[0] if err else "ping produced no output",
                          MAX_REASON)
    return result, None


def classify(result, thresholds):
    """Attach degraded flags + plain-language reasons to one target result."""
    if result is None or not result.get("replied"):
        return
    reasons = []
    if result["avg"] is not None and result["avg"] > thresholds["ms"]:
        reasons.append("avg %.0f ms > %.0f ms"
                       % (result["avg"], thresholds["ms"]))
    if result["mdev"] is not None and result["mdev"] > thresholds["jitter"]:
        reasons.append("jitter %.1f ms > %.1f ms"
                       % (result["mdev"], thresholds["jitter"]))
    if result["loss"] > 0:
        reasons.append("packet loss %.0f%%" % result["loss"])
    result["degraded"] = bool(reasons)
    result["reasons"] = reasons


# ---------------------------------------------------------------------------
# State: history, locking, mock
# ---------------------------------------------------------------------------
def default_state_dir():
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "omarchy" / "settings" / PLUGIN_ID


class Lock:
    """Non-blocking lock so several bars or a shell restart never run the
    helper concurrently. A held lock produces {"busy": true} in the panel."""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / "lock"
        self.handle = None

    def __enter__(self):
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            if Path(self.path).is_symlink():
                raise OSError("lock path is a symlink")
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            self.handle = os.fdopen(fd, "w")
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if self.handle:
                with contextlib.suppress(OSError):
                    self.handle.close()
            self.handle = None
        return self

    def __exit__(self, *exc):
        if self.handle:
            with contextlib.suppress(OSError):
                fcntl.flock(self.handle, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                self.handle.close()
        return False


class Mock:
    """Fixture-based replacement for the real sources. Everything downstream
    (parsing, classification, history, thresholds) runs for real, in an
    isolated state directory."""

    def __init__(self, name):
        if not re.match(r"^[a-z0-9-]+$", name or ""):
            raise ValueError("invalid mock name: %r" % (name,))
        self.dir = MOCK_DIR / name
        if not self.dir.is_dir():
            available = ", ".join(sorted(p.name for p in MOCK_DIR.iterdir()
                                         if p.is_dir())) if MOCK_DIR.is_dir() else "(none)"
            raise ValueError("unknown mock scenario %r; available: %s"
                             % (name, available))
        self.scenario = read_json(self.dir / "scenario.json", {})

    def route(self):
        """Parsed route like default_route() returns, or (None, error) - so
        route parsing runs for real on mock text."""
        if "route_error" in self.scenario:
            return None, self.scenario["route_error"]
        text = self.scenario.get("route")
        if not isinstance(text, str):
            return None, "no default route"
        return parse_route(text)

    def ping(self, key):
        """Simulated ping output for "gateway"/"public", or (None, error)."""
        raw = self.scenario.get(key, {})
        if isinstance(raw, str):
            return raw, None
        if isinstance(raw, dict):
            if raw.get("missing"):
                return None, "ping is not installed"
            if raw.get("timeout"):
                return None, "ping timed out"
            if raw.get("stderr"):
                return None, clip(raw["stderr"], MAX_REASON)
        return "", None


class Context:
    def __init__(self, state_dir, mock_name, config):
        self.state_dir = Path(state_dir)
        self.mock = Mock(mock_name) if mock_name else None
        self.config = config

    def history_path(self):
        return self.state_dir / STATE_FILE


def load_config(raw):
    """Validate + clamp the --config JSON. Invalid values fall back to
    defaults (with an error note) instead of failing the whole check."""
    config = dict(DEFAULT_CONFIG)
    errors = []
    if raw is None:
        return config, errors
    if not isinstance(raw, dict):
        return config, ["--config is not a JSON object; defaults used"]
    for key, default in DEFAULT_CONFIG.items():
        value = raw.get(key)
        if value is None:
            continue
        if key == "publicHost":
            host = clip(value, 253)
            if not VALID_HOST.match(host):
                errors.append("publicHost %r is not a valid host; default %s used"
                              % (host, default))
            else:
                config[key] = host
        else:
            num = _num(value)
            if num is None or num <= 0:
                errors.append("%s %r is not a positive number; default %s used"
                              % (key, value, default))
            else:
                config[key] = num
    return config, errors


def load_history(path):
    data = read_json(path, {})
    samples = data.get("samples")
    if not isinstance(samples, list):
        return []
    out = []
    for item in samples[-HISTORY_LEN:]:
        if isinstance(item, dict) and isinstance(item.get("at"), (int, float)):
            out.append(item)
    return out


def append_history(path, sample):
    """Append one sample (both targets as measured), keep the last HISTORY_LEN.
    Returns (history, saved): the new list and whether the write succeeded."""
    history = load_history(path)
    history.append(sample)
    history = history[-HISTORY_LEN:]
    data = {"schema": SCHEMA, "samples": history}
    try:
        write_json_atomic(path, data)
        saved = isinstance(read_json(path, {}), dict)
    except OSError:
        saved = False
    return history, saved


# ---------------------------------------------------------------------------
# The check document
# ---------------------------------------------------------------------------
def empty_doc():
    return {
        "schema": SCHEMA,
        "state": "unknown",
        "checked_at": None,
        "busy": False,
        "errors": [],
        "gateway": {},
        "public": {},
        "history": [],
        "thresholds": {},
    }


def _target_view(host, result, error, thresholds):
    """The JSON view of one target: state, numbers, degraded reasons."""
    view = {"host": clip(host, 253)}
    if error:
        view.update({"state": "unavailable", "reason": clip(error, MAX_REASON)})
        return view
    if not result["replied"]:
        view.update({"state": "down", "loss": result["loss"],
                     "reason": clip("100%% packet loss" if result["loss"] >= 100
                                    else "%.0f%% packet loss" % result["loss"],
                                    MAX_REASON)})
        return view
    classify(result, thresholds)
    view.update({
        "state": "ok",
        "min": result["min"], "avg": result["avg"], "max": result["max"],
        "mdev": result["mdev"], "loss": result["loss"],
        "degraded": result.get("degraded", False),
        "reasons": [clip(r, MAX_REASON) for r in result.get("reasons", [])],
    })
    return view


def build_doc(ctx):
    doc = empty_doc()
    doc["checked_at"] = time.time()
    errors = []

    thresholds = {
        "gateway": {"ms": ctx.config["gatewayMs"],
                    "jitter": ctx.config["gatewayJitterMs"]},
        "public": {"ms": ctx.config["publicMs"],
                   "jitter": ctx.config["publicJitterMs"]},
    }
    doc["thresholds"] = {
        "gateway": thresholds["gateway"], "public": thresholds["public"]}

    # Gateway from the routing table (fixed argv).
    if ctx.mock:
        route, route_error = ctx.mock.route()
    else:
        route, route_error = default_route()
    gateway_host = route["via"] if route else ""
    if route_error:
        errors.append(clip(route_error, MAX_REASON))

    # Public host from config: validated BEFORE it can enter argv.
    public_host = ctx.config["publicHost"]
    if not VALID_HOST.match(public_host):
        errors.append("publicHost %r is not a valid host; not pinged" % public_host)
        public_host = ""

    # Both bursts in parallel threads; neither can outlive its run_cmd
    # timeout, so the join below cannot hang.
    results = {}
    if ctx.mock:
        for key, host in (("gateway", gateway_host), ("public", public_host)):
            if not host:
                continue                 # default (route_error/no-host) applies
            text, error = ctx.mock.ping(key)
            if error is None:
                result, parse_error = parse_ping_output(text or "")
                results[key] = (result, None) if parse_error == 0 else (result, "no summary line")
            else:
                results[key] = (None, error)
    else:
        def worker(key, host):
            results[key] = (ping_target(host) if host
                            else (None, "no host to ping"))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = []
            if gateway_host:
                futures.append(pool.submit(worker, "gateway", gateway_host))
            if public_host:
                futures.append(pool.submit(worker, "public", public_host))
            for f in futures:
                f.result()

    gateway_result, gateway_error = results.get(
        "gateway", (None, route_error if route_error else "no host to ping"))
    public_result, public_error = results.get("public", (None, "no host to ping"))

    gateway = _target_view(gateway_host or "(no route)", gateway_result,
                           gateway_error, thresholds["gateway"])
    public = _target_view(public_host, public_result, public_error,
                          thresholds["public"])
    doc["gateway"] = gateway
    doc["public"] = public
    if route:
        gateway["dev"] = clip(route.get("dev", ""), 32)

    # Overall state: red only when the connection is really down (public
    # host unreachable); amber on threshold exceedances; a gateway that
    # ignores ICMP while traffic flows is amber with a note, never red.
    if public["state"] == "unavailable":
        overall = "unavailable"
    elif public["state"] == "down":
        overall = "down"
    else:
        degraded = bool(public.get("degraded")) or bool(gateway.get("degraded"))
        degraded = degraded or gateway["state"] in ("down", "unavailable")
        overall = "degraded" if degraded else "ok"

    if overall != "unavailable":
        sample = {"at": doc["checked_at"], "gateway": None, "public": None}
        # Keep any replied sample, degraded or not: the sparkline must show
        # the spike, not a hole.
        if gateway.get("avg") is not None:
            sample["gateway"] = {"avg": gateway["avg"], "mdev": gateway["mdev"]}
        if public.get("avg") is not None:
            sample["public"] = {"avg": public["avg"], "mdev": public["mdev"]}
        history, saved = append_history(ctx.history_path(), sample)
        if not saved:
            errors.append("could not write the history file")
        doc["history"] = history

    doc["state"] = overall
    doc["errors"] = errors[:MAX_ERRORS]
    return doc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    state_dir = default_state_dir()
    mock_name = None
    config_raw = None
    positional = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--state-dir":
            i += 1
            if i >= len(args):
                return usage_error("--state-dir needs a value")
            state_dir = Path(args[i])
        elif arg.startswith("--state-dir="):
            state_dir = Path(arg.split("=", 1)[1])
        elif arg == "--mock":
            i += 1
            if i >= len(args):
                return usage_error("--mock needs a value")
            mock_name = args[i]
        elif arg.startswith("--mock="):
            mock_name = arg.split("=", 1)[1]
        elif arg == "--config":
            i += 1
            if i >= len(args):
                return usage_error("--config needs a value")
            config_raw = args[i]
        elif arg.startswith("--config="):
            config_raw = arg.split("=", 1)[1]
        elif arg == "--version":
            print(VERSION)
            return 0
        elif arg == "check":
            positional.append(arg)
        else:
            return usage_error("unknown argument: %r" % arg)
        i += 1

    if len(positional) != 1:
        return usage_error("usage: latency.py [options] check")

    if mock_name:
        # Mocks must never read or write the real state.
        state_dir = Path(state_dir) / ("mock-" + mock_name)

    try:
        config, config_errors = load_config(
            json.loads(config_raw) if config_raw is not None else None)
    except ValueError:
        return usage_error("invalid --config JSON")
    try:
        ctx = Context(state_dir, mock_name, config)
    except ValueError as exc:
        return usage_error(str(exc))

    with Lock(state_dir) as lock:
        if not lock.handle:
            doc = empty_doc()
            doc["busy"] = True
            doc["checked_at"] = time.time()
        else:
            doc = build_doc(ctx)
            doc["errors"] = (config_errors + doc["errors"])[:MAX_ERRORS]
    json.dump(doc, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def usage_error(message):
    print("latency.py: %s" % message, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
