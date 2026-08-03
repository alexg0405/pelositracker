# Research and implementation references

- Fellegi and Sunter, probabilistic record linkage: <https://doi.org/10.1080/01621459.1969.10501049>
- Shin, insider trading and bookmaker odds: <https://doi.org/10.2307/2234526>
- Beta calibration: <https://proceedings.mlr.press/v54/kull17a.html>
- Probability forecast combination: <https://doi.org/10.1111/j.1467-9868.2009.00726.x>
- Logit forecast combination: <https://doi.org/10.1016/j.ijforecast.2013.09.009>
- Proper scoring rules: <https://doi.org/10.1198/016214506000001437>
- Brier decomposition: <https://doi.org/10.1175/1520-0450(1973)012%3C0595:ANVPOT%3E2.0.CO;2>
- Stationary bootstrap: <https://doi.org/10.1080/01621459.1994.10476870>
- Data-snooping correction: <https://doi.org/10.1111/1468-0262.00152>
- Polymarket CLOB documentation: <https://docs.polymarket.com/trading/orderbook>
- The Odds API v4 guide: <https://the-odds-api.com/liveapi/guides/v4/index.html>
- OWASP session management: <https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html>
- OWASP SSRF prevention: <https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html>
- Lindsey, inning/score/base-out baseball strategy and win state: <https://doi.org/10.1287/opre.11.4.477>
- Bukiet, Harold, and Palacios, Markov-chain baseball run distributions: <https://doi.org/10.1287/opre.45.1.14>
- Choi PhD thesis, chronological comparison of MLB probability forecasters: <https://www.ml.cmu.edu/research/joint_phd_dissertations/thesis_yojoongc_phd_stat_2023.pdf>
- MLB Statcast field definitions: <https://baseballsavant.mlb.com/csv-docs>
- Li, Huang, and Li, MLB feature selection: <https://doi.org/10.3390/e24020288>
- MLB Win Expectancy (score, inning, outs, runners, run environment): <https://www.mlb.com/glossary/advanced-stats/win-expectancy>
- MLB Game Strategy Explorer (2016-2025 empirical state tables): <https://baseballsavant.mlb.com/game-strategy-explorer>
- Wheatcroft, calibration rather than accuracy for sports-betting model selection: <https://doi.org/10.1016/j.mlwa.2024.100539>
- Gupta and Ramdas, online post-hoc calibration under sequential data: <https://proceedings.mlr.press/v202/gupta23c.html>
- Wang, Agarwal, and Dudik, event-context off-policy evaluation and SWITCH:
  <https://proceedings.mlr.press/v70/wang17a.html>
- Brill, Yurko, and Wyner, clustered play-level sports uncertainty and
  event-aware resampling: <https://arxiv.org/abs/2406.16171>
- Huber and Heumann et al., hierarchical batter/pitcher matchup models (2025 preprint): <https://arxiv.org/abs/2511.17733>
- Kaminski and Lo, stop-loss rules under momentum and random-walk processes: <https://citeseerx.ist.psu.edu/document?doi=954a65e94b6cee2abf017650e7381aacef54f8b2&repid=rep1&type=pdf>
- Lo and Remorov, stop-loss rules with serial correlation, regimes, and transaction costs: <https://ssrn.com/abstract=2695383>
- Simon, real-time sportsbook line movement and short-horizon overreaction: <https://doi.org/10.1287/mnsc.2022.00456>
- Polymarket US order semantics and fee schedule: <https://docs.polymarket.us/concepts/orders> and <https://docs.polymarket.us/fees>

## External projects

Surveyed 2026-07-31; see `docs/external-project-review-2026-07-31.md` for the
comparison matrix and the conclusion that none of them clear the bar for
import. Listed here as reference reading only.

- warproxxx/poly-maker, Polymarket CLOB maker bot (MIT; no backtester):
  <https://github.com/warproxxx/poly-maker>
- discountifu/polymarket-trading-bot, CLOB taker with EIP-712 signing (MIT):
  <https://github.com/discountifu/polymarket-trading-bot>
- Kalshi official SDK guidance, API-key venue comparable to Polymarket US:
  <https://docs.kalshi.com/sdks/overview>
- georgedouzas/sports-betting, model backtesting and value-bet selection (MIT):
  <https://github.com/georgedouzas/sports-betting>
- flumine, Betfair exchange trading framework with simulation (MIT):
  <https://pypi.org/project/flumine/2.0.2/>
