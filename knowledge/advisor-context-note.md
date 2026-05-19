# Advisor Context Note (`/context` command)

Added 2026-05-18. Lets the user inject a persistent plain-text instruction
into every `/ask` and `/strategy` call without touching code.

## Use case

After a bulk reconciliation import, the LLM advisor confuses reconcile
transactions for real trades and gives wrong strategy advice. The user can
set a note to filter them out:

```
/context set Ignore reconciliation transactions from 2025-03-01. Those were
manual balance corrections, not real trades. My active DCA cycle started on
2025-03-15.
```

## Commands

```
/context                    — show current note
/context set <text>         — save (replaces any existing note)
/context clear              — remove note
```

## DynamoDB shape

```
PK  = USER#<telegram_id>
SK  = CONFIG#advisor_note
type = "config"
key  = "advisor_note"
value = "<free text>"
```

No TTL — persists until explicitly cleared.

## How it is injected

`_build_portfolio_context()` in `src/handler.py` appends the note as a
final section when non-empty:

```
=== USER INSTRUCTIONS FOR ADVISOR ===
<note text>
```

This section is embedded in the first `user` message of every `/ask` and
`/strategy` call, so it applies to both single-turn and multi-turn advisor
conversations. It is NOT included in the `system` prompt — it rides in the
user message alongside the portfolio snapshot.

## Interaction with advisor sessions

If a multi-turn `/ask` session is in progress (5-minute TTL), the context
note was baked into the first message of that session. Changing the note
mid-conversation will not affect the active session; it takes effect on the
next `/ask` invocation.

## Gotchas

- The note is injected verbatim — no sanitisation. Keep it short (under
  500 chars) to avoid bloating every LLM call.
- There is only one note slot per user. A second `/context set` replaces
  the first.
- `/context clear` writes an empty string to DynamoDB; subsequent lookups
  return `""` which is treated as no note.
