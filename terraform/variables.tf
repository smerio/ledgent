variable "aws_region" {
  description = "AWS region to deploy resources into."
  type        = string
  default     = "eu-central-1"
}

variable "project_name" {
  description = "Prefix used for all named resources (Lambda functions, DynamoDB table, etc.)."
  type        = string
  default     = "crypto-ledger"
}

variable "dynamodb_table_name" {
  description = "Name of the single DynamoDB table that holds transactions, lots, and price cache."
  type        = string
  default     = "crypto-ledger"
}

variable "allowed_telegram_user_id" {
  description = "Telegram numeric user ID allowed to interact with the bot. All other senders are rejected silently."
  type        = string
  sensitive   = true
}

variable "telegram_bot_token" {
  description = "Bot token from @BotFather. Stored as a Lambda environment variable."
  type        = string
  sensitive   = true
}

variable "llm_provider" {
  description = "Which LLM provider parses incoming messages. One of: gemini, openai, claude."
  type        = string
  default     = "claude"
  validation {
    condition     = contains(["gemini", "openai", "claude"], var.llm_provider)
    error_message = "llm_provider must be one of: gemini, openai, claude."
  }
}

variable "llm_api_key" {
  description = "API key for the selected LLM provider."
  type        = string
  sensitive   = true
}

variable "base_currency" {
  description = "Currency used as the reporting unit for PnL summaries."
  type        = string
  default     = "USD"
}

variable "nudge_schedule_expression" {
  description = "EventBridge Scheduler cron expression for the weekly DCA nudge (UTC)."
  type        = string
  default     = "cron(0 9 ? * MON *)"
}
