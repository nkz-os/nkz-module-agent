---
title: "Agent"
description: "Conversational assistant over a messaging channel — read-only in v1, tenant-isolated by design."
sidebar:
  order: 1
---

# Agent

A conversational assistant reachable from a messaging app (Telegram today). A
platform user links their chat account to their tenant from the module's
panel, then talks to the bot from their phone.

## What this is, and what it is not

**Version 1 is read-only.** A language model answers once a chat is linked
(`backend/app/agent/loop.py`), constrained by a per-turn budget and by
per-tenant and per-account turn quotas (`backend/app/handlers.py`). It has
**no tools** in this phase, so it does **not** write to the platform's
context broker, does not record field operations, and does not command
machinery: it turns text into text, and never takes an action.

Shipping that model call without the spend budget, the quota and the audit
trail is exactly the shortcut the design splits into a later phase on
purpose — and the reason the tool set is absent here. If you are about to
give this module the ability to change platform state, that is a new phase
of work with its own human-confirmation design — not a follow-up commit on
this one.

## Two authentication surfaces, deliberately different

The module has two classes of HTTP route, authenticated in two different
ways, and they must stay different.

**Management routes** (`POST /link-tokens`, `GET /links`,
`DELETE /links/{id}`, in `backend/app/api/__init__.py`) arrive through the
platform's api-gateway. The gateway has already authenticated the caller
against the identity provider before the request reaches this module, and it
injects the caller's identity as headers. These routes trust that injection
via `require_auth()` (`backend/app/middleware/__init__.py`) — they never
validate a token or fetch a key set themselves.

The **channel webhook** (`POST /webhook/telegram`,
`backend/app/api/webhook.py`) is different on purpose: it is **not** behind
the api-gateway. The messaging platform calls it directly, so there are no
gateway-injected headers to trust. It authenticates instead with a shared
secret, registered with the channel when the webhook was set up, compared
with `hmac.compare_digest` so timing cannot leak it byte by byte. Putting
this route behind `require_auth()` would not just be redundant — it would be
wrong, because the caller is the messaging platform's servers, not a
platform user with a session. Do not "unify" the two mechanisms; they
authenticate two different kinds of caller.

The webhook also has a latency obligation the management routes do not: it
must **acknowledge quickly and process out of band**. Messaging platforms
retry any delivery they do not see acknowledged, and a retry here is a
second full execution of a conversational turn — including, on a `/start`
message, a second attempt to redeem an already-consumed link token. The
handler validates the secret, does the minimum parsing needed to decide
whether to accept the update, returns `200` immediately, and hands the
actual work to a background task (`process_update`). See "Known
limitations" below for what that in-process background task does not
survive.

## The isolation model

`SessionContext` (`backend/app/domain/session.py`) is the single source of
tenant identity for a conversational turn. It is built once, at the edge —
either from a freshly redeemed link token or from a lookup of an existing
link — and it is an immutable, frozen dataclass from that point on. Nothing
downstream reconstructs it from something an inbound message said.

Tools and handlers never accept a tenant as a parameter they choose. There
is deliberately no code path where a value comes from message content,
model output, or any other untrusted input and ends up selecting a tenant.
The practical effect: once a later phase wires in a language model, there is
no string a model could be led to produce that would let it act as, or read
data belonging to, a different tenant — because the tenant was already fixed
before the model saw the turn.

The database enforces the other half of this: `agent_channel_links` has a
partial unique index over `(channel, channel_user_id) WHERE status =
'active'`, so a channel account can resolve to **at most one** tenant at any
moment, while revoked rows stay in the table indefinitely (`status =
'revoked'`) — unlimited history, one live truth.

Be precise about where the real barrier sits: it is the `SessionContext`
built by the wrapper (webhook handler / management-route dependency), not
the context broker. Orion-LD's own tenant scoping is defence in depth **on
its own route only** — this module makes no Orion-LD calls in v1, but for
context: the timeseries read path elsewhere in the platform is enforced by a
SQL filter, and the BioOrchestrator knowledge graph is not tenant-scoped at
all. Neither of those is a backstop for a bug in how this module resolves a
session; the resolution itself has to be correct.

## The linking flow

1. An authenticated platform user opens the module's panel
   (`src/components/LinkPanel.tsx`) and clicks "generate link", which calls
   `POST /link-tokens` through the api-gateway.
2. `create_link_token` (`backend/app/identity/service.py`) draws 32 bytes
   from `secrets.token_urlsafe` — a CSPRNG, not a derived or predictable
   value — and stores only its SHA-256 hash, alongside the tenant, user and
   roles, with an expiry. **The clear-text token is returned to the caller
   and is never persisted anywhere.** The response carries a deep link of
   the form `https://t.me/<bot>?start=<token>`.
3. The user opens that link on their phone. The messaging app sends
   `/start <token>` to the bot, which lands on the public webhook.
4. The webhook acknowledges and queues the update. The background task
   parses it, recognises the `/start` payload
   (`start_payload` in `backend/app/channels/telegram.py`), and calls
   `redeem_link_token`.
