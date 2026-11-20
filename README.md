# lava-stake-watch

Telegram alerts about your Lava Network provider's state on chain: frozen and jailed
chains, endpoint changes, delegations in and out, commission, and new chain specs, plus a
daily report with the 24-hour change in estimated rewards.

One Python file, standard library only, run by a systemd timer. Built and used by
[MrsML](https://mrs-ml.website) next to [release-watch](https://github.com/milakorn69/release-watch):
release-watch tells you about new node software, lava-stake-watch tells you what the chain
thinks of your provider.

## What it sends

**State changes**, within ten minutes, one message per provider per run:

```
MrsML
🧊 Frozen on SONIC
✅ Unfrozen on ETH1, back in pairing from the next epoch
⛔ Jailed on AVAX (jail #3) until 14.11 15:20 UTC
🔧 Endpoint on BTC, BTCT, BCH: old-host.example:443 → eu1.lava.mrs-ml.website:443
⚙️ Commission 50% → 75%
💰 +250,000 LAVA delegated by lava@13avw…96d264
💸 −40,000 LAVA undelegated by lava@1q9xy…v8a0j0 (left)
Total delegated 1,358,372 LAVA by 19 delegators
```

**New chains on Lava**, with the providers that are not staked on them yet:

```
🆕 New chain on Lava: hyperliquid mainnet (HYPERLIQUID)
Not staked yet: MrsML
```

**Daily report**: chains, stake, delegators, delegation moves, estimated rewards and
everything that happened in the last 24 hours:

```
📊 MrsML · last 24 hours
Chains: 36 active
Self-stake 78,323 LAVA · delegated 1,358,372 LAVA by 19 delegators · commission 75%
Delegations: +250,000 in (2) · −40,000 out (1)
Estimated rewards this month: 59,373 LAVA ($971), +242 LAVA in 24h
```

## How it reads the chain

| What | Query |
|---|---|
| Stake entries per chain: frozen, jails, endpoints, stake | `GET /lavanet/lava/pairing/provider/{address}/` |
| Delegations | `GET /lavanet/lava/dualstaking/provider_delegators/{address}` |
| Chain specs | `GET /lavanet/lava/spec/show_all_chains` |
| Estimated rewards, now and 24h ago | `GET /lavanet/lava/subscription/estimated_provider_rewards/{address}/{address}` |

A stake entry is frozen when its `stake_applied_block` is the maximum int64, and jailed
while `jail_end_time` is in the future. A jail is reported when the `jails` counter goes up,
so a short jail between two runs is not missed.

## Details that took a while to get right

- The first run records the current state silently and sends one summary, so you are
  not flooded with every existing delegation.
- When one host moves for many chains, that is one line, not twenty.
- The provider's own stake is listed on chain as a delegation to itself; it is shown as
  self-stake and not counted as a delegator, and neither are emptied delegations left with
  dust under 1 LAVA.
- Delegation moves under 1 LAVA (reward claims) are ignored; moves under `min_delegation`
  go only to the daily report.
- A provider added to the config later is recorded silently on its first run.
- If the REST API fails several runs in a row you get one alert, and one more when it
  recovers. State is never overwritten by a failed run, so nothing is lost or replayed.
- The 24-hour reward change reads historical state with the `x-cosmos-block-height`
  header, so it needs an archive REST endpoint in the list. Some gateways answer such a
  request with HTTP 200 and only an error message; that counts as a failure and the next
  endpoint is tried, so a missing answer is never read as zero rewards.

## Install

Requires Python 3.11+ and a Telegram bot ([@BotFather](https://t.me/BotFather)) added to your
chat or group. Reads only public REST endpoints: no keys, no node, no SSH.

```sh
sudo install -D -m 755 lava_stake_watch.py /opt/lava-stake-watch/lava_stake_watch.py
sudo install -D -m 644 config.example.toml /etc/lava-stake-watch/config.toml
sudo install -m 600 lava-stake-watch.env.example /etc/lava-stake-watch.env
sudo editor /etc/lava-stake-watch/config.toml /etc/lava-stake-watch.env

# try it without sending anything
sudo python3 /opt/lava-stake-watch/lava_stake_watch.py --dry-run
sudo python3 /opt/lava-stake-watch/lava_stake_watch.py --dry-run --daily
rm -f /var/lib/lava-stake-watch/state.json   # so the real first run sends the summary

sudo cp systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lava-stake-watch.timer lava-stake-watch-daily.timer
```

Get the chat id by sending a message to the group and opening
`https://api.telegram.org/bot<TOKEN>/getUpdates`.

## Configure

Everything is in [`config.example.toml`](config.example.toml), with comments: the providers
to watch, REST endpoints, thresholds for delegation and stake alerts, and the time zone.
Watching another provider (a competitor, or a friend's) is one more `[[provider]]` block.

## License

MIT

