# Async Lambda Pattern (webhook → self-invoke)

Added 2026-05-19. Fixes the 29-second API Gateway hard limit for slow
commands (LLM calls, full DynamoDB replay).

## Problem

API Gateway HTTP API has a 29-second integration timeout. The bot Lambda
was doing everything synchronously: DynamoDB full-table replay (~2s) +
LLM call (~10-20s) = exceeded 29s on `/ask` and `/strategy`.
Lambda would be killed mid-flight, leaving the user with only "Thinking…"
and no follow-up.

## Architecture

```
Telegram → API Gateway → bot Lambda (webhook path, < 1s)
                              │
                              │  lambda:InvokeFunction (InvocationType=Event)
                              ▼
                         bot Lambda (async _proc path, up to 60s)
                              │
                              ▼
                         Telegram sendMessage
```

## Two-mode lambda_handler

`lambda_handler` detects which path it's on by checking `"_proc" in event`:

```python
# Async path (self-invoked, no API GW constraint)
if "_proc" in event:
    p = event["_proc"]
    try:
        _route(p["text"], p["chat_id"], p["user_id"])
    except Exception as e:
        tg.send_message(p["chat_id"], f"_Error: {e}_")
    return {"statusCode": 200, "body": "OK"}

# Webhook path (API GW — validate, dedup, dispatch, return fast)
...
boto3.client("lambda").invoke(
    FunctionName=context.function_name,   # self-invoke
    InvocationType="Event",               # fire-and-forget
    Payload=json.dumps({"_proc": {"text": text, "chat_id": chat_id, "user_id": allowed_id}}),
)
return {"statusCode": 200, "body": "OK"}
```

Fallback: if the boto3 invoke raises (e.g., race during first Terraform
apply before the IAM policy is in place), the webhook path falls back to
synchronous `_route()`.

## Terraform changes

```hcl
# bot Lambda timeout increased from 29 → 60 (meaningful for async path)
resource "aws_lambda_function" "bot" {
  timeout = 60
  ...
}

# Self-invoke IAM policy
data "aws_iam_policy_document" "self_invoke" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.bot.arn]
  }
}
resource "aws_iam_role_policy" "self_invoke" {
  name   = "${var.project_name}-self-invoke"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.self_invoke.json
}
```

Note: there is no circular dependency — the IAM policy resource depends on
the Lambda ARN, but the Lambda only depends on the IAM role (not the policy).

## Deduplication

`acquire_update_lock` runs in the webhook path (before async dispatch), so
duplicate Telegram updates are rejected before the async invocation fires.
The async path does not re-check dedup.

## SDK timeout

The Anthropic SDK client is constructed with `timeout=24.0` in `ClaudeParser`
so that LLM failures surface as a caught exception and send a proper error
message rather than silently expiring the Lambda.

## Gotchas

- The `context.function_name` in the webhook Lambda gives the correct
  function name for self-invocation. Do not hardcode the function name.
- `boto3.client("lambda")` uses `AWS_DEFAULT_REGION` automatically (set by
  Lambda runtime). No need to specify `region_name`.
- Async invocations have at-least-once delivery semantics. The dedup lock
  (DynamoDB conditional write) prevents double-processing on retries.
- If the async Lambda itself times out (60s), the user gets no response.
  CloudWatch logs will show the timeout; no error message is sent to Telegram.
