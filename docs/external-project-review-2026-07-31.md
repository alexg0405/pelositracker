# External project and research review

Review date: 2026-07-31. This is the comparison gate the workstation plan
requires **before** any external code is imported. It is a review of published
material, not an endorsement, and nothing here has been run against this
repository.

## The finding that governs every row below

**None of the surveyed Polymarket projects target Polymarket US.**

They target Polymarket's Polygon CLOB. Their execution layers are built on
wallet custody and EIP-712 order signing with a private key. This workstation
trades Polymarket US, a separate CFTC-regulated venue reached with an API
key/secret over `gateway.polymarket.us` and `api.polymarket.us`, and it
deliberately contains no wallet, private key, or signing component
(`docs/architecture.md`).

So the usual reason to import one of these — "it already does execution" — is
precisely the reason not to. Adopting their execution path would add key
custody and on-chain signing to a codebase whose safety argument depends on not
having them. Any borrowing must be limited to venue-independent *patterns*, and
must be reimplemented against the existing authenticated REST client.

A second constraint: this workstation is a **taker** that crosses the book on a
confirmed edge with fill-or-kill limits. The most mature project found is a
**maker** that quotes both sides for rebates. Their sizing, inventory, and exit
logic answer a different question and do not transfer.

## Prediction-market execution projects

| Project | License | Scale | Real orders | Backtest / OOS evidence | Fee & slippage | Verdict |
|---|---|---|---|---|---|---|
| [warproxxx/poly-maker](https://github.com/warproxxx/poly-maker) | MIT | ~1.4k stars, 37 commits | Yes, via `ExecutionGateway` (py-clob-client-v2) | **None.** README states a replay backtester over captured journals is "not yet built" | Models maker rebates and adjusts spread on volatility/toxicity | **Do not import.** Maker strategy, wallet signing. Two patterns worth *studying*: the paper → livetest → live progression, and journalling order events for later replay |
| [discountifu/polymarket-trading-bot](https://github.com/discountifu/polymarket-trading-bot) | MIT | 75 stars, ~12 commits | Yes, EIP-712 signing | None published | Not documented | **Do not import.** Young, and its core value is exactly the wallet/signing layer this repo must not acquire |
| Benjam1nCup/…-V2, dexorynlabs/…, and similar | Varies | Low | Claimed | None | None | **Do not import, do not run.** Repository titles repeat "polymarket trading bot" a dozen-plus times — SEO seeding, not engineering. Unvetted trade-executing code from such sources is a credential and funds risk |

### Kalshi

Kalshi is a useful comparison venue because, like Polymarket US, it is
regulated and API-key based rather than wallet based.

- Official SDKs are `kalshi_python_sync` / `kalshi_python_async`; the older
  `kalshi-python` PyPI package is **deprecated** and should not be used as a
  reference.
- Kalshi's own guidance is that SDKs lag the API and the REST OpenAPI /
  WebSocket AsyncAPI specs are the source of truth — the same discipline this
  repo already applies by normalising venue payloads at its own boundary.
- Community clients ([kalshi-py](https://apty.github.io/kalshi-py/),
  [pykalshi](https://github.com/arshka/pykalshi)) are transport wrappers only.
  No strategy or evaluation content to borrow.

## Sports-model and backtesting projects

| Project | License | What it offers | Relevance here |
|---|---|---|---|
| [georgedouzas/sports-betting](https://github.com/georgedouzas/sports-betting) | MIT | Model creation, backtesting, value-bet selection; CLI and Python | Closest in spirit. Its value is the *evaluation* scaffolding, not its models. Any adoption must preserve this repo's stricter contract: whole-event chronological splits and event-block bootstrap, not row-level splits |
| [flumine](https://pypi.org/project/flumine/2.0.2/) | MIT | Betfair betting/trading framework with backtesting and simulation | Exchange-shaped like Polymarket US. Its simulation-versus-live separation is a useful reference for the dry-run/live lane split already implemented |

**Leakage caution.** Public sports-betting backtests routinely evaluate on
row-level random splits, which leaks across plays within a game and inflates
results. Where these projects prevent it, they do so by lagging features. This
workstation's requirement is stricter and must not be relaxed to match an
imported framework: split by whole event, chronologically, weight by event, and
report uncertainty by event block.

## Research areas already covered

`docs/research-references.md` holds the cited literature. Additions from this
review, filling gaps the plan named:

- **Off-policy evaluation and contextual bandits.** Already cited (Wang,
  Agarwal, Dudik). The practical constraint is recorded in
  `docs/data-lineage.md`: this workstation's candidate log has deterministic
  0/1 propensities, so IPS and doubly robust estimates are **not identified**
  from it. No external framework changes that; only controlled exploration
  would.
- **Point-in-time backtesting.** The existing replay path already passes the
  original tick time as `as_of` and never rebases to wall clock, which is the
  property most public frameworks lack.
- **Online calibration under drift.** Gupta and Ramdas remains the appropriate
  shadow-recalibration candidate; nothing found supersedes it.

## Conclusion

Nothing surveyed clears the bar for import. The prediction-market projects are
either strategically mismatched (maker versus taker), architecturally
incompatible (wallet signing versus API key), or unvetted. The sports-model
projects have weaker validation contracts than the one already implemented.

The defensible use of this review is as reference reading — specifically
poly-maker's staged live-enablement and order journalling, and flumine's
simulation/live separation — with any borrowed idea reimplemented against the
existing client and held to this repository's own evaluation rules.
