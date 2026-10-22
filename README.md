# Wallet-Based Per-Minute Session Billing

A Django REST Framework backend for prepaid, per-minute consultations between customers and providers. It implements mock recharges, an authorized session lifecycle, and transactional billing with a per-minute charge ledger.

Sessions are prepaid and billed per started minute. The first minute is charged at session start; subsequent minutes are charged when billing is triggered. At ₹50/minute, ending at exactly 3:00 without a billing tick at that instant costs ₹150. If billing for minute four runs at exactly 3:00 before the ending request, the total is ₹200 because committed charges are not refunded. Ongoing billing requires explicit requests; no background scheduler is implemented.

## Capabilities and stack

- USER/PROVIDER accounts, token authentication, and participant-scoped session access.
- Customer wallets, idempotent recharge callbacks, and nonnegative balances.
- Five-minute minimum balance to start, a fixed rate snapshot, atomic first-minute charging, and synchronous catch-up billing.
- Django 5.2, DRF 3.16, PostgreSQL, psycopg 3.2, and python-dotenv; see [dependency bounds](requirements.txt).

See [DESIGN.md](docs/DESIGN.md) for diagrams, transaction boundaries, timing semantics, and engineering decisions.

## Quick start

Prerequisites: Python 3.13, a running PostgreSQL 14 server, and `psql`. The recorded development environment uses Python 3.13.17 and PostgreSQL 14.24. Run commands from the repository root; shell examples use Bash/Zsh.

### 1. Create the local database

Connect with `psql -d postgres` as a PostgreSQL administrator, using your installation's administrator credentials if required. For **new** local resources:

```sql
CREATE ROLE wallet_billing_user LOGIN;
\password wallet_billing_user
CREATE DATABASE wallet_billing_db OWNER wallet_billing_user;
ALTER ROLE wallet_billing_user CREATEDB;
```

`\password` prompts for the application password. Reuse existing resources rather than rerunning CREATE statements. `CREATEDB` lets Django create its separate test database; it is a local testing privilege, not a production runtime requirement.

### 2. Install and configure

```sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
# Copy only when .env does not already exist:
test -e .env || cp .env.example .env
```

Edit `.env` locally using the variables below. Replace database placeholders and choose distinct, strong random signing/webhook secrets, for example with a local password manager. `.env` and `.venv/` are ignored by Git. Existing OS variables take precedence over `.env`.

| Variable | Local value / behavior |
|---|---|
| `DB_NAME` | `wallet_billing_db`; required, nonempty |
| `DB_USER` | `wallet_billing_user`; required, nonempty |
| `DB_PASSWORD` | Your application role password; required, nonempty |
| `DB_HOST`, `DB_PORT` | Default `localhost`, `5432` |
| `DJANGO_SECRET_KEY` | Required, nonempty signing secret |
| `RECHARGE_WEBHOOK_SECRET` | Separate secret; missing/empty configuration denies callbacks with 403 |
| `DJANGO_DEBUG` | Set `True` locally for demo seeding; defaults to `False`; only True/False, case-insensitive, accepted |
| `DJANGO_ALLOWED_HOSTS` | Local example: `localhost,127.0.0.1,[::1]`; comma-separated, defaults empty |

Generic `DEBUG` and `ALLOWED_HOSTS` environment variables are not used. Host configuration is required for local `runserver` with debug disabled. [.env.example](.env.example) contains safe local examples and secret placeholders.

