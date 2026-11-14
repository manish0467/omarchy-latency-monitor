#!/usr/bin/env python3
"""Hardening tests for helper/latency.py.

The plugin's untrusted inputs are (a) settings values that reach a command
line (only the public host, strictly validated) and (b) ping/route output.
These tests pin the behaviours that keep both benign: fixed process argv,
no path construction from untrusted text, clipping everywhere, bounded
output reads, and defensive state reading that never raises.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "helper"))
import latency as lt  # noqa: E402

HELPER = ROOT / "helper" / "latency.py"


def run_cli(*argv):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = lt.main(list(argv))
    parsed = None
    if out.getvalue().strip():
        try:
            parsed = json.loads(out.getvalue())
        except ValueError:
            parsed = {"_raw": out.getvalue()}
    return code, parsed, err.getvalue()


class TestSpawnedArgv(unittest.TestCase):
    def test_fixed_argv_invariant(self):
        # Document the invariant: user text reaches a command line ONLY as
        # the last element of the ping argv, and only after VALID_HOST.
        source = HELPER.read_text()
        self.assertIn('["ping", "-c", str(PING_COUNT), "-i", PING_INTERVAL,\n'
                      '            "-w", PING_DEADLINE, host]', source)
        self.assertIn('["ip", "route", "show", "default"]', source)
        for bad in ("shell=True", "os.system", "os.popen", "sh -c", "/bin/sh"):
            self.assertNotIn(bad, source)

    def test_host_validation_gates_argv(self):
        # The regex is the gate; these strings must never pass it.
        for bad in ("; rm -rf ~", "1.1.1.1 && x", "a b", "$(id)", "`id`",
                    "x\ny", "-v", "--help"):
            self.assertIsNone(lt.VALID_HOST.match(bad), bad)

    def test_invalid_public_host_never_pinged(self):
        # load_config rejects the host BEFORE anything is spawned and falls
        # back to the default, so the evil string never reaches argv at all.
        code, doc, _err = run_cli("check", "--config",
                                  json.dumps({"publicHost": "1.1.1.1 -c 99999"}),
                                  "--state-dir", tempfile.mkdtemp())
        self.assertEqual(code, 0)
        self.assertIn("is not a valid host; default 1.1.1.1 used",
                      " ".join(doc["errors"]))
        self.assertEqual(doc["public"]["host"], "1.1.1.1")   # default, safe
        self.assertEqual(doc["public"]["state"], "ok")

    def test_dash_host_rejected_by_load_config(self):
        # "-v" would parse as a ping flag if it ever reached argv.
        config, errors = lt.load_config({"publicHost": "-v"})
        self.assertEqual(config["publicHost"], lt.DEFAULT_CONFIG["publicHost"])
        self.assertEqual(len(errors), 1)


class TestNoPathFromUntrusted(unittest.TestCase):
    def test_state_file_name_is_constant(self):
        ctx = lt.Context(Path("/tmp"), None, lt.DEFAULT_CONFIG)
        self.assertEqual(ctx.history_path().name, lt.STATE_FILE)

    def test_state_dir_with_spaces(self):
        tmp = Path(tempfile.mkdtemp())
        weird = tmp / "dir with spaces/../state"
        code, doc, _err = run_cli("check", "--mock", "normal",
                                  "--state-dir", str(weird))
        self.assertEqual(code, 0)
        self.assertIn("state", doc)


class TestBoundedInputs(unittest.TestCase):
    def test_huge_output_ignored_beyond_cap(self):
        text = ("x" * (lt.MAX_BYTES + 10) + "\n"
                "4 packets transmitted, 4 received, 0% packet loss\n"
                "rtt min/avg/max/mdev = 1/2/3/4 ms\n")
        result, _errors = lt.parse_ping_output(text)
        # The summary lines sit past the cap and are ignored -> default loss.
        self.assertEqual(result["loss"], 100.0)

    def test_huge_reason_clipped(self):
        doc_view = lt._target_view("h", None, "r" * 500, {"ms": 1, "jitter": 1})
        self.assertLessEqual(len(doc_view["reason"]), lt.MAX_REASON)


class TestDefensiveState(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_directory_instead_of_file(self):
        (self.tmp / lt.STATE_FILE).mkdir()
        code, doc, _err = run_cli("check", "--mock", "normal",
                                  "--state-dir", str(self.tmp))
        self.assertEqual(code, 0)
        self.assertEqual(doc["state"], "ok")

    def test_binary_garbage_history(self):
        (self.tmp / lt.STATE_FILE).write_text("\x00\x01\x02noise")
        code, doc, _err = run_cli("check", "--mock", "normal",
                                  "--state-dir", str(self.tmp))
        self.assertEqual(code, 0)
        self.assertEqual(len(doc["history"]), 1)   # fresh append survived


class TestProcessHygiene(unittest.TestCase):
    def test_helper_as_subprocess(self):
        tmp = tempfile.mkdtemp()
        proc = subprocess.run(
            [sys.executable, "-I", str(HELPER), "check",
             "--mock", "normal", "--state-dir", tmp],
            capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["state"], "ok")

    def test_no_stray_children_on_timeout(self):
        # run_cmd kills the process group; a sleeping child must not survive.
        # A unique fractional duration keeps pgrep from matching unrelated
        # processes (a bare "sleep 30" could).
        marker = "30.7351"
        _code, _out, kind, _c = lt.run_cmd(
            ["/bin/sh", "-c", "sleep %s & sleep %s" % (marker, marker)],
            timeout=0.5)
        self.assertEqual(kind, "timeout")
        out = subprocess.run(["pgrep", "-f", "sleep " + marker],
                             capture_output=True)
        self.assertNotEqual(out.returncode, 0, "children survived the timeout")

    def test_ping_argv_uses_no_shell(self):
        # Run the REAL helper against a host string that would be deadly in
        # a shell context. The regex stops it before argv, and the doc must
        # stay healthy instead of crashing.
        evil = "1.1.1.1; touch /tmp/pwned-latency-monitor"
        tmp = tempfile.mkdtemp()
        code, doc, _err = run_cli("check", "--config",
                                  json.dumps({"publicHost": evil}),
                                  "--state-dir", tmp)
        self.assertEqual(code, 0)
        self.assertFalse(Path("/tmp/pwned-latency-monitor").exists())
        # The regex gate replaced the host with the default before argv;
        # the default host is pinged normally.
        self.assertEqual(doc["public"]["host"], "1.1.1.1")
        self.assertEqual(doc["public"]["state"], "ok")


if __name__ == "__main__":
    unittest.main()
