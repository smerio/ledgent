terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

locals {
  webhook_function_name = "${var.project_name}-bot"
  nudge_function_name   = "${var.project_name}-nudge"
  lambda_source_dir     = "${path.module}/../src"
}

# ---------------------------------------------------------------------------
# DynamoDB single-table
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "ledger" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  attribute {
    name = "GSI1PK"
    type = "S"
  }

  attribute {
    name = "GSI1SK"
    type = "S"
  }

  global_secondary_index {
    name            = "ByAssetAndDate"
    hash_key        = "GSI1PK"
    range_key       = "GSI1SK"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }
}

# ---------------------------------------------------------------------------
# Lambda packaging
# ---------------------------------------------------------------------------

# Lambda layer is the cleanest way to ship Python deps with arm64 wheels.
# In practice the user runs `pip install -r requirements.txt -t layer/python`
# before `terraform apply`; we just zip whatever sits in that directory.
data "archive_file" "deps_layer" {
  type        = "zip"
  source_dir  = "${path.module}/../layer"
  output_path = "${path.module}/.build/deps_layer.zip"
}

data "archive_file" "lambda_src" {
  type        = "zip"
  source_dir  = local.lambda_source_dir
  output_path = "${path.module}/.build/lambda_src.zip"
}

resource "aws_lambda_layer_version" "deps" {
  filename            = data.archive_file.deps_layer.output_path
  layer_name          = "${var.project_name}-deps"
  source_code_hash    = data.archive_file.deps_layer.output_base64sha256
  compatible_runtimes = ["python3.12"]
}

# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.project_name}-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "ledger_access" {
  statement {
    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:UpdateItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:DeleteItem",
    ]
    resources = [
      aws_dynamodb_table.ledger.arn,
      "${aws_dynamodb_table.ledger.arn}/index/*",
    ]
  }
}

resource "aws_iam_role_policy" "ledger_access" {
  name   = "${var.project_name}-ddb-access"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.ledger_access.json
}

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

# ---------------------------------------------------------------------------
# Webhook Lambda
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "bot" {
  name              = "/aws/lambda/${local.webhook_function_name}"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "nudge" {
  name              = "/aws/lambda/${local.nudge_function_name}"
  retention_in_days = 14
}

resource "aws_lambda_function" "bot" {
  function_name    = local.webhook_function_name
  role             = aws_iam_role.lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = 60
  memory_size      = 512
  filename         = data.archive_file.lambda_src.output_path
  source_code_hash = data.archive_file.lambda_src.output_base64sha256
  layers           = [aws_lambda_layer_version.deps.arn]

  environment {
    variables = {
      ALLOWED_TELEGRAM_USER_ID = var.allowed_telegram_user_id
      TELEGRAM_BOT_TOKEN       = var.telegram_bot_token
      LLM_PROVIDER             = var.llm_provider
      LLM_API_KEY              = var.llm_api_key
      DYNAMODB_TABLE_NAME      = aws_dynamodb_table.ledger.name
      BASE_CURRENCY            = var.base_currency
    }
  }

  depends_on = [aws_cloudwatch_log_group.bot]
}

resource "aws_lambda_function" "nudge" {
  function_name    = local.nudge_function_name
  role             = aws_iam_role.lambda.arn
  handler          = "scheduler.nudge_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = 60
  memory_size      = 256
  filename         = data.archive_file.lambda_src.output_path
  source_code_hash = data.archive_file.lambda_src.output_base64sha256
  layers           = [aws_lambda_layer_version.deps.arn]

  environment {
    variables = {
      ALLOWED_TELEGRAM_USER_ID = var.allowed_telegram_user_id
      TELEGRAM_BOT_TOKEN       = var.telegram_bot_token
      LLM_PROVIDER             = var.llm_provider
      LLM_API_KEY              = var.llm_api_key
      DYNAMODB_TABLE_NAME      = aws_dynamodb_table.ledger.name
      BASE_CURRENCY            = var.base_currency
    }
  }

  depends_on = [aws_cloudwatch_log_group.nudge]
}

# ---------------------------------------------------------------------------
# API Gateway HTTP API → Lambda proxy
# ---------------------------------------------------------------------------

resource "aws_apigatewayv2_api" "webhook" {
  name          = "${var.project_name}-webhook"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "webhook" {
  api_id                 = aws_apigatewayv2_api.webhook.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.bot.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "webhook" {
  api_id    = aws_apigatewayv2_api.webhook.id
  route_key = "POST /webhook"
  target    = "integrations/${aws_apigatewayv2_integration.webhook.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.webhook.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.bot.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.webhook.execution_arn}/*/*"
}

# ---------------------------------------------------------------------------
# EventBridge Scheduler — weekly DCA nudge
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.project_name}-scheduler-role"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler_invoke" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [
      aws_lambda_function.nudge.arn,
      aws_lambda_function.bot.arn
    ]
  }
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  name   = "${var.project_name}-invoke-nudge"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_invoke.json
}

resource "aws_scheduler_schedule" "nudge" {
  name = "${var.project_name}-weekly-nudge"

  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 15
  }

  schedule_expression          = var.nudge_schedule_expression
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_lambda_function.nudge.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

resource "aws_scheduler_schedule" "price_alerts" {
  name = "${var.project_name}-price-alerts"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.alert_schedule_expression
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_lambda_function.bot.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ "_alert_check" = true })
  }
}