Exported `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, and `DB_PORT` override their `.env` values. To use your configured `.env` instead of unintended shell overrides, clear these variables in the current terminal; this does not edit `.env`:

```sh
unset DB_NAME DB_USER DB_PASSWORD DB_HOST DB_PORT
```

Before migrating, verify the connected database name and role with this read-only query. Confirm they match your intended local database and application role; no password is displayed.

```sh
python manage.py shell -c "from django.db import connection; cursor = connection.cursor(); cursor.execute('SELECT current_database(), current_user'); print(cursor.fetchone()); cursor.close()"
```

```sh
python manage.py migrate
python manage.py check --database default
```

### 3. Create a demo provider and run

With `DJANGO_DEBUG=True`, enter a strong demo password at the hidden prompt below. No password is supplied by the project.

```sh
printf '%s' 'Demo provider password: '
read -r -s DEMO_PROVIDER_PASSWORD
printf '\n'
export DEMO_PROVIDER_PASSWORD
python manage.py seed_demo --local --username demo_provider --rate 10.00
unset DEMO_PROVIDER_PASSWORD
python manage.py runserver 127.0.0.1:8000
```

The command prints **`User ID: N`**; use that number as `provider_id` in API requests. `--local` and the password environment variable are required; `--password-env NAME` selects a variable other than `DEMO_PROVIDER_PASSWORD`. Matching reruns leave the account unchanged; conflicting credentials, role, profile, or rate are refused. No public provider signup exists.

## Tests

Run the suite on PostgreSQL with the local `CREATEDB` privilege above. Django creates and destroys a separate test database. SQLite does not test PostgreSQL row locking.

```sh
python manage.py test --noinput --verbosity 1
python manage.py check --database default
python manage.py makemigrations --check --dry-run
python manage.py migrate --check
```

### Test Coverage

The six core test scenarios map to these tests and assertions.

| Required scenario | Existing test(s) and file | Assertions / coverage |
|---|---|---|
| 1. Minimum start balance | [core/test_session_api.py](core/test_session_api.py): `SessionWorkflowAPITests.test_five_minute_minimum_is_domain_400_and_has_no_partial_start`; [core/test_billing.py](core/test_billing.py): `BillingTests.test_start_debits_first_minute_at_exact_five_minute_threshold` | 49.99 at rate 10 is rejected with accepted state/no charges; exactly 50 starts, debits 10, and persists minute 1. |
| 2. Recharge → two-minute session → end | [core/test_session_api.py](core/test_session_api.py): `SessionWorkflowAPITests.test_full_http_workflow_recharge_to_completed_session` | Full authenticated HTTP flow; persisted wallet 30 after recharge 50, two distinct 10-unit charges, fixed snapshot, counters, completion metadata, and 75-second actual/120-second billed duration. |
| 3. Balance runs out | [core/test_session_api.py](core/test_session_api.py): `SessionWorkflowAPITests.test_bill_trigger_completes_at_insufficient_funds_boundary` | Completed/system/insufficient_balance response; persisted zero balance, five charges, totals, and first-unpaid-boundary end/duration. |
| 4. Invalid state transition | [core/test_session_api.py](core/test_session_api.py): `SessionWorkflowAPITests.test_invalid_transition_has_machine_readable_domain_error` | Start/bill/end before start return 400 and `invalid_transition`; after each request, all persisted session fields (including status, timestamps, and completion metadata), wallet balance, and charge rows remain unchanged. |
| 5. Duplicate successful recharge callback | [core/test_api.py](core/test_api.py): `RechargeCallbackAPITests.test_duplicate_success_credits_once_and_preserves_timestamp` | Two successful callbacks return 200; persisted success, one wallet credit, and unchanged completion timestamp. |
| 6. Duplicate minute billing | [core/test_billing.py](core/test_billing.py): `BillingTests.test_repeated_and_earlier_billing_never_refunds_or_duplicates` | Repeated/backdated ticks preserve wallet, contiguous unique ledger, billed count/sum/duration, and monotonic checkpoint. |

Additional tests cover PostgreSQL constraints, rollback injection, concurrent starts/ticks/recharges/endings, authorization, malformed IDs, and demo/configuration safeguards.

## API reference

All paths have trailing slashes. Send JSON; customer/provider authentication is `Authorization: Token <TOKEN>`. Registration creates a USER and zero-balance wallet atomically; it does not issue a token.

| Method | Route | Request fields | Authority | Success |
|---|---|---|---|---|
| POST | `/api/register/` | `username`, `password` | Public | 201: id, username, role |
| POST | `/api/token/` | `username`, `password` | Public | 200: token |
| GET | `/api/wallet/` | — | USER | 200: balance |
| POST | `/api/recharges/` | `amount` | USER | 201: reference, status |
| POST | `/api/recharges/callback/` | `reference`, `outcome`: success/failed | Webhook secret | 200: reference, status |
| POST | `/api/sessions/` | `{}` | USER | 201: session detail |
| POST | `/api/sessions/{id}/assign/` | `provider_id`: provider User ID | Customer owner | 200: session detail |
| POST | `/api/sessions/{id}/accept/` | `{}` | Assigned provider | 200: session detail |
| POST | `/api/sessions/{id}/start/` | `{}` | Customer owner | 200: session detail |
| POST | `/api/sessions/{id}/bill/` | `{}` | Either participant | 200: session detail |
| POST | `/api/sessions/{id}/end/` | `{}` | Either participant | 200: session detail |
| GET | `/api/sessions/{id}/` | — | Either participant | 200: stored detail; no billing |

Amounts are positive decimal values with at most 12 digits and two decimal places; `10.005` is rejected. Registration and recharge/session request serializers reject unexpected fields. Lifecycle actions accept no client clocks, rates, ownership, counters, or end metadata. Session path IDs must be canonical positive ASCII integers within signed bigint range; leading zeros, signs, underscores, Unicode numerals, and oversized IDs return the same 404 as missing/inaccessible sessions.

The callback requires `X-Recharge-Webhook-Secret`, compared against `RECHARGE_WEBHOOK_SECRET`; user tokens do not authorize it. Matching duplicates return 200 without additional credit; conflicting final outcomes return 409 without state changes. Recharge statuses are `pending`, `success`, `failed`.

Errors use an `errors` envelope. Domain failures include `detail` and `code`, for example `{"errors":{"detail":["Session is not in the required state."],"code":"invalid_transition"}}`. Field validation errors contain field names and message lists.

Statuses: 400 invalid input/business state, 401 missing/invalid user token, 403 forbidden role or webhook secret, 404 missing/inaccessible resource, 409 conflicting callback. Invalid token credentials return a generic 400. A missing wallet returns 400.

## Curl walkthrough

Keep the server running in another terminal and run the requests below one at a time. Replace `<...>` placeholders with credentials or values from earlier responses; use the same customer password for registration and token requests. For `<PROVIDER_USER_ID>`, use the number printed by `seed_demo`, without quotes. Use your configured webhook secret for the callback, and keep real credentials out of shared history/logs.

```sh
BASE=http://127.0.0.1:8000
curl -sS "$BASE/api/register/" -H 'Content-Type: application/json' \
  -d '{"username":"customer_demo","password":"<CUSTOMER_PASSWORD>"}'
