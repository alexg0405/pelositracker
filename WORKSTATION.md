# Polymarket US Research Workstation

This repository can run as an isolated local copy of the PelosiTracker website.
`start-workstation.cmd` binds it to localhost, disables the separate paper-bot
subsystem, and uses workstation-specific SQLite paths.

The existing Python/Rust signal pipeline is retained unchanged. Polymarket US is
implemented as a separate execution sidecar: it consumes existing signals and
US executable prices without feeding them back into or silently altering the
established calculations.

## First run

Double-click `setup-workstation.cmd`. It creates `.venv`, installs the exact
dependencies, builds the Rust extension, and creates the ignored `.env` file.

Then double-click `start-workstation.cmd` and open:

<http://127.0.0.1:8775>

The local default login is `admin` / `admin`. The server binds only to
`127.0.0.1`, so it is not exposed to other devices.

## Polymarket US API key

Public Polymarket US sports events and market prices work without a key.

When you are ready:

1. Open <https://polymarket.us/developer>.
2. Select **Create API Key**.
3. Save both values. The Secret Key is shown only once.
4. Stop the workstation with `Ctrl+C`.
5. Open `.env` in this folder and set:

   ```env
   POLYMARKET_US_KEY_ID=your-key-id
   POLYMARKET_US_SECRET_KEY=your-secret-key
   ```

6. Save `.env` and restart `start-workstation.cmd`.
7. Open the **Polymarket US Research** tab and select **Test account key**.

Do not paste either value into the website, frontend JavaScript, source files,
GitHub, screenshots, or chat. `.env` is excluded by `.gitignore`.

## Automatic trading and safety boundary

Automatic trading starts off, in dry-run mode, and disarmed. A restart always
closes the live-order latch. Configure it in **Polymarket US Research →
Automatic trade controls**.

Recommended rollout:

1. Leave **Dry run** and **Require the existing engine to clear every entry
   gate** selected.
2. Set a small total exposure, per-position/event limits, and a cash reserve.
3. Turn on automatic analysis and inspect the execution journal for mappings,
   rejections, simulated fills, marks, and logical exits.
4. Only after reviewing that evidence, select **Live**, save, type
   `ARM LIVE TRADING`, and arm the workstation. Arming expires after 30 minutes.
5. Use **Disarm** to close the live-order latch or **Emergency stop** to turn off
   automation and request cancellation of this workstation's open orders.

Live entries and live automatic cash-outs both require the current 30-minute
arm. Marks and exit recommendations continue after it expires, but an order is
blocked until you explicitly arm the workstation again.

Live entries use previewed, fill-or-kill limit orders. The backend enforces
strict 5¢–95¢ hard bounds plus the narrower bracket you choose, positive
execution edge, signal quality/reference floors, spread/depth limits, cash
reserve, exposure/concentration limits, daily-loss stop, and order cadence.
Only one new entry is allowed per cycle.

Automatic cash-out never triggers merely because a line increased by one cent.
After the configured minimum hold, it requires a meaningful return plus edge
decay or a trailing pullback, a loss plus model invalidation, or a material model
reversal. The workstation manages only positions it created; manual Polymarket
positions are read for account visibility but never auto-traded.

Unchecking **Require the existing engine to clear every entry gate** does not
change any probability or edge calculation, but permits positive-edge `WATCH`
signals to enter the separate execution-policy review. This is materially
riskier and is not the recommended live setting.

## Data and reproducibility

Local SQLite data lives under `workstation-data/`, which is also ignored by Git.
`polymarket-us-trading.db` contains the execution policy, managed positions and
bounded audit journal. It does not contain either API credential.
The source repository includes the calculation and model-training code plus the
research bibliography. It does not include production database history, paper
PDFs, or separately supplied calibration/model artifacts. Add reviewed artifact
paths to `.env` only when you have those exact files.

The journal is training/evaluation data, not automatic model retraining. Updating
a predictive model from these records requires a separate chronological,
out-of-sample validation step so the live engine is never silently contaminated
by leakage or a handful of recent outcomes.
