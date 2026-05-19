# IAM: required permissions for the Lambda role

The Lambda role (`crypto-ledger-lambda-role`) needs the following DynamoDB
actions on the `crypto-ledger` table and its GSIs. If any are missing the
bot silently fails at runtime (the error appears in the Telegram message
but not obviously in Lambda logs unless you search for `AccessDeniedException`).

## Required actions (as of 2026-05-19)

### Lambda self-invoke (added 2026-05-19)

```hcl
# terraform/main.tf — data.aws_iam_policy_document.self_invoke
actions   = ["lambda:InvokeFunction"]
resources = [aws_lambda_function.bot.arn]
```

Required for the async dispatch pattern: the webhook Lambda invokes itself
with `InvocationType=Event` to avoid the 29-second API Gateway timeout.
See [[async-lambda-pattern]].

### DynamoDB (as of 2026-05-18)

```hcl
actions = [
  "dynamodb:PutItem",
  "dynamodb:GetItem",
  "dynamodb:Query",
  "dynamodb:UpdateItem",
  "dynamodb:BatchWriteItem",
  "dynamodb:DeleteItem",   # required for advisor session TTL cleanup
]
```

Defined in `terraform/main.tf` → `data.aws_iam_policy_document.ledger_access`.

## Why `DeleteItem` is needed

`database.clear_advisor_session()` calls `delete_item` to remove the
`SESSION#advisor` record when an `/ask` conversation ends cleanly (reply has
no `?`). Without `DeleteItem` the session lingers in DynamoDB until the
5-minute TTL expires — harmless but generates an `AccessDeniedException`
error message in the Telegram chat after every advisor reply.

## Diagnosis

If you see:
```
Error: An error occurred (AccessDeniedException) when calling the DeleteItem
operation: User: arn:aws:sts::<account>:assumed-role/crypto-ledger-lambda-role/...
is not authorized to perform: dynamodb:DeleteItem
```
add `"dynamodb:DeleteItem"` to the IAM policy in Terraform and run
`terraform apply`.
