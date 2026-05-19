# Advisor Session Pattern (multi-turn /ask conversations)

When `/ask` calls the LLM advisor and the reply ends with a `?`, the bot
keeps the conversation open so the user's next plain-text message is routed
back to the advisor instead of the transaction parser.

## DynamoDB shape

```
PK  = USER#<telegram_id>
SK  = SESSION#advisor
type = "session"
messages = [
  {"role": "user",      "content": "Portfolio data:\n...\n\nQuestion: ..."},
  {"role": "assistant", "content": "Your profit is... What's BTC price?"},
  {"role": "user",      "content": "78201"},          ← added on continuation
]
ttl = <epoch + 300>   ← 5-minute TTL; DynamoDB auto-deletes expired items
```

The `messages` list is stored as a DynamoDB List-of-Maps. `_to_decimal`
in `database.py` only converts `float → Decimal`, so string values pass
through safely.

## Routing logic (`handler._route`)

```
if lower.startswith("/") and not /ask or /strategy:
    database.clear_advisor_session(user_id)   # slash resets session
    ...
    return

session_msgs = database.get_advisor_session(user_id)
if session_msgs is not None:
    _cmd_continue_advisor(text, ...)           # resume conversation
    return

_cmd_freeform(...)                             # normal transaction parser
```

## Session lifecycle

| Event | Session state |
|---|---|
| `/ask <question>` | Created if reply contains `?` |
| User sends plain text reply | Continued; cleared if reply has no `?` |
| Any other slash command | Explicitly cleared |
| 5 minutes pass with no reply | TTL expires, DynamoDB auto-deletes |

## `_advisor_is_asking` heuristic

```python
def _advisor_is_asking(reply: str) -> bool:
    return bool(re.search(r'\?', reply[-200:]))
```

Checks the last 200 chars for a `?`. Keeps session alive only when the
advisor genuinely needs a follow-up answer. A concluding statement without
a question mark closes the session cleanly.

## Parser: `continue_conversation()`

All three LLM backends implement `continue_conversation(messages) → (str, list)`.
`ask()` now delegates to it (single-turn = one-element message list).

- **Claude**: passes `messages` directly to `client.messages.create`
- **Gemini**: converts `"assistant"` → `"model"` in role names
- **OpenAI**: prepends system message to the list

## Gotchas

- The **advisor context note** (set via `/context set <text>`) is appended to
  the portfolio snapshot in the first user message as
  `=== USER INSTRUCTIONS FOR ADVISOR ===`. It is baked in at session start —
  changing it mid-conversation has no effect until the next `/ask`.
  See [[advisor-context-note]].
- As of 2026-05-19, the portfolio context also includes **per-lot FIFO detail**
  for non-stable crypto (up to 20 lots, oldest first) and **live prices** from
  CoinGecko. These are baked in at session start. See [[advisor-live-context]].
- For sell simulations spanning multiple lots, the LLM's per-lot arithmetic is
  unreliable. Recommend `/sim sell <amount> <asset>` for precise results.
- Portfolio context is embedded in the **first user message** only.
  Subsequent turns carry the conversation history, so the LLM retains
  the portfolio data across turns without re-fetching it.
- Session size: first message (~2-4 KB context) + short follow-ups;
  well within DynamoDB's 400 KB per-item limit for typical conversations.
- If the user logs a transaction while a session is active (within 5 min),
  they must either wait for TTL or send any slash command to reset.
