# modules/platform-services/outputs.tf
# Outputs from the Platform-Services Module

# Namespace Outputs
output "app_namespace_name" {
  description = "Name of the application namespace"
  value       = kubernetes_namespace.app.metadata[0].name
}

output "external_secrets_namespace_name" {
  description = "Name of the external secrets namespace"
  value       = kubernetes_namespace.external_secrets_system.metadata[0].name
}

# External Secrets Outputs
output "external_secrets_operator_role_arn" {
  description = "ARN of the External Secrets Operator IAM role"
  value       = aws_iam_role.external_secrets_operator.arn
}

output "external_secrets_service_account_name" {
  description = "Name of the External Secrets service account in app namespace"
  value       = kubernetes_service_account.external_secrets_app.metadata[0].name
}

output "secret_store_name" {
  description = "Name of the SecretStore"
  value       = "${var.project_name}-${var.environment}-secret-store"
}

output "external_secret_name" {
  description = "Name of the ExternalSecret for database credentials"
  value       = "${var.project_name}-${var.environment}-db-credentials"
}

# Metrics Server Outputs
output "metrics_server_role_arn" {
  description = "ARN of the Metrics Server IAM role"
  value       = aws_iam_role.metrics_server.arn
}

output "metrics_server_service_account_name" {
  description = "Name of the Metrics Server service account"
  value       = kubernetes_service_account.metrics_server.metadata[0].name
}

# Cluster Autoscaler Outputs
output "cluster_autoscaler_role_arn" {
  description = "ARN of the Cluster Autoscaler IAM role"
  value       = aws_iam_role.cluster_autoscaler_service_account.arn
}

output "cluster_autoscaler_service_account_name" {
  description = "Name of the Cluster Autoscaler service account"
  value       = kubernetes_service_account.cluster_autoscaler.metadata[0].name
}

output "cluster_autoscaler_deployment_name" {
  description = "Name of the Cluster Autoscaler deployment"
  value       = kubernetes_deployment.cluster_autoscaler.metadata[0].name
}

# Load Balancer Controller Outputs
output "load_balancer_controller_role_arn" {
  description = "ARN of the AWS Load Balancer Controller IAM role"
  value       = aws_iam_role.aws_load_balancer_controller.arn
}

output "load_balancer_controller_service_account_name" {
  description = "Name of the AWS Load Balancer Controller service account"
  value       = kubernetes_service_account.aws_load_balancer_controller.metadata[0].name
}

# Verification Commands
output "verification_commands" {
  description = "Commands to verify platform services are working"
  value = {
    check_external_secrets   = "kubectl get pods -n ${kubernetes_namespace.external_secrets_system.metadata[0].name}"
    check_metrics_server     = "kubectl get deployment metrics-server -n kube-system"
    check_cluster_autoscaler = "kubectl get deployment ${kubernetes_deployment.cluster_autoscaler.metadata[0].name} -n kube-system"
    check_load_balancer      = "kubectl get deployment aws-load-balancer-controller -n kube-system"
    check_secret_sync        = "kubectl get secret ${var.project_name}-db-credentials -n ${kubernetes_namespace.app.metadata[0].name}"
  }
}

# Storage Class Outputs (Updated to match actual resources)
output "storage_class_gp3_standard" {
  description = "Name of the GP3 standard storage class"
  value       = kubernetes_storage_class.gp3_standard.metadata[0].name
}

output "storage_class_gp3_monitoring" {
  description = "Name of the GP3 monitoring storage class"
  value       = kubernetes_storage_class.gp3_monitoring.metadata[0].name
}

output "storage_class_efs_shared" {
  description = "Name of the EFS shared storage class"
  value       = kubernetes_storage_class.efs_shared.metadata[0].name
}

# Storage Usage Guide
output "storage_usage_guide" {
  description = "Guide for using different storage classes"
  value = {
    default_storage      = "gp3-standard (default for most workloads)"
    monitoring_storage   = "gp3-monitoring (for Prometheus and monitoring workloads)"
    shared_storage       = "efs-shared (for shared file system across pods)"
    encryption_info      = "All storage classes use default EBS encryption"
    monitoring_retention = "gp3-monitoring uses Retain policy to preserve monitoring data"
  }
}