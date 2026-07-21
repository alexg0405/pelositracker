# Paper-bot lifecycle

The bots are fake-money research agents. They cannot hold a wallet, sign an
order, submit a real trade, or route an order to Polymarket or a sportsbook.

## Entry

A bot may open a position only when the engine emits `PAPER_BET`, the selection
has an exact Polymarket token identity, and the market can be graded from the
event's final score. Player props and ambiguous lines are rejected before
entry. The requested stake is walked through complete ask depth using Decimal
math. Tick size, minimum size, market status, depth completeness, and the
recorded fee schedule are fail-closed gates.

`stake` is total fake cash consumed, including the entry fee. `entry_vwap` is
the ask-depth VWAP and `entry_price` is the all-in cost per share. The order-book
hash, provider identifiers, timestamps, fee, signal decision ID, and shares are
stored with the position.

## Marking and cash-out

On each monitored update, the whole open position is walked through complete
bid depth. The mark is net liquidation value after the simulated sell fee, not
the midpoint, last trade, ask, or original cost:

`unrealized P/L = net executable sell proceeds - total entry cash`

Every attempt is appended to `account_bet_marks`, including unpriced attempts
and their rejection reasons. Missing identity, bid depth, fee metadata, or an
open market makes the position unpriced and prevents cash-out.

Automatic cash-out is controlled by the persistent switch on each bot. When it
is off, marks are still collected and the position is held for settlement. When
it is on, the default policy requires at least a 120-second hold and at least a
three-cent favorable all-in price move. It then permits one of these exits:

- a 20% net return after both entry and exit costs;
- protection of a meaningful trailing profit after a 12% net high-water mark
  and a 35% retracement;
- a calibrated-estimate reversal while at least the larger of $2 or 8% of the
  stake remains as net profit;
- a calibrated-estimate reversal with an 18% net stop loss.

The policy probability that produced the paper signal is used. In the current
runtime that is calibrated consensus; independent-model output remains a
separate cross-check and is never silently promoted into a bot decision. A
cash-out update and fake-bankroll credit occur in one database transaction and
are idempotent. The position cannot re-enter the same event/market/outcome
after closing.

## Settlement, cancellation, and removal

Open moneyline, spread, and total positions settle from the authoritative final
score. A genuine provider cancellation may void and refund them. Manually
removing monitoring is not outcome evidence, so the API returns `409` while any
bot position remains open instead of manufacturing a void.

## Research data

`GET /api/accounts/{name}/marks` returns the latest 5,000 valuation and decision
rows for a bot in chronological order. `GET
/api/accounts/{name}/bets/{bet_id}/marks` returns one position's path. The
leaderboard reports realized P/L, executable open P/L,
fees, cash-out counts, and average cash-out holding time. If any open position
cannot be fully marked, total equity is `null` and the response reports known
equity plus the count and stake of unpriced positions.

These observations are inputs for offline, event-grouped chronological
research. They are not proof of predictive edge. Any future model trained from
them must keep future events out of feature construction and use untouched
event-level test windows before it can affect paper actions.
