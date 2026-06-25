# Deploying the Arena server (live `/compete/arena-open` ladder)

The Arena server is a long-running WebSocket process. Hosting it on Railway makes the public
**Arena Open Ladder** at `/compete/arena-open` live: students connect agents, and the server writes
their PnL into `arena_rankings` so the leaderboard updates in real time.

## 1. Create the Railway service

From the `convexpi/arena` repo (Railway reads `railway.toml` at the repo root, which forces the
Dockerfile builder via `deploy/Dockerfile` — so the package is installed and `data/` is shipped):

1. New Railway service → deploy from the `convexpi/arena` GitHub repo.
2. It builds the Dockerfile and runs `python -m convexpi.arena.server` (works regardless of PATH).
   ⚠️ If the build logs show **Nixpacks** instead of Dockerfile — or you hit
   `convexpi-server: command not found` — the service isn't reading `railway.toml`. Fix it in
   Settings → Build → Builder = **Dockerfile**, Dockerfile Path = `deploy/Dockerfile`.

## 2. Set Variables (Railway dashboard → Variables)

**Required for a live leaderboard** — without these the server runs but writes *no* rankings:

| Variable | Value |
|---|---|
| `SUPABASE_URL` | your Supabase project URL (`NEXT_PUBLIC_SUPABASE_URL` is also accepted) |
| `SUPABASE_SERVICE_KEY` | service-role key (bypasses RLS) |
| `SUPABASE_SESSION_ID` | the active `arena-open` session id (see below) |

**Gameplay tuning** (optional; defaults shown):

| Variable | Default | Notes |
|---|---|---|
| `ARENA_N_TICKS` | (blank) | **leave blank** = run forever (needed for an always-open ladder) |
| `ARENA_TICK_INTERVAL` | 1.0 | seconds per tick |
| `ARENA_MAX_DRAWDOWN` | (off) | $ drawdown before elimination |
| `ARENA_POSITION_LIMIT` | (off) | max abs position before elimination |
| `ARENA_ADMIN_TOKEN` | (off) | secret for the instructor console — **set this on a public server** |

**Real order-book mode** (the `arena-book` season — players trade against recorded L2 depth):

| Variable | Value | Notes |
|---|---|---|
| `ARENA_CRYPTO_BOOK` | `data/btcusd_book.jsonl` | real recorded depth, shipped in the image |
| `ARENA_MAKER_FEE_BPS` | -1 | maker rebate (optional) |
| `ARENA_TAKER_FEE_BPS` | 3 | taker fee (optional) |

Point `SUPABASE_SESSION_ID` at the `arena-book` session (slug `arena-book`) for this service.

### Finding the `arena-open` session id

```sql
select s.id
from arena_sessions s
join cohorts c on c.id = s.cohort_id
where c.slug = 'arena-open' and s.status = 'active';
```

(As of seeding, that session is `bbc9a447-79f0-4d0c-bc94-0ad21b091030`; re-query if you re-seed.)
If you ever recreate the session, update `SUPABASE_SESSION_ID` to match. One Railway service feeds
one session.

## 3. Point the web app at the server

Add the server's public URL to the web app so students can connect (use `wss://` for the deployed
service):

```
NEXT_PUBLIC_ARENA_URL=wss://<your-arena-service>.up.railway.app
```

The `/compete/arena-open` page and the instructor console read this variable.

## 4. Verify it's live

- Railway logs show `tick … fv=… bid=… ask=…` heartbeats and **no** `[rankings push failed: …]` lines.
- The session accrues rows:
  ```sql
  select count(*) from arena_rankings where session_id = '<SUPABASE_SESSION_ID>';
  ```
  Background agents (`noise_*`, `market_maker`, `momentum_*`, `informed`) appear within a few ticks;
  real players appear once they connect.
- `/compete/arena-open/leaderboard` shows the rankings.

## Local testing

```bash
pip install -e .
SUPABASE_URL=… SUPABASE_SERVICE_KEY=… SUPABASE_SESSION_ID=… \
  python -m convexpi.arena.server --n-ticks 20 --tick-interval 0.5 --max-drawdown 500 --position-limit 300
```
A run with the risk limits set (the production config) should write rows with populated
`survival_score`. If you see `[rankings push failed: …]`, the Supabase vars are wrong.
