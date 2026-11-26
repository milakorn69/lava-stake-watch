import contextlib
import datetime
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lava_stake_watch as lsw  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
ADDR = "lava@1replacewithyourprovideraddressxxxxxxxxxxx"
U = lsw.U


def make_watch(tmp, rest=None):
    """Watch built from config.example.toml with env/state moved into `tmp` and an optional REST URL."""
    with open(os.path.join(ROOT, "config.example.toml")) as f:
        text = f.read()
    text = text.replace('"/etc/lava-stake-watch.env"', json.dumps(os.path.join(tmp, "none.env")))
    text = text.replace('"/var/lib/lava-stake-watch/state.json"', json.dumps(os.path.join(tmp, "state.json")))
    if rest:
        text = "\n".join('rest = ["%s"]' % rest if line.startswith("rest = ") else line for line in text.splitlines())
    text = text.replace("usd_price = true", "usd_price = false")
    path = os.path.join(tmp, "config.toml")
    with open(path, "w") as f:
        f.write(text)
    return lsw.Watch(path, dry_run=True)


def snap(chains=None, delegators=None, commission=10, moniker="MrsML"):
    base = {"stake": 5000 * U, "frozen": False, "jails": 0, "jail_end": 0, "endpoints": ["eu.example.com:443"]}
    return {"moniker": moniker, "commission": commission,
            "chains": {c: {**base, **over} for c, over in (chains or {"LAVA": {}, "COSMOSHUB": {}}).items()},
            "delegators": delegators or {}}


class Helpers(unittest.TestCase):
    def test_format(self):
        self.assertEqual(lsw.lava(12_345_678 * U), "12,345,678")
        self.assertEqual(lsw.signed(-5 * U), "−5")
        self.assertEqual(lsw.signed(5 * U), "+5")
        self.assertEqual(lsw.short("lava@1abcdefghijklmnopqrstuvwxyz"), "lava@1abcd…uvwxyz")
        self.assertEqual(lsw.chain_list(["C", "A", "B"], limit=2), "A, B and 1 more")


