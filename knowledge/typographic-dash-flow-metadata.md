# Typographic Dash Resilience and Flow Metadata in Advisor Context

This document details the architectural solutions implemented to solve two distinct operational and reasoning issues:
1. **The Typographic Dash Parse Bug** (Regex/Decimal conversion syntax failures on Unicode dash inputs)
2. **The Blind-Transaction LLM Hallucination** (Advisor logic losing track of active bank, exchange, and fiat flows)

---

## 1. Typographic/Unicode Dash Resilience (`/set` Command)

### The Problem
When users interact with a Telegram bot via mobile devices (iOS/Android) or macOS, their keyboards or native autocorrect systems often automatically replace a standard ASCII hyphen-minus `-` (`\u002d`) with typographic Unicode characters:
* **Unicode Minus Sign**: `−` (`\u2212`)
* **En-dash**: `–` (`\u2013`)
* **Em-dash**: `—` (`\u2014`)

Standard numeric regular expressions (e.g. `-?\d+`) do not recognize typographic dashes, leading to command matching failures. Furthermore, passing any typographic dash directly to Python's `Decimal` parser throws a severe `decimal.InvalidOperation: [<class 'decimal.ConversionSyntax'>]` exception. 

### The Solution: Robust Regex and String Normalization
To make the `/set` reconciliation command completely immune to input layout variations, we updated the pattern matcher and parsing logic:

1. **Typographic Dash Regex**:
   We expanded the amount group pattern in `_SET_RE` within `src/handler.py` to match all common typographic dashes:
   ```python
   _SET_RE = re.compile(
       r"^/set\s+(?P<asset>\S+)\s+(?P<location>.+?)\s+(?P<amount>[-−–—]?\d+(?:\.\d+)?)\s*$",
       re.IGNORECASE,
   )
   ```

2. **Amount Normalization Helper**:
   Inside `_cmd_set`, prior to `Decimal` conversion or swap-detection string formatting, the matched value is programmatically normalized by replacing all Unicode dashes with a standard ASCII hyphen-minus:
   ```python
   amount_str = m.group("amount").replace("−", "-").replace("–", "-").replace("—", "-")
   ```
   This ensures that the final value parses successfully in `Decimal(amount_str)` without triggering any syntax or conversion exceptions, and that the swap-detection suggest card uses standard numbers.

---

## 2. Advisor Context: Flow Metadata Integration

### The Problem
During multi-step transaction loops (like DCA trading cycles), the user moves funds between multiple exchanges, banks, and storage cold wallets (e.g. *ExchangeA*, *ExchangeB*, *BankA*, *Ledger*). 

In previous versions, the portfolio context transaction history lines sent to the LLM Advisor were formatted strictly as:
`[Date] [Operation] [Amount] [Asset] @ [Price] [Quote]`

However, for `P2P_BUY`, `P2P_SELL`, and `TRANSFER` operations, the exchange rates are often implied or pricing fields are stored as `None`/`0`. When the LLM Advisor read a history line like `2026-05-25 P2P_BUY 100.00 USDT` with no pricing quote, it:
1. Had no way to know which bank or fiat funded the transaction (RUB via BankA vs RSD via BankB).
2. Guessed/hallucinated the direction or currency of the flow based on other random holdings, producing wrong analysis.

### The Solution: Flow Annotations (`[source → destination]`)
We upgraded `_build_portfolio_context` in `src/handler.py` to pull the `source` and `destination` fields from the transaction database record and append them as a visual routing annotation on the history line.

#### Implementation
```python
# In src/handler.py::_build_portfolio_context
src = tx.get("source") or ""
dest = tx.get("destination") or ""
flow_s = f" [{src} → {dest}]" if src or dest else ""
lines.append(f"  {ts} {op} {tg._fmt(amount)} {asset}{price_s}{flow_s}")
```

#### Impact on LLM Reasoning
By appending the flow metadata, the history lines are formatted dynamically with full physical visibility:
* `2026-05-25 P2P_BUY 100.00 USDT [BankA → ExchangeB]`
* `2026-05-22 P2P_SELL 50.00 USDT [ExchangeA → BankB] @ 95.50 RSD`
* `2026-05-25 TRANSFER 50.00 USDT [ExchangeA → ExchangeB]`

The LLM Advisor immediately reads exactly which bank, exchange, or wallet was utilized for the transaction. This completely eliminates guesswork, halts fiat flow hallucinations, and aligns the Advisor's logical cycle-tracking perfectly with actual physical funds routing.
