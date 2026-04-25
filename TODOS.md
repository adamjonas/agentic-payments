# TODOs — Agentic Payments

## Frontend / Backend Coordination

### [ ] SSE step text: populate with plain-English descriptions

**What:** In the agent loop (FastAPI), populate the `text` field of `step` SSE events
with plain-English descriptions of what the agent is doing — e.g., "Searching the web
for Lightning search APIs" — not raw tool arguments like `web_search(query='...')`.

**Why:** The frontend agent stream uses a narrative format. If `text` contains raw
Python function calls, the stream looks like a noisy log viewer instead of a readable
story. This is the key visual distinction of the UI.

**When:** Implement during Next Steps #5 (agent loop). NOT a frontend polish item.

**Example mapping:**
| Tool call | text field value |
|-----------|-----------------|
| `web_search(query="lightning search api")` | "Searching the web for Lightning search APIs" |
| `fetch_l402_resource(url="https://lightning.video/...")` | "Fetching resource from lightning.video — requesting access, found invoice" |
| `check_balance()` | "Checking wallet balance" |
| Payment blocked | "Vendor lightning.video is on the blocklist — skipping" |

**Depends on:** Agent loop implementation (#5 in Next Steps)

---

## Safety / Correctness (before mainnet)

### [ ] Budget check must include pending transactions

**What:** Change the budget enforcement query to count `status IN ('completed', 'pending')` instead of just `'completed'`. Or set `status='reserved'` atomically before `mdk send` and update to `'completed'`/`'failed'` after confirmation.

**Why:** The plan's budget query only sums `status='completed'`. An in-flight payment (`status='pending'`) doesn't count against the daily budget. A second payment could go out before the first settles. On mainnet with real sats, this means potential overspend beyond the daily limit.

**How to apply:** Before switching from signet to mainnet, verify the budget check covers all in-flight payments. 5-line change to `budget.py` or the payment logging path.

**Depends on:** Initial SQLite schema + budget.py implementation.

### [ ] Invoice deduplication before mdk send

**What:** Store attempted invoice payment hashes in SQLite (add a `payment_hash` column to `transactions`). Before any `mdk send`, check if the hash has already been attempted.

**Why:** If `fetch_l402_resource` retries on invoice expiry and the first `mdk send` had already executed before the error was detected, retrying with a reused invoice could double-pay. One extra SQLite read per payment.

**How to apply:** Add `payment_hash TEXT` to the `transactions` schema. Add a `SELECT 1 FROM transactions WHERE payment_hash = ?` guard before every `mdk send` call in `l402.py`.

**Depends on:** `transactions` table schema, `l402.py` implementation.