class Diff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = make_watch(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_change(self):
        self.assertEqual(self.w.diff(snap(), snap(), 1000), ([], []))

    def test_freeze_jail_stake(self):
        new = snap({"LAVA": {"frozen": True}, "COSMOSHUB": {"jails": 1, "jail_end": 5000}, "SOLANAT": {}})
        lines, events = self.w.diff(snap(), new, 1000)
        text = "\n".join(lines)
        self.assertIn("Staked on SOLANAT", text)
        self.assertIn("Frozen on LAVA", text)
        self.assertIn("Jailed on COSMOSHUB (jail #1)", text)
        self.assertNotIn("Self-stake", text)  # stake changed because a chain was added
        self.assertEqual(len(events), len(lines))

    def test_endpoint_move_grouped(self):
        moved = {"endpoints": ["eu2.example.com:443"]}
        lines, _ = self.w.diff(snap(), snap({"LAVA": moved, "COSMOSHUB": moved}), 1000)
        self.assertEqual(len(lines), 1)
        self.assertIn("COSMOSHUB, LAVA: eu.example.com:443 → eu2.example.com:443", lines[0])

    def test_delegations(self):
        old = snap(delegators={"lava@1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": 5000 * U, "lava@1dust": 3 * U})
        new = snap(delegators={"lava@1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": 1000 * U, "lava@1dust": 3 * U + 500_000,
                               "lava@1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": 200 * U})
        lines, events = self.w.diff(old, new, 1000)
        text = "\n".join(lines)
        self.assertIn("−4,000 LAVA undelegated", text)
        self.assertNotIn("+200", text)  # under min_delegation: daily report only
        self.assertEqual(sorted(e["delta"] for e in events), [-4000 * U, 200 * U])  # dust skipped
        self.assertIn("Total delegated 1,204 LAVA by 3 delegators", text)  # 1,203.5 rounds up

    def test_commission_and_moniker(self):
        lines, _ = self.w.diff(snap(), snap(commission=20, moniker="New <name>"), 1000)
        self.assertIn("⚙️ Commission 10% → 20%", lines)
        self.assertIn("🏷 Moniker MrsML → New &lt;name&gt;", lines)


class FakeLava(http.server.BaseHTTPRequestHandler):
    rewards_now, rewards_old = 1000, 900
    entries = []
    delegations = []
    chains = [{"chainID": "LAVA", "chainName": "Lava mainnet"}]

    def do_GET(self):
        p = self.path
        if p.startswith("/lavanet/lava/pairing/provider/"):
            body = {"stakeEntries": self.entries}
        elif p.startswith("/lavanet/lava/dualstaking/provider_delegators/"):
            body = {"delegations": self.delegations}
        elif p == "/lavanet/lava/spec/show_all_chains":
            body = {"chainInfoList": self.chains}
        elif p.startswith("/cosmos/base/tendermint/v1beta1/blocks/"):
            h = 20000 if p.endswith("latest") else int(p.rsplit("/", 1)[1])
            t = datetime.datetime(2026, 11, 1) + datetime.timedelta(seconds=6 * h)
            body = {"block": {"header": {"height": str(h), "time": t.strftime("%Y-%m-%dT%H:%M:%SZ")}}}
        elif p.startswith("/lavanet/lava/subscription/estimated_provider_rewards/"):
            old = self.headers.get("x-cosmos-block-height")
            body = {"total": [{"denom": "ulava", "amount": str((self.rewards_old if old else self.rewards_now) * U)}]}
        else:
            self.send_error(404)
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def entry(chain, frozen=False):
    return {"chain": chain, "stake": {"amount": str(5000 * U)}, "jails": "0", "jail_end_time": "0",
            "stake_applied_block": str(lsw.FROZEN if frozen else 100), "delegate_commission": "10",
            "description": {"moniker": "MrsML"}, "endpoints": [{"iPPORT": "eu.example.com:443"}]}


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLava)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.rest = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        FakeLava.entries = [entry("LAVA"), entry("COSMOSHUB")]
        FakeLava.delegations = [{"delegator": ADDR, "amount": {"amount": str(5000 * U)}},
                                {"delegator": "lava@1delegatorxxxxxxxxxxxxxxxxxxxxxxxxxxx", "amount": {"amount": str(2000 * U)}}]

    def tearDown(self):
        self.tmp.cleanup()

    def run_watch(self, daily=False):
        w = make_watch(self.tmp.name, self.rest)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            w.daily() if daily else w.check()
        return out.getvalue()

    def test_first_run_then_freeze(self):
        first = self.run_watch()
        self.assertIn("is watching", first)
        self.assertIn("2 chains active", first)
        self.assertIn("2,000 LAVA delegated by 1 delegators", first)  # own stake is not a delegation
        self.assertEqual(self.run_watch(), "")  # nothing changed
        FakeLava.entries = [entry("LAVA", frozen=True), entry("COSMOSHUB")]
        FakeLava.chains = FakeLava.chains + [{"chainID": "SOLANAT", "chainName": "Solana testnet"}]
        second = self.run_watch()
        self.assertIn("Frozen on LAVA", second)
        self.assertIn("New chain on Lava: <b>Solana testnet</b> (SOLANAT)", second)
        self.assertIn("Not staked yet: MrsML", second)
        FakeLava.chains = FakeLava.chains[:1]

    def daily_rewards_line(self, now, old):
        FakeLava.rewards_now, FakeLava.rewards_old = now, old
        try:
            self.run_watch()
            report = self.run_watch(daily=True)
        finally:
            FakeLava.rewards_now, FakeLava.rewards_old = 1000, 900
        self.assertIn("MrsML</b> · last 24 hours", report)
        return [line for line in report.splitlines() if line.startswith("Estimated rewards")][0]

    def test_daily_rewards(self):
        self.assertEqual(self.daily_rewards_line(1000, 900), "Estimated rewards this month: 1,000 LAVA, +100 LAVA in 24h")
        # the estimate can drift down a little; that is not a payout
        self.assertEqual(self.daily_rewards_line(34909, 34933), "Estimated rewards this month: 34,909 LAVA, −24 LAVA in 24h")
        self.assertEqual(self.daily_rewards_line(120, 35000), "Estimated rewards this month: 120 LAVA, new rewards month started")


if __name__ == "__main__":
    unittest.main()
