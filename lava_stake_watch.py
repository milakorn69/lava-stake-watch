#!/usr/bin/env python3
"""lava-stake-watch: Telegram alerts about your Lava provider's state on chain.

    lava_stake_watch.py [-c config.toml] [--dry-run]           check, every few minutes
    lava_stake_watch.py [-c config.toml] [--dry-run] --daily   24h report

Check run, for every [[provider]]:
  * frozen and unfrozen chains, jails, chains staked or unstaked;
  * endpoint changes, grouped when one host moves for many chains;
  * delegations in and out (above min_delegation), commission and self-stake changes;
  * new chain specs on Lava (checked once for all providers).
The first run records the current state silently and sends one summary.
Daily run: chains, stake, delegators, delegation moves, the 24h change in estimated
rewards and everything that happened in the last 24 hours.

Standard library only (Python 3.11+). Secrets come from the environment or env_file:
TG_TOKEN and TG_CHAT.
"""
import argparse, html, json, os, sys, time, tomllib, urllib.parse, urllib.request, zoneinfo
from datetime import datetime, timezone

UA = {"User-Agent": "lava-stake-watch/1.0"}
FROZEN = 2**63 - 1  # stake_applied_block of a frozen stake entry
U = 1_000_000       # ulava per LAVA
DAY = 86400


def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    env.update({k: v for k, v in os.environ.items() if k in ("TG_TOKEN", "TG_CHAT")})
    return env


def lava(ulava):
    return f"{ulava / U:,.0f}"


def signed(ulava):
    return ("+" if ulava > 0 else "−" if ulava < 0 else "") + lava(abs(ulava))


def short(addr):
    return addr[:10] + "…" + addr[-6:]


def chain_list(chains, limit=8):
    chains = sorted(chains)
    more = f" and {len(chains) - limit} more" if len(chains) > limit else ""
    return ", ".join(chains[:limit]) + more


