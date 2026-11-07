# Nastiness list

RC1-629, Phase 1. Committed before any experiment runs (epic rule, R4). The
workload is synthetic and was written by the person who will tune for it.
This list is the defense: every trap is named here first, with the shape that
carries it and what each configuration is expected to do with it. The
held-out slice and the results document test it; a trap that turns out not
to bite is reported as such, not quietly dropped.

Generator: `pocbench/corpus.py` (dataset) and `pocbench/workload.py` (query
log). Both are pure functions of the code, the row counts in
`config/config-space.toml` and the seed. The profiler never reads the shape
names below; it derives its own shapes from the literal SQL.

## Dataset traps

| ID | Trap | How it is built |
| --- | --- | --- |
| D1 | Skewed tenants | A contact's account is `1 + floor(A * u^3)` with `u` a seeded hash of the contact id. Account 1 owns about 8% of contacts; the smallest own about a thousand. |
| D2 | Skewed activity | An event's contact is `1 + floor(C * u^2)`: low contact ids are the busiest. Account contact lists sort by id, so the contacts a page shows first are its busiest. |
| D3 | Skewed campaigns | Each account has 10 campaigns; an event's campaign within its account is drawn with `u^2`, so each account's first campaign is its largest. |
| D4 | Correlated predicates | `region` implies `country` (R0-R7 US, R8-R11 CA, R12-R15 GB, R16-R19 DE). The planner multiplies the two selectivities. `email_status = 'bounced'` (0.5%) implies `status = 'inactive'` 80% of the time. |
| D5 | Churned tenants | About a quarter of accounts above id 3 churned at a seeded date and have no events after it. Their events sit at the old end of the time-ordered table. |
| D6 | Time-ordered heap | Events are generated in `occurred_at` order, so a BRIN on `occurred_at` is meaningful and any one account's events are spread across the whole heap. |

## Query traps

| ID | Trap | Shape | Expected behavior |
| --- | --- | --- | --- |
| Q1 | N+1 | `latest_events`: last 5 events for one contact, 40% of the log. Contacts come from the first 200 active ids of an account, the ones an account page lists. | PK-only: full scan of events per call. Naive `events(contact_id)`: fetches every event of a busy contact and sorts. Only I1 `(contact_id, occurred_at DESC)` stops after 5. |
| Q2 | Expression the naive index misses | `email_lookup`: `lower(email) = lower(...)`, mixed case in the literal. | A btree on `email` cannot serve `lower(email)`. Naive builds exactly that index and still scans contacts. I7 serves it. |
| Q3 | Index that looks useful and is not | `account_activity`: `WHERE account_id = $1 ORDER BY occurred_at DESC LIMIT 50`. | Naive builds `events(occurred_at)`. For a churned account (D5), walking that index from the newest event finds no match until the churn date, so it reads most of the index. The planner assumes matches are spread evenly in time. I2 `(account_id, occurred_at)` avoids it. |
| Q4 | Deep pagination | `account_activity` with OFFSET up to 5,000 (10% of calls), `account_contacts` OFFSET up to 100. | Every skipped row is still fetched. No candidate index fixes it; compute cache and tier are the only levers. |
| Q5 | Index that looks useless and is not | `bounced_contacts`: `email_status = 'bounced'` per account. | A two-value column looks unindexable, but the queried value is 0.5% of rows. I6, a partial index on the rare value, serves it. |
| Q6 | Correlated-predicate misestimate | `segment_preview`: `country = $1 AND region = $2 AND status = 'active' ORDER BY id LIMIT 100`, platform-wide; 20% of calls pair a region with the wrong country. | For a mismatched pair the planner expects tens of thousands of matches and walks the primary key for 100 of them; the true count is zero, so it walks the whole table. I8 `(country, region)` answers zero rows from the index. Extended statistics would fix the estimate but are outside the v1 space. |
| Q7 | Large aggregate on a skewed key | `campaign_funnel`: count by event type for one campaign. | Hot campaigns hold over a million events. I3 helps; the hottest stay expensive. |
| Q8 | Large sort | `campaign_last_touch`: `DISTINCT ON (contact_id) ... ORDER BY contact_id, occurred_at DESC` for one campaign. | Sorts every event of the campaign; spills past `work_mem` on hot campaigns. No candidate index removes the sort. A shape the recommendation is expected to leave slow. |
| Q9 | Large time-window report | `daily_report`: one day of events grouped by campaign and type, top 50. No tenant. | About 140,000 rows per call. I4 (BRIN) or naive's `events(occurred_at)` bound the scan. |

## What the workload leaves out

- **Writes.** Neon runs with `fsync` off, so write-path numbers are not
  comparable to standalone Postgres, and writes would make variant branches
  diverge from each other mid-experiment. v1 is read-only.
- **Request sequencing.** The log records N+1 the way a statement log shows
  it: many small lookups whose contacts come from list pages. The closed-loop
  replay draws instances independently per session, so a burst is not
  replayed as a burst.
- **Prepared-statement plan caching.** Every instance is planned fresh.

## Shape mix

`latest_events` 40%, `email_lookup` 15%, `account_contacts` 10%,
`account_activity` 10%, `bounced_contacts` 6%, `campaign_funnel` 6%,
`segment_preview` 5%, `daily_report` 5%, `campaign_last_touch` 3%.
Account-scoped shapes draw the account in proportion to its contact count.
