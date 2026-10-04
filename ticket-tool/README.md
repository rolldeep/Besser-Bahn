# bbt — Besser-Bahn ticket tool

A small tool for buying Deutsche Bahn tickets on demand and watching for
last-minute fares:

- **Search & book**: connections with live prices, then one click/tap to the
  bahn.de booking page for *that exact train*. You pick the fare and pay there.
- **Last-minute watches**: cron checks your route, day and time window. You get
  a push notification (ntfy / Telegram / any command) when a matching ticket
  becomes bookable or gets cheaper. Examples: a Sparpreis contingent is
  released, a sold-out train opens up again, or the price drops under your
  limit. The notification's button opens the booking page.
- **Hunt**: the cheapest fare per day over a date range, `--top N` for the N
  cheapest trains across all those days (the
  [db-price-hunter](https://github.com/anshamray/db-price-hunter) idea, with one
  request per day).
- Two front-ends: a CLI and a one-page web UI (stdlib HTTP server, no build
  step, works on a phone).

It talks to the same DB Navigator backend (`app.services-bahn.de/mob`) as the
Besser-Bahn app. The bahn.de website API is Akamai-blocked for scripts.

## Quick start (uv)

With [uv](https://docs.astral.sh/uv/) you don't install anything by hand. The
first run creates `ticket-tool/.venv` with Python and the deps from `uv.lock`:

```bash
git clone https://github.com/rolldeep/Besser-Bahn && cd Besser-Bahn/ticket-tool
uv run bbt serve                                  # web UI → http://127.0.0.1:8737
uv run bbt hunt Hildesheim "Berlin Hbf" --date 2026-10-07 --days 2 --top 10 --bahncard bc25
```

`./bbt …` does the same (it uses `uv run` whenever uv is on your PATH). To get
a global `bbt` command, which is also the best setup for cron:

```bash
uv tool install ./ticket-tool        # from the repo root; `uv tool upgrade bbtickets` later
bbt search Hildesheim Berlin --date 2026-10-08 --time 07:00
```

Don't use a one-off `uvx` run for `bbt cron install`: uvx's environment is
temporary, so the cron line would stop working once uv prunes its cache. The
tool warns you about this.

Run it on your own machine. DB blocks many datacenter and cloud IPs, so a
home connection works best.

### Without uv

```bash
cd ticket-tool
pip install -r requirements.txt      # requests + curl_cffi
ln -s "$PWD/bbt" ~/.local/bin/bbt    # optional: `bbt` on your PATH
```

Requires Python ≥ 3.10. Install `curl_cffi`: DB's edge often blocks the plain
`requests` TLS fingerprint (`OPS_BLOCKED`), especially from servers and VPS.

## Buy a ticket

```bash
bbt search "Berlin Hbf" "München Hbf" --date 2026-10-10 --time 08:00 --bahncard bc25
#  1. Sat 10.10. 08:34 → 12:28 · ICE 597 · direct · €39.99
#  2. ...
# Book which one? [number, Enter to skip] 1    → opens bahn.de for that train
```

Non-interactive: `--book 1` (add `--no-browser` to only print the link).
Options: `--class 1`, `--adults 2`, `--dticket`, `--max-transfers 0`,
`--arrive`, `--json`.

Or use the web UI:

```bash
bbt serve                       # http://127.0.0.1:8737
bbt serve --watch-every 10      # …and run the watches itself, no cron needed
```

To use the UI from your phone on the home network, it must have a token:
`bbt serve --host 0.0.0.0 --token <something-long>`, then open
`http://<pc-ip>:8737/?token=<something-long>`.

## Watch for last-minute tickets

1. **Pick a notification channel** (any mix):

   ```bash
   # ntfy: install the ntfy app, subscribe to the same hard-to-guess topic
   bbt config set notify.ntfy_topic bb-$(openssl rand -hex 8)
   # Telegram: token from @BotFather, chat id from @userinfobot
   bbt config set notify.telegram_token 123:ABC…
   bbt config set notify.telegram_chat_id 987654
   # Anything else: a shell command gets $BBT_TITLE $BBT_MESSAGE $BBT_URL
   bbt config set notify.command 'notify-send "$BBT_TITLE" "$BBT_MESSAGE"'
   bbt notify-test
   ```

2. **Add watches**:

   ```bash
   # any train Fri morning at or under €30
   bbt watch add Hamburg Köln --date 2026-10-09 --earliest 06:00 --latest 10:00 --max-price 30
   # one specific (sold-out) train: tell me as soon as it's bookable at all
   bbt watch add Berlin München --date 2026-10-10 --train "ICE 597" --label "Oma"
   bbt watch list
   bbt watch rm 3fa2c1
   ```

3. **Schedule the checker**:

   ```bash
   bbt cron install --every 10     # adds one tagged line to your crontab
   bbt cron show                   # print the line; `bbt cron remove` undoes it
   tail -f ~/.config/besser-bahn-tickets/check.log
   ```

   `bbt check` (what cron runs) is safe to run by hand; `--dry-run` doesn't
   notify or save anything.

How a check decides what to send:

| Situation                                   | Notification? |
| ------------------------------------------- | ------------- |
| A connection starts matching (fare appears / drops under max) | yes |
| Same connection, same or higher price on the next run          | no  |
| Same connection, cheaper than you were last told              | yes |
| It stops matching (sold out), then comes back                 | yes |
| DB errors 6 runs in a row                                     | one warning |

A watch deletes itself once its departure window has passed. Each check only
searches from *now* to the window end, so past departures are ignored.

## Notes & limits

- **Paying happens on bahn.de / in DB Navigator.** The tool never handles your
  DB login or payment details. It mints the same `vbid` link DB Navigator's
  "Reise teilen" produces, which opens the exact connection. If that fails, it
  falls back to a pre-filled bahn.de search at that departure. Check the
  traveller/BahnCard settings on the booking page.
- **Rate limit:** `/mob` blocks a client for minutes after ~10 requests in a
  few seconds. Requests are paced 2 s apart and 429s back off. Each watch
  costs 1–4 requests per run. Keep `--every` ≥ 5 min (enforced) and the
  number of watches sensible.
- Prices are DB's "ab" price for the connection with your traveller settings.
  No price means sold out, or a fare DB doesn't sell (e.g. some regional
  trains).
- State lives in `~/.config/besser-bahn-tickets/` (override with `BBT_HOME`).
  Config can also come from env vars: `BBT_NTFY_TOPIC`, `BBT_NTFY_SERVER`,
  `BBT_TELEGRAM_TOKEN`, `BBT_TELEGRAM_CHAT_ID`, `BBT_NOTIFY_COMMAND`.

## Development

```bash
uv run python -m unittest discover -s tests       # offline, no network needed
uv run python ../api-tests/healthcheck.py         # live: includes the bbt parser check
```
