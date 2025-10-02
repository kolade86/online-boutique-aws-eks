# modules/observability/outputs.tf
# Outputs from the Observability Module

# Namespace Outputs
output "monitoring_namespace_name" {
  description = "Name of the monitoring namespace"
  value       = kubernetes_namespace.monitoring.metadata[0].name
}

# CloudWatch Log Groups
output "cloudwatch_log_groups" {
  description = "CloudWatch Log Groups for container insights"
  value = {
    application_logs = aws_cloudwatch_log_group.application_logs.name
    dataplane_logs   = aws_cloudwatch_log_group.dataplane_logs.name
    host_logs        = aws_cloudwatch_log_group.host_logs.name
    performance_logs = aws_cloudwatch_log_group.performance_logs.name
    retention_days   = "7 days"
  }
}

output "cloudwatch_log_group_arns" {
  description = "ARNs of the CloudWatch Log Groups"
  value = {
    application_logs = aws_cloudwatch_log_group.application_logs.arn
    dataplane_logs   = aws_cloudwatch_log_group.dataplane_logs.arn
    host_logs        = aws_cloudwatch_log_group.host_logs.arn
    performance_logs = aws_cloudwatch_log_group.performance_logs.arn
  }
}

# CloudWatch Observability
output "cloudwatch_observability_addon_info" {
  description = "CloudWatch Observability add-on information"
  value = {
    addon_name               = aws_eks_addon.cloudwatch_observability.addon_name
    addon_version            = aws_eks_addon.cloudwatch_observability.addon_version
    arn                      = aws_eks_addon.cloudwatch_observability.arn
    service_account_role_arn = aws_iam_role.cloudwatch_observability.arn
  }
}

# Prometheus
output "prometheus_role_arn" {
  description = "ARN of the Prometheus IAM role"
  value       = aws_iam_role.prometheus.arn
}

output "prometheus_role_name" {
  description = "Name of the Prometheus IAM role"
  value       = aws_iam_role.prometheus.name
}

# SNS & AlertManager
output "sns_topic_arn" {
  description = "ARN of the SNS topic for AlertManager notifications"
  value       = aws_sns_topic.alertmanager_notifications.arn
}

output "sns_topic_name" {
  description = "Name of the SNS topic"
  value       = aws_sns_topic.alertmanager_notifications.name
}

output "alertmanager_role_arn" {
  description = "ARN of the AlertManager SNS IAM role"
  value       = aws_iam_role.alertmanager_sns.arn
}

output "alertmanager_secret_name" {
  description = "Name of the AlertManager SNS config secret"
  value       = kubernetes_secret.alertmanager_sns_config.metadata[0].name
}

# Verification Commands
output "verification_commands" {
  description = "Commands to verify observability setup"
  value = {
    check_cloudwatch_pods      = "kubectl get pods -n amazon-cloudwatch"
    check_monitoring_namespace = "kubectl get namespace ${kubernetes_namespace.monitoring.metadata[0].name}"
    view_application_logs      = "aws logs tail '${aws_cloudwatch_log_group.application_logs.name}' --follow --region ${var.aws_region}"
    check_sns_subscription     = "aws sns list-subscriptions-by-topic --topic-arn ${aws_sns_topic.alertmanager_notifications.arn} --region ${var.aws_region}"
    check_addon_status         = "aws eks describe-addon --cluster-name ${var.cluster_name} --addon-name amazon-cloudwatch-observability --region ${var.aws_region}"
  }
}

# Console URLs
output "cloudwatch_console_urls" {
  description = "URLs to access monitoring in AWS Console"
  value = {
    cloudwatch_logs    = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#logsV2:log-groups"
    container_insights = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#container-insights:infrastructure"
    sns_subscriptions  = "https://${var.aws_region}.console.aws.amazon.com/sns/v3/home?region=${var.aws_region}#/topic/${aws_sns_topic.alertmanager_notifications.arn}"
  }
}

# Important Notes
output "important_notes" {
  description = "Important post-deployment notes"
  value = {
    email_confirmation = "IMPORTANT: Check your email (${var.alert_email_address}) to confirm the SNS subscription"
    log_retention      = "CloudWatch logs are retained for 7 days to minimize costs"
    prometheus_setup   = "Deploy Prometheus using Helm chart and reference the IAM role: ${aws_iam_role.prometheus.arn}"
    alertmanager_setup = "Configure AlertManager to use the secret: ${kubernetes_secret.alertmanager_sns_config.metadata[0].name}"
  }
}