# 201: {"id":1,"username":"customer_demo","role":"USER"} (ID illustrative)
curl -sS "$BASE/api/token/" -H 'Content-Type: application/json' \
  -d '{"username":"customer_demo","password":"<CUSTOMER_PASSWORD>"}'
# 200: {"token":"<CUSTOMER_TOKEN>"}
curl -sS "$BASE/api/wallet/" -H 'Authorization: Token <CUSTOMER_TOKEN>'
# 200: {"balance":"0.00"}
curl -sS "$BASE/api/recharges/" -H 'Authorization: Token <CUSTOMER_TOKEN>' \
  -H 'Content-Type: application/json' -d '{"amount":"50.00"}'
# 201: {"reference":"<RECHARGE_UUID>","status":"pending"}
curl -sS "$BASE/api/recharges/callback/" -H 'Content-Type: application/json' \
  -H 'X-Recharge-Webhook-Secret: <WEBHOOK_SECRET>' \
  -d '{"reference":"<RECHARGE_UUID>","outcome":"success"}'
# 200: {"reference":"<RECHARGE_UUID>","status":"success"}
curl -sS "$BASE/api/sessions/" -H 'Authorization: Token <CUSTOMER_TOKEN>' \
  -H 'Content-Type: application/json' -d '{}'
# 201: session detail; copy its id as <SESSION_ID>.
curl -sS "$BASE/api/sessions/<SESSION_ID>/assign/" -H 'Authorization: Token <CUSTOMER_TOKEN>' \
  -H 'Content-Type: application/json' -d '{"provider_id":<PROVIDER_USER_ID>}'