class Watch:
    def __init__(self, path, dry_run):
        with open(path, "rb") as f:
            self.cfg = tomllib.load(f)
        s = self.cfg.get("settings", {})
        self.rest = s.get("rest", ["https://lava-api.polkachu.com", "https://lava.rest.lava.build"])
        self.rest = [self.rest] if isinstance(self.rest, str) else self.rest
        self.state_file = s.get("state_file", "/var/lib/lava-stake-watch/state.json")
        self.tz_name = s.get("timezone", "UTC")
        try:
            self.tz = timezone.utc if self.tz_name == "UTC" else zoneinfo.ZoneInfo(self.tz_name)
        except zoneinfo.ZoneInfoNotFoundError:
            sys.exit(f"unknown timezone {self.tz_name!r} (on Debian: apt install tzdata)")
        self.min_deleg = s.get("min_delegation", 1000) * U
        self.min_stake = s.get("min_stake_change", 1000) * U
        self.watch_specs = s.get("new_specs", True)
        self.fail_after = s.get("fail_alert_after", 3)
        self.price = s.get("usd_price", True)
        self.env = load_env(s.get("env_file", "/etc/lava-stake-watch.env"))
        self.dry_run = dry_run
        self.providers = self.cfg.get("provider", [])
        if not self.providers:
            sys.exit("no [[provider]] in the config")
        if not dry_run and not (self.env.get("TG_TOKEN") and self.env.get("TG_CHAT")):
            sys.exit("TG_TOKEN and TG_CHAT must be set (environment or env_file); use --dry-run to test")

    # ---- output and state ----

    def send(self, text):
        if self.dry_run:
            print(text, end="\n\n")
            return
        data = urllib.parse.urlencode({"chat_id": self.env["TG_CHAT"], "text": text, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.env['TG_TOKEN']}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()

    def load_state(self):
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def save_state(self, state):
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, self.state_file)

    def local_time(self, ts):
        return datetime.fromtimestamp(ts, self.tz).strftime("%d.%m %H:%M") + f" {self.tz_name}"

    # ---- chain queries ----

    def get(self, path, height=None):
        """GET from the first REST endpoint that answers; a height reads historical state."""
        headers = dict(UA)
        if height:
            headers["x-cosmos-block-height"] = str(height)
        err = None
        for base in self.rest:
            for attempt in range(2):
                try:
                    with urllib.request.urlopen(urllib.request.Request(base + path, headers=headers), timeout=30) as r:
                        j = json.load(r)
                    # some gateways answer 200 with only an error message, e.g. for historical heights
                    if isinstance(j, dict) and "message" in j and len(j.keys() - {"code", "message", "details"}) == 0:
                        raise RuntimeError(f"{base}: {j['message']}")
                    return j
                except Exception as e:
                    err = e
                    time.sleep(1 + attempt)
        raise RuntimeError(f"{path}: {err}")

    def specs(self):
        return {c["chainID"]: c["chainName"] for c in self.get("/lavanet/lava/spec/show_all_chains")["chainInfoList"]}

    def snapshot(self, addr):
        entries = self.get(f"/lavanet/lava/pairing/provider/{addr}/").get("stakeEntries") or []
        delegs = self.get(f"/lavanet/lava/dualstaking/provider_delegators/{addr}").get("delegations") or []
        first = entries[0] if entries else {}
        return {
            "moniker": (first.get("description") or {}).get("moniker") or first.get("moniker") or "",
            "commission": int(first.get("delegate_commission") or 0) if entries else None,
            "chains": {e["chain"]: {
                "frozen": int(e.get("stake_applied_block") or 0) == FROZEN,
                "jails": int(e.get("jails") or 0),
                "jail_end": int(e.get("jail_end_time") or 0),
                "stake": int(e["stake"]["amount"]),
                "endpoints": sorted(ep["iPPORT"] for ep in e.get("endpoints") or []),
            } for e in entries},
            # the provider's own stake is listed as a delegation to itself, and emptied delegations stay as dust
            "delegators": {d["delegator"]: int(d["amount"]["amount"]) for d in delegs
                           if d["delegator"] != addr and int(d["amount"]["amount"]) >= U},
        }

    def name(self, p, snap):
        return p.get("name") or snap.get("moniker") or short(p["address"])

    # ---- check run ----

    def diff(self, old, new, now):
        """Compare two snapshots of one provider -> (alert lines, events for the daily report)."""
        lines, events = [], []
        oc, nc = old["chains"], new["chains"]

        def note(text, alert=True):
            events.append({"t": now, "text": text})
            if alert:
                lines.append(text)

        added, removed = set(nc) - set(oc), set(oc) - set(nc)
        if added:
            note(f"➕ Staked on {chain_list(added)}")
        if removed:
            note(f"➖ No longer staked on {chain_list(removed)}")

        both = set(nc) & set(oc)
        frozen = [c for c in both if nc[c]["frozen"] and not oc[c]["frozen"]]
        thawed = [c for c in both if oc[c]["frozen"] and not nc[c]["frozen"]]
        if frozen:
            note(f"🧊 Frozen on {chain_list(frozen)}")
        if thawed:
            note(f"✅ Unfrozen on {chain_list(thawed)}, back in pairing from the next epoch")
        for c in sorted(both):
            if nc[c]["jails"] > oc[c]["jails"]:
                until = f" until {self.local_time(nc[c]['jail_end'])}" if nc[c]["jail_end"] > now else ", already released"
                note(f"⛔ Jailed on {c} (jail #{nc[c]['jails']}){until}")

        # one host moving for many chains is one message, not twenty
        moves = {}
        for c in both:
            if nc[c]["endpoints"] != oc[c]["endpoints"]:
                moves.setdefault((tuple(oc[c]["endpoints"]), tuple(nc[c]["endpoints"])), []).append(c)
        for (was, now_eps), chains in moves.items():
            note(f"🔧 Endpoint on {chain_list(chains)}: {', '.join(was) or '—'} → {', '.join(now_eps) or '—'}")

        if old.get("commission") is not None and new.get("commission") != old.get("commission"):
            note(f"⚙️ Commission {old['commission']}% → {new['commission']}%")
        if old.get("moniker") and new.get("moniker") != old.get("moniker"):
            note(f"🏷 Moniker {html.escape(old['moniker'])} → {html.escape(new['moniker'])}")

        ds = sum(v["stake"] for v in nc.values()) - sum(v["stake"] for v in oc.values())
        if not added and not removed and abs(ds) >= self.min_stake:
            note(f"🪙 Self-stake {signed(ds)} LAVA, now {lava(sum(v['stake'] for v in nc.values()))} LAVA")

        od, nd = old["delegators"], new["delegators"]
        for who in sorted(set(od) | set(nd)):
            delta = nd.get(who, 0) - od.get(who, 0)
            if abs(delta) < U:  # dust under 1 LAVA, e.g. reward claims
                continue
            events.append({"t": now, "deleg": who, "delta": delta})
            if abs(delta) >= self.min_deleg:
                verb = "delegated" if delta > 0 else "undelegated"
                tail = "" if who in nd else " (left)"
                lines.append(f"{'💰' if delta > 0 else '💸'} {signed(delta)} LAVA {verb} by <code>{short(who)}</code>{tail}")
        if any("deleg" in e and abs(e["delta"]) >= self.min_deleg for e in events):
            lines.append(f"Total delegated {lava(sum(nd.values()))} LAVA by {len(nd)} delegators")
        return lines, events

    def check(self):
        state = self.load_state()
        now = int(time.time())
        try:
            specs = self.specs() if self.watch_specs else {}
            snaps = {p["address"]: self.snapshot(p["address"]) for p in self.providers}
        except Exception as e:
            state["fails"] = state.get("fails", 0) + 1
            if state["fails"] == self.fail_after:
                self.send(f"⚠️ <b>lava-stake-watch</b> cannot read the Lava REST API ({self.fail_after} runs in a row)\n"
                          f"<code>{html.escape(str(e))[:300]}</code>")
            self.save_state(state)
            sys.exit(f"check failed: {e}")

        if state.get("fails", 0) >= self.fail_after:
            self.send("✅ <b>lava-stake-watch</b> reads the Lava REST API again")
        state["fails"] = 0
        first_run = "providers" not in state
        old_snaps = state.get("providers", {})
        events = [e for e in state.get("events", []) if now - e["t"] < 2 * DAY]

        if first_run:
            parts = []
            for p in self.providers:
                s = snaps[p["address"]]
                fr = [c for c, v in s["chains"].items() if v["frozen"]]
                parts.append(f"<b>{html.escape(self.name(p, s))}</b>: {len(s['chains']) - len(fr)} chains active"
                             + (f", frozen on {chain_list(fr)}" if fr else "")
                             + f"; {lava(sum(s['delegators'].values()))} LAVA delegated by {len(s['delegators'])} delegators")
            self.send("👀 <b>lava-stake-watch</b> is watching\n" + "\n".join(parts)
                      + (f"\n{len(specs)} chain specs on Lava" if specs else ""))
        else:
            for p in self.providers:
                addr, new = p["address"], snaps[p["address"]]
                old = old_snaps.get(addr)
                if old is None:  # provider added to the config later: record silently
                    continue
                lines, evs = self.diff(old, new, now)
                for e in evs:
                    e["p"] = addr
                events += evs
                if lines:
                    self.send(f"<b>{html.escape(self.name(p, new))}</b>\n" + "\n".join(lines))

            known = set(state.get("specs", []))
            if specs and known:
                for cid in sorted(set(specs) - known):
                    missing = [self.name(p, snaps[p["address"]]) for p in self.providers if cid not in snaps[p["address"]]["chains"]]
                    text = f"🆕 New chain on Lava: <b>{html.escape(specs[cid])}</b> ({cid})"
                    events.append({"t": now, "text": text})
                    self.send(text + (f"\nNot staked yet: {html.escape(', '.join(missing))}" if missing else ""))
                for cid in sorted(known - set(specs)):
                    events.append({"t": now, "text": f"🗑 Chain spec removed: {cid}"})
                    self.send(f"🗑 Chain spec removed from Lava: {cid}")

        state["providers"] = snaps
        if specs:
            state["specs"] = sorted(specs)
        state["events"] = events
        self.save_state(state)

    # ---- daily report ----

    def heights(self):
        """(latest height, height about 24h ago) from the recent block time."""
        latest = self.get("/cosmos/base/tendermint/v1beta1/blocks/latest")["block"]["header"]
        h = int(latest["height"])
        old = self.get(f"/cosmos/base/tendermint/v1beta1/blocks/{h - 10000}")["block"]["header"]
        bt = (datetime.fromisoformat(latest["time"][:19]) - datetime.fromisoformat(old["time"][:19])).total_seconds() / 10000
        return h, h - round(DAY / bt)

    def rewards(self, addr, height=None):
        j = self.get(f"/lavanet/lava/subscription/estimated_provider_rewards/{addr}/{addr}", height)
        return sum(float(c["amount"]) for c in j.get("total", []) if c["denom"] == "ulava")

    def usd(self):
        if not self.price:
            return None
        try:
            req = urllib.request.Request("https://api.coingecko.com/api/v3/simple/price?ids=lava-network&vs_currencies=usd", headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)["lava-network"]["usd"]
        except Exception:
            return None

    def daily(self):
        state = self.load_state()
        now = int(time.time())
        events = [e for e in state.get("events", []) if now - e["t"] < DAY]
        price = self.usd()
        try:
            _, h1 = self.heights()
        except Exception:
            h1 = None
        for p in self.providers:
            addr = p["address"]
            try:
                s = self.snapshot(addr)
            except Exception as e:
                self.send(f"📊 <b>{html.escape(p.get('name') or short(addr))}</b>: daily report unavailable\n"
                          f"<code>{html.escape(str(e))[:300]}</code>")
                continue
            ch = s["chains"]
            fr = [c for c, v in ch.items() if v["frozen"]]
            jailed = [c for c, v in ch.items() if v["jail_end"] > now]
            lines = [f"📊 <b>{html.escape(self.name(p, s))}</b> · last 24 hours",
                     f"Chains: {len(ch) - len(fr)} active"
                     + (f" · frozen on {chain_list(fr)}" if fr else "")
                     + (f" · jailed on {chain_list(jailed)}" if jailed else ""),
                     f"Self-stake {lava(sum(v['stake'] for v in ch.values()))} LAVA · delegated "
                     f"{lava(sum(s['delegators'].values()))} LAVA by {len(s['delegators'])} delegators"
                     + (f" · commission {s['commission']}%" if s["commission"] is not None else "")]

            moves = [e for e in events if e.get("p") == addr and "deleg" in e]
            if moves:
                inc = [e["delta"] for e in moves if e["delta"] > 0]
                out = [e["delta"] for e in moves if e["delta"] < 0]
                lines.append(f"Delegations: {signed(sum(inc))} in ({len(inc)}) · {signed(sum(out))} out ({len(out)})"
                             if inc and out else f"Delegations: {signed(sum(inc or out))} {'in' if inc else 'out'} ({len(inc or out)})")
            try:
                r_now = self.rewards(addr)
                r_old = self.rewards(addr, h1) if h1 else None
                line = f"Estimated rewards this month: {lava(r_now)} LAVA"
                if price:
                    line += f" (${r_now / U * price:,.0f})"
                # the estimate drifts both ways during the month; only a drop by more than half is the payout reset
                if r_old is not None and r_now >= r_old / 2:
                    line += f", {signed(r_now - r_old)} LAVA in 24h"
                elif r_old is not None:  # rewards were paid out and the estimate started from zero
                    line += ", new rewards month started"
                lines.append(line)
            except Exception as e:
                lines.append(f"Estimated rewards unavailable: {html.escape(str(e))[:120]}")

            texts = [e["text"] for e in events if e.get("p") == addr and "text" in e]
            if texts:
                lines.append("")
                lines += texts
            self.send("\n".join(lines))

        news = [e["text"] for e in events if "p" not in e and "text" in e]
        if news:
            self.send("Lava in the last 24 hours\n" + "\n".join(news))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-c", "--config", default=os.environ.get("LAVA_STAKE_WATCH_CONFIG", "/etc/lava-stake-watch/config.toml"))
    ap.add_argument("--daily", action="store_true", help="send the 24h report instead of checking")
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending them")
    a = ap.parse_args()
    w = Watch(a.config, a.dry_run)
    w.daily() if a.daily else w.check()


if __name__ == "__main__":
    main()
