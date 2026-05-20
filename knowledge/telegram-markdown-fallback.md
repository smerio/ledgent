# Resilient Telegram Markdown Parsing and Plain-Text Fallback

This document details the architectural pattern implemented to prevent Telegram message delivery failures due to strict Markdown parsing rules, particularly during LLM-generated responses like `/strategy` or `/ask`.

---

## 1. The Strict Markdown Parsing Problem

### Background
The Ledgent Telegram Bot utilizes the standard Telegram Bot API `sendMessage` endpoint to deliver account reports, transaction updates, and advisor recommendations. By default, the bot formats messages with `parse_mode="Markdown"` to provide styled text (bold, italics, code blocks).

### The Bug
Telegram's standard V1 Markdown parser is notoriously fragile and strict. If a message contains unclosed or unbalanced formatting characters, Telegram's API rejects the entire message with:
`400 Bad Request: can't parse entities: Can't find end of the entity`

Common triggers for this error in LLM-generated or financial messages include:
*   **Unclosed Underscores**: Large numbers written like `1_200_000` or system variables like `user_id` or `price_usd` are interpreted as the start of an italic section.
*   **Arbitrary Markdown**: Claude's advisor responses often use standard markdown lists, bullet points, or nested brackets that don't match the strict Telegram V1 Markdown syntax rules.

Because the final `tg.send_message` call in commands like `/strategy` or `/ask` occurred outside of the LLM `try/except` block, any parse failure logged an error on the Lambda side but yielded a silent failure to the user. The user was left staring at a perpetual `Analyzing…` or `Thinking…` message with no response delivered.

---

## 2. Resilient Plain-Text Fallback Pattern

To guarantee that the user always receives the advisor's advice or portfolio stats, we implemented an automatic fallback retry mechanism directly inside the core `send_message` helper in `src/telegram_utils.py`:

```python
def send_message(chat_id: int | str, text: str, parse_mode: str = "Markdown") -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    resp = requests.post(
        _API.format(token=token, method="sendMessage"),
        json=payload,
        timeout=10,
    )
    data = resp.json()
    if not data.get("ok"):
        # If Markdown parsing fails, fallback to plain text so the user at least gets the message!
        if parse_mode == "Markdown" and "parse" in data.get("description", "").lower():
            logging.getLogger(__name__).warning("Telegram Markdown parsing failed, retrying in plain text...")
            fallback_payload = payload.copy()
            fallback_payload.pop("parse_mode", None)
            resp = requests.post(
                _API.format(token=token, method="sendMessage"),
                json=fallback_payload,
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                return
        logging.getLogger(__name__).error("Telegram sendMessage failed: %s", data)
```

### Key Architectural Decisions:
1.  **Fail-Safe Interception**: We intercept any API response where `"ok": false` and the `"description"` contains the term `"parse"` (case-insensitive) under `parse_mode == "Markdown"`.
2.  **No In-place Dictionary Mutation**: Instead of modifying the initial `payload` directly via `payload.pop()`, we create a shallow copy (`fallback_payload = payload.copy()`). This keeps unit test mocks and API invocation logs clean by avoiding side-effect mutation of parameters.
3.  **Plain-Text Retry**: We pop the `"parse_mode"` from the copied payload, stripping all strict Telegram parsing constraints, and attempt a second HTTP `post` call. The message is delivered as plain text, ensuring readability and 100% resilience.
4.  **Logging**: Appropriate levels are used (`warning` for the markdown retry attempt, and `error` only if the fallback attempt or other API request fails completely).

---

## 3. Unit Testing and Mock Safeguards

To prevent regressions, we created a comprehensive unit test suite in `tests/test_telegram.py` mapping all execution flows:

*   **First-Try Success**: Verifies that standard messages with valid markdown are delivered in a single HTTP call with the `parse_mode` parameter intact.
*   **Markdown Parse Failure & Fallback Success**: Simulates a `400 Bad Request` Markdown parsing failure on the first HTTP call, verifies that `send_message` executes a second HTTP post with `parse_mode` removed, and asserts that the final text is identical.
*   **True API Failures (Chat Not Found/Blocked)**: Verifies that other Telegram API errors (like `403 Forbidden` due to a blocked bot) do not cause infinite loops or incorrect retries, and are logged immediately as failures.
*   **Module-Scope Imports**: Moved `logging` import to the file-scope level in `telegram_utils.py`, allowing unit tests to reliably patch and verify logging output (`telegram_utils.logging.getLogger`) without throwing module attribute errors.