5. `consume_link_token` atomically flips `consumed_at` on the matching row —
   an unknown, expired, and already-consumed token are all indistinguishable
   `None` results to the caller. On success, `upsert_active_link` revokes any
   existing active link for that channel account and inserts the new one, in
   one transaction, so the partial unique index is never violated mid-flight.
6. The chat now has a `SessionContext`; the assistant replies confirming the
   link and mentions `/unlink`.

## Known limitations

These are named on purpose, with the reason and what would trigger revisiting
them — not gaps to quietly close in an unrelated change.

- **The retry on a link-collision race happens once.** `redeem_link_token`
  catches exactly one `UniqueViolationError` from `upsert_active_link` and
  retries; a second collision propagates. This survives a two-way race
  (two redemptions landing at nearly the same instant), not a fifteen-way
  one. The realistic case this covers is a farmer tapping the link twice —
  a genuine flood of concurrent redemptions for the same channel account is
  not the threat model, and the function fails closed (raises) rather than
  looping or silently dropping one side.
- **The token is consumed before the link row is written.** `consume_link_token`
  and `upsert_active_link` are two separate calls, not one transaction. A
  failure between them (process crash, unhandled exception) leaves the token
  burned with no link created; the user has to generate a new link. Fixing
  this properly means one transaction spanning both steps, which means a new
  repository function — not a try/except patch here.
- **Without `LLM_MODEL` the module does not crash.** It starts, logs the
  missing model at CRITICAL (`llm_unconfigured` in
  `backend/app/agent/loop.py`), and answers with the not-configured notice
  (`UNCONFIGURED_TEXT`). That is deliberate: crashing would leave the
  webhook silent, and a messaging platform that sees no acknowledgement
  retries the delivery — so a missing model would become an unbounded retry
  loop against a dead route. Starting degraded and answering is the cheaper,
  louder failure.
- **The webhook processes in-process, via `BackgroundTasks`.** If the pod
  exits between returning the acknowledgement and sending the reply, that
  reply is lost with no retry (the platform already saw its `200`). A lost
  turn now spends a model call and loses its audit row, so this is more
  material than it was before the model was wired in; the in-process
  background task still does not survive a pod exit, so a durable
  out-of-band queue is the follow-up.
- **The schema is duplicated between this repository and the platform's
  numbered migrations.** `backend/tests/fixtures/schema.sql` here is meant
  to be byte-identical to the platform repository's
  `config/timescaledb/migrations/101_agent_channel_links.sql`.
  `backend/tests/test_schema_sync.py` checks that, but skips whenever the
  platform repository is not checked out alongside this one — which is
  always true in this module's own CI, since it checks out only this repo.
  The guard exists for whoever runs both repos side by side; it is not
  exercised automatically here.
- **`purge_expired()` (`backend/app/dedupe/__init__.py`) is implemented and
  tested, but nothing calls it.** Its scheduler is out of scope for this
  phase; wiring a periodic caller belongs to later operational work.
- **A non-numeric `date` field in an inbound update raises**, rather than
  being treated as missing. `TelegramAdapter.parse` reads
  `message.get("date", 0)` and converts it with `datetime.fromtimestamp`,
  which only tolerates a genuinely absent key, not a present-but-wrong-typed
  one. The platform always sends a numeric Unix timestamp here; if it ever
  didn't, the consequence is one lost reply (the exception is caught and
  logged by `process_update`, not left to crash the process), not a security
  issue.

## Two prohibitions that are documented, not enforced

`_authorised` in `backend/app/api/webhook.py` carries two rules in its own
docstring that no test in this suite checks for. They are repeated here
because a docstring is read by someone already inside that function; this
page is read by someone deciding whether to touch it at all.

1. **No length precheck before the constant-time comparison.** It is
   tempting to add `if len(provided) != len(expected): return False` before
   calling `hmac.compare_digest`, as an "optimisation" or a guard. Do not:
   `compare_digest` is constant-time specifically so that neither a length
   mismatch nor a byte position leaks anything through response timing. A
   precheck like that reopens exactly the side channel the function exists
   to close.
2. **No in-place reassignment of `hmac.compare_digest`** on the real
   standard-library module object, anywhere in this process (for example
   `hmac.compare_digest = insecure_fn`). Doing that would make this function
   insecure without changing a single line inside it. This is not enforced
   by any test — it would require arbitrary code already running in this
   process, at which point the secret comparison has stopped being the
   weakest point — but it is a real way to defeat this function silently,
   so it is written down as a boundary nobody should cross.

## Test conventions that must not be relaxed

A few tests in `backend/tests/` look redundant on a skim. They are not; each
one guards a property that would otherwise have no test at all. Do not
delete or "simplify" any of these without re-reading why they exist.

- **Security properties here are proven by mutation, not just by passing.**
  The discipline behind these tests was: break the code on purpose, confirm
  the test goes red **for the right reason**, put the code back, confirm it
  goes green again. Several tests in this module's history were found to be
  unable to fail at all until this step was applied — a passing test that
  cannot go red proves nothing.
