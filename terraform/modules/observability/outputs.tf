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


# Add these outputs to your terraform/modules/observability/outputs.tf

# kube-prometheus-stack Outputs
output "prometheus_stack_release_name" {
  description = "Name of the kube-prometheus-stack Helm release"
  value       = helm_release.kube_prometheus_stack.name
}

output "prometheus_stack_namespace" {
  description = "Namespace where kube-prometheus-stack is deployed"
  value       = helm_release.kube_prometheus_stack.namespace
}

output "grafana_admin_password" {
  description = "Grafana admin password (sensitive)"
  value       = random_password.grafana_admin_password.result
  sensitive   = true
}

output "grafana_admin_credentials_secret" {
  description = "Name of the Kubernetes secret containing Grafana admin credentials"
  value       = kubernetes_secret.grafana_admin_credentials.metadata[0].name
}

# Monitoring Stack Service Information
output "monitoring_services" {
  description = "Information about monitoring services"
  value = {
    prometheus_service   = "monitoring-kube-prometheus-prometheus"
    grafana_service      = "monitoring-grafana"
    alertmanager_service = "monitoring-kube-prometheus-alertmanager"
    namespace            = kubernetes_namespace.monitoring.metadata[0].name
  }
}

# Enhanced verification commands
output "enhanced_verification_commands" {
  description = "Commands to verify monitoring stack"
  value = {
    check_prometheus_pods   = "kubectl get pods -n ${kubernetes_namespace.monitoring.metadata[0].name} -l app.kubernetes.io/name=prometheus"
    check_grafana_pods      = "kubectl get pods -n ${kubernetes_namespace.monitoring.metadata[0].name} -l app.kubernetes.io/name=grafana"
    check_alertmanager_pods = "kubectl get pods -n ${kubernetes_namespace.monitoring.metadata[0].name} -l app.kubernetes.io/name=alertmanager"
    check_all_monitoring    = "kubectl get all -n ${kubernetes_namespace.monitoring.metadata[0].name}"
    get_grafana_password    = "kubectl get secret ${kubernetes_secret.grafana_admin_credentials.metadata[0].name} -n ${kubernetes_namespace.monitoring.metadata[0].name} -o jsonpath='{.data.admin-password}' | base64 -d"
    port_forward_grafana    = "kubectl port-forward -n ${kubernetes_namespace.monitoring.metadata[0].name} svc/monitoring-grafana 3000:80"
    check_prometheus_config = "kubectl get prometheus -n ${kubernetes_namespace.monitoring.metadata[0].name}"
    check_servicemonitors   = "kubectl get servicemonitor -n ${kubernetes_namespace.monitoring.metadata[0].name}"
  }
}

# Grafana access information
output "grafana_access_info" {
  description = "Information for accessing Grafana"
  value = {
    url              = "http://monitoring-${var.environment}.${var.project_name}.local/grafana/"
    port_forward_cmd = "kubectl port-forward -n ${kubernetes_namespace.monitoring.metadata[0].name} svc/monitoring-grafana 3000:80"
    username         = "admin"
    password_secret  = kubernetes_secret.grafana_admin_credentials.metadata[0].name
    namespace        = kubernetes_namespace.monitoring.metadata[0].name
  }
}

# Monitoring Access URLs
output "monitoring_urls" {
  description = "URLs to access all monitoring services"
  value = {
    grafana      = "http://monitoring-${var.environment}.${var.project_name}.local/grafana/"
    prometheus   = "http://monitoring-${var.environment}.${var.project_name}.local/prometheus/"
    alertmanager = "http://monitoring-${var.environment}.${var.project_name}.local/alertmanager/"
    ingress_host = "monitoring-${var.environment}.${var.project_name}.local"
    alb_name     = "${var.project_name}-${var.environment}-monitoring-alb"
  }
}

# Ingress information
output "monitoring_ingress_info" {
  description = "Information about the monitoring ingress"
  value = {
    name          = kubernetes_ingress_v1.monitoring_ingress.metadata[0].name
    namespace     = kubernetes_namespace.monitoring.metadata[0].name
    hostname      = "monitoring-${var.environment}.${var.project_name}.local"
    load_balancer = "${var.project_name}-${var.environment}-monitoring-alb"
    class         = "alb"
  }
}

# Storage information
output "monitoring_storage_info" {
  description = "Storage information for monitoring components"
  value = {
    prometheus_storage_class   = "gp3-monitoring"
    prometheus_storage_size    = "50Gi"
    grafana_storage_class      = "gp3-monitoring"
    grafana_storage_size       = "10Gi"
    alertmanager_storage_class = "gp3-monitoring"
    alertmanager_storage_size  = "10Gi"
    retention_period           = "30 days"
  }
}