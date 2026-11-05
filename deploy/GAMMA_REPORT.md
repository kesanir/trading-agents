# QQQ gamma map

The reporting-only pipeline prepares one snapshot at 05:55 and publishes a text
summary plus a two-page PDF at 06:00 America/Los_Angeles, weekdays. The NYSE
calendar skips holidays and adjusts expiry life for early closes. Timers use the
named Pacific timezone and do not replay missed mornings after a reboot.

## Sources and methods

- Current futures reference: Yahoo Finance `NQ=F` one-minute close, with its bar
  timestamp. QQQ uses Yahoo as well. Both must be no older than 30 minutes at
  publication and within five minutes of one another. These may be delayed.
- NQ=F is a continuous reference. Its broker-deliverable contract/basis is not
  verified. NQ and MNQ share point levels; rounded to 0.25 points. Dollar exposure
  and order size are not calculated.
- Options: existing Alpaca adapter, including OI settlement dates and quote
  timestamps. A partial page fetch fails the report. At least 80% of loaded
  unexpired contracts must have usable market IV, OI, and acceptable dates.
  Quotes must be from the preceding equity session or newer; OI settlement date
  must be the preceding trading date or newer. Defaults/estimated IV tables are
  excluded. A premarket report explicitly labels prior-session options inputs.
- GEX: existing engine for signed strike exposures, expiry matrices and buckets.
  Report gamma is repriced at the Yahoo QQQ reference using fixed IV and actual
  time to expiry. Call-positive/put-negative OI is a proxy assumption.
  QQQ option life uses cash close plus 15 minutes (16:15 ET normally, 13:15 on
  early-close days); expected-range horizon remains the cash close. See
  https://www.nasdaqtrader.com/Trader.aspx?id=optionshours for QQQ trading hours.
- Gamma switch: all interpolated net-GEX roots across +/-15% hypothetical spot;
  nearest root shown, or unavailable. This differs from the legacy cumulative
  strike crossing, which is retained with its own label in the report.
- Walls: largest gross call/put gamma exposure by strike, not guaranteed price
  support/resistance. Strict today-expiry exposure is separate from 0-1 DTE.
- Expected move: QQQ nearest-expiry ATM IV (equal-side median), current reference
  to today's cash close with calendar-time scaling. One-calendar-day projection
  is separately labeled. This is not a calibrated high/low forecast.
- Delta references: nearest OTM strikes at 10/15/25 absolute model delta, same
  named expiry. More than 0.03 delta away is unavailable. No invented strike.
- Three conditional scenarios, with no calibrated probabilities. Entries are
  prospective five-minute acceptance/retest conditions, not live confirmations.
  Actual stop, target ordering and 1.5R minimum must be checked at execution.
- The reference-inspired second page adds a gamma asymmetry banner, a compact
  NQ/MNQ trigger/stop/target table and a regime-dependent "What to avoid" panel.
  It compares repriced GEX at -1%, current spot and +1%, now and 30 minutes
  before cash close. The time projection holds OI, IV and scenario spot fixed.
  Finite-move hedge adjustment is the negative signed change in option delta
  times OI, 100 shares and destination spot. Positive means modeled buying;
  negative means modeled selling. These are inventory-proxy sensitivities,
  not observed flows, forced orders or predictions of actual dealer behavior.
- Nearest-expiry and whole-board switches use independent spot sweeps. The
  nearest expiry is explicitly named; "whole board" means the loaded 0-45 DTE
  chain, not all listed options. Missing crossings remain unavailable.
  The panel never assumes a nearest-expiry reclaim flips the whole board.
  Selling-premium edge, realized volatility and max pain are not inferred from
  gamma. Futures reference R/R uses tick-rounded levels;
  rows below 1.5R are marked WAIT. Example prices are never copied from the image.
- QQQ option suggestions accompany the same directional triggers. Bullish
  candidates are bull-call debit verticals; bearish candidates are bear-put
  debit verticals. Both legs must exist in the collected chain, have positive
  OI, share an expiry and type, and form a 1-10 point spread. The long strike
  must be within one point of the trigger; the short strike must be reachable
  by the farther listed scenario target. Width near five points is preferred.
  The expiry policy uses the nearest suitable 1-7 calendar DTE and never silently
  substitutes 0DTE. If contracts are unavailable, the row says unavailable.
- Option debit ceilings are width / 2.5 rounded down to the cent, giving at least
  1.5 maximum expiration reward/risk before fees. They are payoff conditions,
  not estimated fair values or live offers. Maximum loss at the ceiling is
  ceiling * 100; maximum expiration gain is (width - ceiling) * 100; breakeven
  is long strike + debit for calls or long strike - debit for puts. All values
  assume one intact standard QQQ spread, held to expiry, before fees.
- Valid quote snapshots use long ask minus short bid, require both legs to be
  two-sided and not crossed, less than 60 seconds apart, and a debit strictly
  between zero and width. Quotes older than 15 minutes are marked prior/stale;
  non-OPRA feeds are marked indicative. Missing/invalid quotes remain N/A.
  Snapshot cost above the debit ceiling is WAIT, never a suggested fill. Both
  legs must be repriced on trigger confirmation. Intraday target arrival does
  not guarantee maximum spread profit; exit the intact spread before expiry
  to manage exercise/assignment exposure. Futures reference R/R is separate
  from option expiration payoff R/R. No options orders or broker writes occur.
- Globex VWAP anchors at 18:00 ET; RTH VWAP/opening range are unavailable at 06:00
  Pacific. The economic calendar is not integrated; the report says so.

## Configuration

Reuse ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_OPTION_FEED, TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID in the existing EC2 .env. Yahoo needs no new API key. Verify the
configured options feed entitlement. Both services enforce paper and dry run.
No broker execution, position closing, or NinjaTrader signal output is imported.

Reports and audit snapshots are in app/reports/gamma/YYYY-MM-DD/. The manifest
contains the PDF hash, option inputs, quote timestamps and model assumptions.
Publication rejects stale snapshots, wrong sessions and altered PDFs. Telegram
receipts are persisted per part. Delivery is at least once: a crash after a send
but before receipt persistence can duplicate that part. Failure alerts use the
same Telegram channel, so a Telegram outage is visible in journald only.

## Validation and activation

Run `python -m unittest discover -s deployment_tests -v`. Offline tests make no
market-data requests and send no messages. `python gamma_report_demo.py --output
PATH` creates a clearly labeled synthetic two-page example without credentials.

Deploy through the existing reviewed OIDC/SSM path. Code deployment does not
activate schedules. Before unit staging, follow deploy/README.md: disable the
reviewed reporting timers and wait for their services to be idle. The installer
checks gamma timers too. Stage with deploy/install.sh and verify both new unit
pairs with .github/deploy/verify-reporting-units.py. Preserve independent
premarket/intraday schedules and inspect overlap before activation.

Verify the Pacific calendar expressions with systemd-analyze calendar. After
reviewing a real credentialed dry run, explicitly enable the two gamma timers
and restore the existing reporting timers that were previously active. Do not
use --now on either service. Inspect next firing times and run the existing
schedule-verification workflow. Yahoo/Alpaca live entitlement, EC2 unit state,
Telegram delivery and deadline performance require host verification.

If preparation exceeds its four-minute timeout, no partial report is published.
Publication has a 06:00-06:05 guard and rejects quotes older than 30 minutes.
Network, scheduler, shared-lock and Telegram latency prevent an exact-second
delivery guarantee. No automatic timer activation or live delivery test is part
of the local implementation.