- **The link-token test asserts the token's source, not its values**
  (`test_create_link_token_uses_the_csprng_and_only_the_csprng` in
  `backend/tests/test_identity_service.py`). Unpredictability cannot be
  observed from any finite sample of outputs: a generator built as
  `sha256(tenant_id:user_id:counter)` produces values that are unique and
  fully predictable at the same time, and it would pass a fifty-token
  uniqueness check cleanly. The test instead monkeypatches
  `secrets.token_urlsafe` and asserts the minted token *is* its return
  value — proving the token comes from nowhere else. The companion test,
  `test_tokens_minted_for_the_same_identity_are_unique`, catches a
  degenerate source that does call the CSPRNG but seeds it badly; it is a
  complement to the source test, not a replacement for it.
- **The webhook secret comparison is guarded by two kinds of test, and
  neither is sufficient alone.**
  `test_secret_comparison_uses_hmac_compare_digest`
  (`backend/tests/test_webhook_auth.py`) parses `_authorised`'s own source
  with `ast` and checks that every `return` in it is provably the result of
  `hmac.compare_digest` (or a literal `False`) — this proves a *timing*
  property that is invisible in any response body or status code. The
  behavioural tests alongside it — in particular
  `test_prefix_of_the_secret_is_rejected` and
  `test_differently_cased_secret_is_rejected` — prove that the *right
  values* are being compared, which the mechanism test does not inspect at
  all (a mechanism test would stay green even if the code compared the
  wrong two strings, as long as it used `compare_digest` to do it). Deleting
  either half leaves a real, exploitable gap; do not remove the prefix or
  capitalisation vectors as "redundant" with the AST check.
- **The mechanism test inspects the shape of one function.** It walks the
  AST of `_authorised` specifically. A perfectly safe refactor — extracting
  the comparison into a helper, or assigning the call's result to a variable
  before returning it — changes that shape and will fail this test even
  though nothing is actually broken. When that happens, update the test to
  inspect the new call site; it is not a sign the refactor is wrong. The
  test's own docstring says the same thing, so this survives independently
  of this page too.
- **The autouse `_reset_process_pool` fixture (`backend/tests/conftest.py`)
  is not incidental cleanup.** `app.db.get_pool()` is a process-wide
  singleton by design — one pool for the whole running service — but
  `pytest-asyncio` gives each test function its own event loop, and an
  `asyncpg` pool is bound to the loop that created it. Without resetting the
  pool around every test, a pool built by one test's loop becomes unusable
  the moment a later test's (different) loop tries to use it, with
  confusing "attached to a different loop" failures.
- **One test drives a real, non-overridden client:**
  `TestLinkRoutesRequireAuth.test_no_gateway_headers_is_rejected`
  (`backend/tests/test_api.py`). Every other test against the management
  routes overrides `get_current_user` via FastAPI's
  `app.dependency_overrides` — correct for testing route *logic*, but it
  bypasses authentication entirely, so none of those tests would notice a
  route silently losing its `Depends(get_current_user)`. This one test is
  the only place that wiring itself is checked. Consolidating it into the
  overridden tests removes exactly what it exists to catch.

## The channel boundary

`ChannelAdapter` (`backend/app/channels/base.py`) is a `Protocol` with two
methods: `parse(raw) -> InboundMessage | None` and
`render(msg, chat_id) -> dict`. Nothing above that boundary — the handler,
the identity service, the domain types — knows anything about Telegram's
wire format, or any other channel's. Adding a second messaging channel means
writing a new adapter that implements this protocol; it does not mean
touching the agent, the tools, or the handlers.

One detail worth stating explicitly: the sender identity the adapter
extracts comes from the message's **sender** (`message.from.id`), never from
the **chat** (`message.chat.id`). In a one-to-one conversation they happen to
match; in a group they do not, and the account link is keyed on the sender —
if it were keyed on the chat instead, every member of a group chat would
resolve to whichever single account had linked that chat.

Delivery follows a different identifier on purpose: `InboundMessage` also
carries a `conversation_id`, taken from the message's **chat**
(`message.chat.id`), and the webhook renders and sends the reply there, not
to the sender. The two must not be conflated — identity has to stay pinned
to the sender so the account link resolves correctly, while delivery has to
follow the chat so the reply lands where the message came from; in a group
those are different values, and using the sender for delivery answers in
the wrong place (or fails outright if the bot cannot open a private chat
with them).

## Forward note: the data manifest

This module's root `manifest.json` declares no `data.entities` or
`data.timeseries` today, which is correct: v1 reads no platform entities at
all, so there is nothing to enumerate. The api-gateway's data-access
enforcement (the "CSP-of-data" check other modules use to declare which
NGSI-LD types or timeseries hypertables they are allowed to query) fails
open when a module declares nothing — which is the right posture for a
module that makes no such calls, not a hole to patch now.

When a later phase adds tools that read platform entities, declare exactly
what those tools need under `data.entities` (and `data.timeseries` if
applicable) in `manifest.json` at that point, so the gateway can start
enforcing it.
