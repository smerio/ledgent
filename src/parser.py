"""LLM factory for parsing free-text messages into transaction JSON.

Three providers are supported (gemini / openai / claude) behind a common
`LLMParser` interface. The parser returns either a structured transaction
dict (write intent) or a query intent marker — the handler decides what to
do with each. Confidence below 0.7 raises `LowConfidenceError` so the bot
can ask for clarification.
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod

import requests


class ParseError(Exception):
    """Raised when the LLM cannot produce a valid structured response."""


class LowConfidenceError(ParseError):
    """Raised when the LLM produced output but signalled low confidence."""

    def __init__(self, message: str, partial: dict | None = None):
        super().__init__(message)
        self.partial = partial


VALID_OPERATIONS = [
    "P2P_BUY", "P2P_SELL", "SPOT_BUY", "SPOT_SELL",
    "TRANSFER", "STAKE", "UNSTAKE", "INCOME", "EXPENSE",
    "SET_BALANCE",  # reconcile asset@location to a target amount
    "QUERY",        # bot meta-intent: this message is a question, not a write
]


SYSTEM_PROMPT = """You are the parser inside a personal crypto-finance Telegram bot.

Convert each user message into JSON describing one of two intents:

(A) WRITE — a transaction the user is logging
(B) QUERY — a question or command (e.g. "show my balance", "/pnl")

JSON schema:
{
  "operation": one of [P2P_BUY, P2P_SELL, SPOT_BUY, SPOT_SELL, TRANSFER, STAKE, UNSTAKE, INCOME, EXPENSE, SET_BALANCE, QUERY],
  "asset": ticker symbol the operation is about (any symbol — BTC, ETH, SOL, USDT, USDC, RUB, USD, EUR, ...),
  "amount": positive number; for TRANSFER this is the amount that arrives at destination,
  "price": price of 1 unit of `asset` in `quote_asset` units; 0 for transfers/income/expense/set_balance/query,
  "quote_asset": currency/asset the price is denominated in; null if not applicable,
  "total_quote_value": positive number representing the EXACT total proceeds or cost in quote_asset if explicitly mentioned in the message (e.g., 3550.37 in 'Swap ETH for 3550.37 USDC', 998 in 'Buy BTC for 998 USDT', or 639635.56 in 'Buy USDT using 639635.56 RSD'). This field is EXTREMELY CRITICAL for math accuracy. If the user states an exact total quote amount used or received, YOU MUST EXTRACT IT HERE. Null only if completely omitted,
  "source": wallet/exchange/bank name the asset comes FROM (free text, as the user said it),
  "destination": wallet/exchange/bank name the asset goes TO (free text),
  "fee": { "amount": number, "asset": ticker } or null,
  "timestamp": ISO-8601 UTC; if the user did not specify a date, use the supplied current_time,
  "raw_text": original user message,
  "confidence": 0.0 to 1.0 — how sure you are about the parse
}

Operation rules:
- P2P_BUY:    bought crypto/stablecoin from a person via P2P, paid with fiat. The bought crypto/stablecoin goes to 'destination' (e.g., Bybit, Binance). The fiat is paid from 'source' (e.g., Tbank, Sberbank). If the paying bank is not mentioned but a P2P exchange is named (e.g., 'via P2P Bybit', 'on Bybit P2P', 'P2P Bybit'), map both 'source' and 'destination' to that exchange (e.g. source='Bybit', destination='Bybit'). Otherwise, set 'source' to null.
- P2P_SELL:   sold crypto/stablecoin to a person via P2P, received fiat. The sold crypto/stablecoin comes from 'source' (e.g., Bybit, Binance). The fiat is received at 'destination' (e.g., Tbank, SRB Bank). If the receiving bank is not mentioned but a P2P exchange is named (e.g., 'via P2P Bybit', 'on Bybit P2P', 'P2P Bybit'), map both 'source' and 'destination' to that exchange (e.g. source='Bybit', destination='Bybit'). Otherwise, set 'destination' to null.
- SPOT_BUY:   bought asset on an exchange spot market. The bought asset goes to 'destination'. The quote asset used to pay comes from 'source'. Usually 'source' and 'destination' are the same exchange (e.g., Binance, Bybit).
- SPOT_SELL:  sold asset on an exchange spot market. The sold asset comes from 'source'. The quote asset received goes to 'destination'. Usually 'source' and 'destination' are the same exchange (e.g., Binance, Bybit).
- TRANSFER:   moved an asset between wallets/exchanges (no ownership change). The asset is the same on both sides. The asset comes from 'source' and goes to 'destination'.
- INCOME:     received staking rewards, airdrop, interest
- EXPENSE:    paid a fee or subscription separately
- STAKE / UNSTAKE: locked/unlocked an asset
- SET_BALANCE: user is RECONCILING — declaring that a specific asset at a specific location currently holds a specific amount. Use this when the message says "balance", "is", "=", "set to", "should be", "reconciliation", or similar declarative phrasing about a known balance. Fill `asset`, `destination` (the location), and `amount` (the target). Examples:
    "Ledger BTC = 1.395"            → asset=BTC, destination=Ledger, amount=1.395
    "set binance usdt to 0"         → asset=USDT, destination=Binance, amount=0
    "ledger should have 0.5 btc"    → asset=BTC, destination=Ledger, amount=0.5
    "binance has 0 btc, reconciliation" → asset=BTC, destination=Binance, amount=0
