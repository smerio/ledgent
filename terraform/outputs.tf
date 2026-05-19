output "webhook_url" {
  description = "Full API Gateway invoke URL to register with Telegram via setWebhook."
  value       = "${aws_apigatewayv2_api.webhook.api_endpoint}/webhook"
}

output "bot_function_name" {
  description = "Name of the webhook Lambda function."
  value       = aws_lambda_function.bot.function_name
}

output "nudge_function_name" {
  description = "Name of the weekly nudge Lambda function."
  value       = aws_lambda_function.nudge.function_name
}

output "dynamodb_table_name" {
  description = "Name of the ledger DynamoDB table."
  value       = aws_dynamodb_table.ledger.name
}

output "set_webhook_command" {
  description = "Curl command to point Telegram at the deployed webhook."
  value       = "curl -X POST 'https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook' -d 'url=${aws_apigatewayv2_api.webhook.api_endpoint}/webhook'"
}
