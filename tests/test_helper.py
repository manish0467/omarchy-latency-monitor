#!/usr/bin/env python3
"""Unit tests for helper/latency.py.

Run:  python3 -m unittest discover -s tests -v
Also: uv run --no-project --python 3.9 python -m unittest discover -s tests -v

Standard library only; nothing here pings the network or touches the real
state directory - every CLI test uses --mock fixtures and a temporary
--state-dir. The ping fixtures are synthetic output (structure only).
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "helper"))
import latency as lt  # noqa: E402


def run_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = lt.main(list(argv))
    assert code == 0, "helper exited %d: %s" % (code, out.getvalue())
    return json.loads(out.getvalue())


def mock_check(name, tmp=None, extra=()):
    return run_cli("check", "--mock", name, "--state-dir",
                   str(tmp or Path(tempfile.mkdtemp())), *extra)


ROUTE = "default via 10.63.16.28 dev wlan0 proto dhcp src 10.63.16.131 metric 600"

GOOD = ("\n--- 1.1.1.1 ping statistics ---\n"
        "4 packets transmitted, 4 received, 0% packet loss, time 602ms\n"
        "rtt min/avg/max/mdev = 76.053/83.781/94.306/6.715 ms\n")
LOSSY = ("\n--- 1.1.1.1 ping statistics ---\n"
         "4 packets transmitted, 3 received, 25% packet loss, time 601ms\n"
         "rtt min/avg/max/mdev = 80.1/85.0/90.2/4.0 ms\n")
DOWN = ("\n--- 1.1.1.1 ping statistics ---\n"
        "4 packets transmitted, 0 received, 100% packet loss, time 1871ms\n")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
class TestParse(unittest.TestCase):
    def test_good_burst(self):
        result, errors = lt.parse_ping_output(GOOD)
        self.assertEqual(errors, 0)
        self.assertTrue(result["replied"])
        self.assertEqual(result["loss"], 0.0)
        self.assertAlmostEqual(result["avg"], 83.781)
        self.assertAlmostEqual(result["mdev"], 6.715)
        self.assertAlmostEqual(result["min"], 76.053)
        self.assertAlmostEqual(result["max"], 94.306)

    def test_lossy_burst(self):
        result, _ = lt.parse_ping_output(LOSSY)
        self.assertTrue(result["replied"])
        self.assertEqual(result["loss"], 25.0)

    def test_full_loss_no_rtt(self):
        result, errors = lt.parse_ping_output(DOWN)
        self.assertFalse(result["replied"])
        self.assertEqual(result["loss"], 100.0)
        self.assertIsNone(result["avg"])
        self.assertEqual(errors, 0)

    def test_garbage(self):
        result, errors = lt.parse_ping_output("nothing useful here\n")
        self.assertEqual(result["loss"], 100.0)
        self.assertFalse(result["replied"])
        self.assertEqual(errors, 1)

    def test_control_chars_and_huge_values_clipped(self):
        text = ("4 packets transmitted, 4 received, 0% packet loss\n"
                "rtt min/avg/max/mdev = 1/99999999/3/4 ms\n")
        result, _ = lt.parse_ping_output(text)
        self.assertEqual(result["avg"], 100000.0)   # clamped
        self.assertTrue(result["replied"])

    def test_fractional_loss(self):
        text = ("10 packets transmitted, 9 received, 10.0% packet loss\n"
                "rtt min/avg/max/mdev = 1/2/3/0.5 ms\n")
        result, _ = lt.parse_ping_output(text)
        self.assertEqual(result["loss"], 10.0)


# ---------------------------------------------------------------------------
# Route parsing
# ---------------------------------------------------------------------------
class TestRoute(unittest.TestCase):
    def test_regex(self):
        m = lt.RE_VIA.search(ROUTE)
        self.assertEqual(m.group(1), "10.63.16.28")
        m = lt.RE_VIA.search("default via fe80::1 dev eth0")
        self.assertEqual(m.group(1), "fe80::1")     # IPv6 gateway

    def test_no_via(self):
        self.assertIsNone(lt.RE_VIA.search("default dev wlan0 scope link"))


# ---------------------------------------------------------------------------
# Validation and config
# ---------------------------------------------------------------------------
class TestValidation(unittest.TestCase):
    def test_host_regex(self):
        self.assertTrue(lt.VALID_HOST.match("1.1.1.1"))
        self.assertTrue(lt.VALID_HOST.match("gateway.home.arpa"))
        self.assertTrue(lt.VALID_HOST.match("::1"))
        self.assertTrue(lt.VALID_HOST.match("fe80::1%eth0".replace("%eth0", "")))
        for bad in ("a b", "h;rm", "$(x)", "1.1.1.1 && evil", "", "a\tb"):
            self.assertIsNone(lt.VALID_HOST.match(bad), bad)

    def test_config_defaults_and_clamps(self):
        config, errors = lt.load_config(None)
        self.assertEqual(config, lt.DEFAULT_CONFIG)
        self.assertEqual(errors, [])

        config, errors = lt.load_config(
            {"publicHost": "9.9.9.9", "gatewayMs": 40, "publicJitterMs": "55"})
        self.assertEqual(config["publicHost"], "9.9.9.9")
        self.assertEqual(config["gatewayMs"], 40.0)
        self.assertEqual(config["publicJitterMs"], 55.0)
        self.assertEqual(errors, [])

    def test_config_invalid_host_rejected(self):
        config, errors = lt.load_config({"publicHost": "a b"})
        self.assertEqual(config["publicHost"], lt.DEFAULT_CONFIG["publicHost"])
        self.assertEqual(len(errors), 1)

    def test_config_bad_numbers(self):
        config, errors = lt.load_config(
            {"gatewayMs": 0, "publicMs": -5, "gatewayJitterMs": "x"})
        self.assertEqual(config["gatewayMs"], lt.DEFAULT_CONFIG["gatewayMs"])
        self.assertEqual(config["publicMs"], lt.DEFAULT_CONFIG["publicMs"])
        self.assertEqual(config["gatewayJitterMs"], lt.DEFAULT_CONFIG["gatewayJitterMs"])
        self.assertEqual(len(errors), 3)

    def test_config_not_object(self):
        config, errors = lt.load_config([1, 2])
        self.assertEqual(config, lt.DEFAULT_CONFIG)
        self.assertEqual(len(errors), 1)

    def test_config_invalid_json_cli(self):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = lt.main(["check", "--config", "not json",
                            "--state-dir", tempfile.mkdtemp()])
        self.assertEqual(code, 2)
        self.assertIn("invalid --config", err.getvalue())


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
class TestClassify(unittest.TestCase):
    TH = {"ms": 150.0, "jitter": 40.0}

    def test_ok(self):
        result, _ = lt.parse_ping_output(GOOD)
        lt.classify(result, self.TH)
        self.assertFalse(result["degraded"])
        self.assertEqual(result["reasons"], [])

    def test_avg_over(self):
        result, _ = lt.parse_ping_output(
            "rtt min/avg/max/mdev = 100/200/300/5 ms\n"
            "4 packets transmitted, 4 received, 0% packet loss\n")
        lt.classify(result, self.TH)
        self.assertTrue(result["degraded"])
        self.assertIn("avg 200 ms > 150 ms", result["reasons"])

    def test_jitter_over(self):
        result, _ = lt.parse_ping_output(
            "rtt min/avg/max/mdev = 100/120/500/55 ms\n"
            "4 packets transmitted, 4 received, 0% packet loss\n")
        lt.classify(result, self.TH)
        self.assertTrue(result["degraded"])
        self.assertIn("jitter 55.0 ms > 40.0 ms", result["reasons"])

    def test_loss_flags(self):
        result, _ = lt.parse_ping_output(LOSSY)
        lt.classify(result, self.TH)
        self.assertTrue(result["degraded"])
        self.assertIn("packet loss 25%", result["reasons"])

    def test_no_reply_not_classified(self):
        result, _ = lt.parse_ping_output(DOWN)
        lt.classify(result, self.TH)
        self.assertNotIn("degraded", result)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
class TestHistory(unittest.TestCase):
    def test_roundtrip_and_trim(self):
        tmp = Path(tempfile.mkdtemp()) / lt.STATE_FILE
        for i in range(lt.HISTORY_LEN + 10):
            history, saved = lt.append_history(tmp, {"at": i, "public": {"avg": i}})
            self.assertTrue(saved)
        self.assertEqual(len(history), lt.HISTORY_LEN)
        self.assertEqual(history[-1]["at"], lt.HISTORY_LEN + 9)
        self.assertEqual(history[0]["at"], 10)   # trimmed from the front

    def test_load_defensive(self):
        tmp = Path(tempfile.mkdtemp()) / lt.STATE_FILE
        tmp.write_text("garbage")
        self.assertEqual(lt.load_history(tmp), [])
        tmp.write_text(json.dumps({"schema": 1, "samples": [
            {"at": 1, "public": {"avg": 1}}, "junk", {"no_at": True},
            {"at": "x"}, {"at": 2}]}))
        history = lt.load_history(tmp)
        self.assertEqual(len(history), 2)        # only valid samples


# ---------------------------------------------------------------------------
# Full documents via mocks
# ---------------------------------------------------------------------------
class TestCheck(unittest.TestCase):
    def test_normal(self):
        doc = mock_check("normal")
        self.assertEqual(doc["state"], "ok")
        self.assertEqual(doc["gateway"]["state"], "ok")
        self.assertEqual(doc["public"]["state"], "ok")
        self.assertAlmostEqual(doc["public"]["avg"], 84.0)
        self.assertFalse(doc["gateway"]["degraded"])
        self.assertEqual(len(doc["history"]), 1)

    def test_degraded_amber(self):
        doc = mock_check("degraded")
        self.assertEqual(doc["state"], "degraded")
        self.assertTrue(doc["public"]["degraded"])
        self.assertIn("avg 185 ms > 150 ms", doc["public"]["reasons"])

    def test_lossy(self):
        doc = mock_check("lossy")
        self.assertEqual(doc["state"], "degraded")
        self.assertEqual(doc["public"]["loss"], 25.0)
        self.assertIn("packet loss 25%", doc["public"]["reasons"])

    def test_down_is_red(self):
        doc = mock_check("down")
        self.assertEqual(doc["state"], "down")
        self.assertEqual(doc["public"]["state"], "down")
        self.assertEqual(doc["gateway"]["state"], "ok")

    def test_gateway_down_is_amber_not_red(self):
        doc = mock_check("gateway-down")
        self.assertEqual(doc["state"], "degraded")   # connection still up
        self.assertEqual(doc["gateway"]["state"], "down")
        self.assertEqual(doc["public"]["state"], "ok")

    def test_no_route(self):
        doc = mock_check("no-route")
        # The connection itself is fine (public answers); gateway is
        # unavailable with a note.
        self.assertEqual(doc["state"], "degraded")
        self.assertEqual(doc["gateway"]["state"], "unavailable")
        self.assertIn("no default route", doc["gateway"]["reason"])
        self.assertEqual(doc["public"]["state"], "ok")

    def test_ping_missing(self):
        doc = mock_check("ping-missing")
        self.assertEqual(doc["state"], "unavailable")
        self.assertIn("ping is not installed", doc["public"]["reason"])

    def test_thresholds_in_doc(self):
        doc = mock_check("normal", extra=["--config", json.dumps({"gatewayMs": 5})])
        self.assertEqual(doc["thresholds"]["gateway"]["ms"], 5.0)

    def test_custom_thresholds_change_classification(self):
        doc = mock_check("normal", extra=["--config",
                                          json.dumps({"publicMs": 10})])
        self.assertEqual(doc["state"], "degraded")
        self.assertIn("avg 84 ms > 10 ms", doc["public"]["reasons"])

    def test_unknown_mock(self):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = lt.main(["check", "--mock", "nope", "--state-dir",
                            tempfile.mkdtemp()])
        self.assertEqual(code, 2)
        self.assertIn("unknown mock", err.getvalue())

    def test_mock_state_isolated(self):
        # A mock check writes history ONLY inside its isolated dir; the real
        # state dir stays untouched.
        tmp = Path(tempfile.mkdtemp())
        mock_check("normal", tmp)
        self.assertFalse((tmp / lt.STATE_FILE).exists())
        self.assertTrue((tmp / "mock-normal" / lt.STATE_FILE).exists())

    def test_usage_errors(self):
        for argv in (["check", "check"], [], ["bogus"], ["--mock"]):
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = lt.main(argv)
            self.assertEqual(code, 2, argv)

    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(lt.main(["--version"]), 0)
        self.assertEqual(out.getvalue().strip(), lt.VERSION)


# ---------------------------------------------------------------------------
# Busy lock
# ---------------------------------------------------------------------------
class TestBusy(unittest.TestCase):
    def test_busy_doc(self):
        import fcntl
        tmp = Path(tempfile.mkdtemp())
        state_dir = tmp / "mock-normal"
        state_dir.mkdir(parents=True)
        with open(state_dir / "lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            doc = mock_check("normal", tmp)
        self.assertTrue(doc.get("busy"))
        self.assertEqual(doc["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