- QUERY:      user asked a question (e.g. "/balance", "what's my pnl?", "show last 5 trades")

Rules:
- Never invent numbers. If a field is unclear, lower `confidence`.
- amount is always positive. Direction comes from operation type.
- If the user provides slash commands like /balance, /pnl, /history, /unrealized, /fees, /stats, /help — operation is QUERY.
- Output ONLY the JSON object. No prose.
"""


ADVISOR_SYSTEM_PROMPT = """You are a personal crypto portfolio advisor inside a Telegram bot.
The user shares their portfolio data and asks questions or wants strategy guidance.

Rules:
- Reply concisely (under 280 words). Use Telegram Markdown: *bold*, _italic_, `code`.
- Ground every answer in the actual numbers from their portfolio data.
- For strategy questions be specific: name assets, amounts, dates.
- Never invent transactions or prices not present in the context.
- If data is insufficient to answer, say so briefly and suggest what to check.
"""


class LLMParser(ABC):
    @abstractmethod
    def parse(self, user_message: str, current_time: str) -> dict:
        """Returns the validated dict or raises ParseError."""

    @abstractmethod
    def ask(self, question: str, portfolio_context: str) -> str:
        """Answer a freeform question given portfolio context. Returns plain text."""

    @abstractmethod
    def continue_conversation(self, messages: list) -> tuple[str, list]:
        """Continue a multi-turn advisor chat.

        `messages` is a list of {"role": "user"|"assistant", "content": str}.
        Returns (reply_text, updated_messages_with_reply_appended).
        """


def _validate(payload: dict, raw_text: str) -> dict:
    if not isinstance(payload, dict):
        raise ParseError(f"LLM did not return a JSON object: {payload!r}")
    op = payload.get("operation")
    if op not in VALID_OPERATIONS:
        raise ParseError(f"Invalid operation: {op!r}")
    payload.setdefault("raw_text", raw_text)
    payload.setdefault("confidence", 0.0)

    # Programmatic override of price if total_quote_value and amount are present
    total_quote = payload.get("total_quote_value")
    quote_asset = payload.get("quote_asset")
    
    # Deterministic fallback: extract total quote value from text if missed by LLM
    if total_quote is None and raw_text and quote_asset:
        import re
        qa = re.escape(quote_asset)
        m = re.search(rf'(?:using|for|from)\s+([\d,]+(?:\.\d+)?)\s*{qa}\b', raw_text, re.IGNORECASE)
        if m:
            try:
                total_quote = float(m.group(1).replace(',', ''))
                payload["total_quote_value"] = total_quote
            except ValueError:
                pass

    amount = payload.get("amount")
    if total_quote is not None and amount:
        try:
            from decimal import Decimal
            total_dec = Decimal(str(total_quote))
            amount_dec = Decimal(str(amount))
            if amount_dec > 0 and total_dec > 0:
                payload["price"] = float(total_dec / amount_dec)
        except Exception:
            pass

    fee = payload.get("fee")
    if isinstance(fee, dict):
        payload["fee_amount"] = fee.get("amount", 0) or 0
        payload["fee_asset"] = fee.get("asset")
    else:
        payload.setdefault("fee_amount", 0)
        payload.setdefault("fee_asset", None)
    if op != "QUERY" and float(payload.get("confidence", 0)) < 0.7:
        raise LowConfidenceError(
            f"Low confidence parse: {payload.get('confidence')}",
            partial=payload,
        )
    return payload


# ---------------------------------------------------------------------------
# Gemini  (REST — no SDK so the Lambda layer stays small)
# ---------------------------------------------------------------------------


class GeminiParser(LLMParser):
    _MODEL = "gemini-2.0-flash"
    _URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self):
        self._api_key = os.environ["LLM_API_KEY"]

    def parse(self, user_message: str, current_time: str) -> dict:
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{
                "role": "user",
                "parts": [{"text": f"current_time={current_time}\nuser_message={user_message}"}],
            }],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        try:
            resp = requests.post(
                self._URL.format(model=self._MODEL),
                params={"key": self._api_key},
                json=body,
                timeout=15,
            )
        except requests.RequestException as e:
            raise ParseError(f"Gemini request failed: {e}") from e
        if not resp.ok:
            raise ParseError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            payload = json.loads(text)
        except (KeyError, IndexError, json.JSONDecodeError, TypeError) as e:
            raise ParseError(f"Gemini returned unexpected shape: {e}; body={resp.text[:300]}") from e
        return _validate(payload, user_message)

    def ask(self, question: str, portfolio_context: str) -> str:
        msgs = [{"role": "user", "content": f"Portfolio data:\n{portfolio_context}\n\nQuestion: {question}"}]
        reply, _ = self.continue_conversation(msgs)
        return reply

    def continue_conversation(self, messages: list) -> tuple[str, list]:
        gemini_msgs = [
            {"role": "model" if m["role"] == "assistant" else "user",
             "parts": [{"text": m["content"]}]}
            for m in messages
        ]
        body = {
            "systemInstruction": {"parts": [{"text": ADVISOR_SYSTEM_PROMPT}]},
            "contents": gemini_msgs,
        }
        try:
            resp = requests.post(
                self._URL.format(model=self._MODEL),
                params={"key": self._api_key},
                json=body,
                timeout=30,
            )
        except requests.RequestException as e:
            raise ParseError(f"Gemini request failed: {e}") from e
        if not resp.ok:
            raise ParseError(f"Gemini HTTP {resp.status_code}")
        try:
            reply = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as e:
            raise ParseError(f"Gemini unexpected shape: {e}") from e
        return reply, messages + [{"role": "assistant", "content": reply}]


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


class OpenAIParser(LLMParser):
    def __init__(self):
        from openai import OpenAI
        self._client = OpenAI(api_key=os.environ["LLM_API_KEY"])

    def parse(self, user_message: str, current_time: str) -> dict:
        resp = self._client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"current_time={current_time}\nuser_message={user_message}"},
            ],
        )
        try:
            payload = json.loads(resp.choices[0].message.content)
        except (json.JSONDecodeError, AttributeError, KeyError) as e:
            raise ParseError(f"OpenAI returned non-JSON: {e}") from e
        return _validate(payload, user_message)

    def ask(self, question: str, portfolio_context: str) -> str:
        msgs = [{"role": "user", "content": f"Portfolio data:\n{portfolio_context}\n\nQuestion: {question}"}]
        reply, _ = self.continue_conversation(msgs)
        return reply

    def continue_conversation(self, messages: list) -> tuple[str, list]:
        openai_messages = [{"role": "system", "content": ADVISOR_SYSTEM_PROMPT}] + messages
        resp = self._client.chat.completions.create(
            model="gpt-4o-mini",
            messages=openai_messages,
        )
        reply = resp.choices[0].message.content
        return reply, messages + [{"role": "assistant", "content": reply}]


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------


_CLAUDE_TOOL = {
    "name": "log_transaction",
    "description": "Record a parsed transaction or query intent.",
    "input_schema": {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": VALID_OPERATIONS},
            "asset": {"type": ["string", "null"]},
            "amount": {"type": ["number", "null"]},
            "price": {"type": ["number", "null"]},
            "quote_asset": {"type": ["string", "null"]},
            "source": {"type": ["string", "null"]},
            "destination": {"type": ["string", "null"]},
            "fee": {
                "type": ["object", "null"],
                "properties": {
                    "amount": {"type": "number"},
                    "asset": {"type": "string"},
                },
            },
            "timestamp": {"type": "string"},
            "raw_text": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["operation", "timestamp", "raw_text", "confidence"],
    },
}


class ClaudeParser(LLMParser):
    def __init__(self):
        import anthropic
        self._client = anthropic.Anthropic(
            api_key=os.environ["LLM_API_KEY"],
            timeout=24.0,  # stay under the 29-second Lambda hard limit
        )
        self._model = "claude-haiku-4-5-20251001"

    def parse(self, user_message: str, current_time: str) -> dict:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[_CLAUDE_TOOL],
            tool_choice={"type": "tool", "name": "log_transaction"},
            messages=[{
                "role": "user",
                "content": f"current_time={current_time}\nuser_message={user_message}",
            }],
        )
        payload = None
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                payload = block.input
                break
        if payload is None:
            raise ParseError("Claude did not return a tool_use block")
        return _validate(payload, user_message)

    def ask(self, question: str, portfolio_context: str) -> str:
        msgs = [{"role": "user", "content": f"Portfolio data:\n{portfolio_context}\n\nQuestion: {question}"}]
        reply, _ = self.continue_conversation(msgs)
        return reply

    def continue_conversation(self, messages: list) -> tuple[str, list]:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=ADVISOR_SYSTEM_PROMPT,
            messages=messages,
        )
        reply = resp.content[0].text
        return reply, messages + [{"role": "assistant", "content": reply}]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_parser() -> LLMParser:
    provider = os.environ.get("LLM_PROVIDER", "claude").lower()
    if provider == "gemini":
        return GeminiParser()
    if provider == "openai":
        return OpenAIParser()
    if provider == "claude":
        return ClaudeParser()
    raise ValueError(f"Unknown LLM_PROVIDER: {provider!r}")