curl -sS "$BASE/api/token/" -H 'Content-Type: application/json' \
  -d '{"username":"demo_provider","password":"<DEMO_PROVIDER_PASSWORD>"}'
# 200: {"token":"<PROVIDER_TOKEN>"}
curl -sS "$BASE/api/sessions/<SESSION_ID>/accept/" -H 'Authorization: Token <PROVIDER_TOKEN>' \
  -H 'Content-Type: application/json' -d '{}'
curl -sS "$BASE/api/sessions/<SESSION_ID>/start/" -H 'Authorization: Token <CUSTOMER_TOKEN>' \
  -H 'Content-Type: application/json' -d '{}'
# 200: in_progress, rate_snapshot "10.00", billed_minutes 1, billed_amount "10.00".
```

At rate 10.00, start requires 50.00 and leaves 40.00. Wait until just after 60 seconds from `started_at`, then bill and end **before 120 seconds** to settle exactly two minutes:

```sh
curl -sS "$BASE/api/sessions/<SESSION_ID>/bill/" -H 'Authorization: Token <CUSTOMER_TOKEN>' \
  -H 'Content-Type: application/json' -d '{}'
curl -sS "$BASE/api/sessions/<SESSION_ID>/end/" -H 'Authorization: Token <PROVIDER_TOKEN>' \
  -H 'Content-Type: application/json' -d '{}'
curl -sS "$BASE/api/sessions/<SESSION_ID>/" -H 'Authorization: Token <CUSTOMER_TOKEN>'
curl -sS "$BASE/api/wallet/" -H 'Authorization: Token <CUSTOMER_TOKEN>'
# 200: {"balance":"30.00"} when two minutes were charged.
```

Session detail includes participant IDs, status, rate snapshot, lifecycle timestamps, ending metadata, counters, and durations. For an illustrative provider end at 75 seconds, selected fields are:

```json
{"status":"completed","rate_snapshot":"10.00","ended_by":"provider",
 "end_reason":"participant_ended","billed_minutes":2,"billed_amount":"20.00",
 "billed_duration":"00:02:00","actual_duration":"00:01:15","actual_duration_seconds":75.0}
```

Money is serialized as decimal strings and timestamps as UTC ISO-8601. Actual duration is zero until completion. Credentials, customer wallet balance, and the internal billing checkpoint are not session response fields.

## Billing rules and limitations

- Start checks balance ≥ five minutes at the provider's current rate, snapshots that rate, and commits minute 1's debit/ledger/state together. Only one session per customer may be in progress.
- Active billing charges in advance: t=0 → minute 1; t=59.999 → still 1; t=60 → minute 2; t=120 → minute 3. Repeated billing never charges the same minute twice.
- End settles started minutes: an end exactly at t=60 excludes minute 2 unless a billing tick already committed it. Partial final minutes are fully charged; committed charges are not refunded.
- **Explicit `/bill/` calls are required to advance an ongoing session.** Start/end also settle; GET requests and recharge callbacks do not. No scheduler or background termination exists.
- Insufficient funds complete the session at the first unpaid boundary, even if discovered later. System-ended actual duration represents funded time, not measured audio/video duration. Delayed settlement uses currently available funds, including intervening recharges.

Payments use a mock shared-secret callback; there is no payment gateway or audio/video transport. Tokens do not expire automatically. Throttling, provider discovery, cancellation, reassignment, and production deployment infrastructure are not implemented. Catch-up inserts one ledger row per charged minute while holding locks. Dependencies use version ranges, not a lockfile. See [DESIGN.md](docs/DESIGN.md) for timing, storage-limit, and concurrency trade-offs.

AI tools assisted implementation, review, tests, and documentation.
