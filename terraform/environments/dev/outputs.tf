# environments/dev/outputs.tf
# Outputs from Dev Environment - Complete

# ============================================
# Networking Outputs
# ============================================

output "vpc_id" {
  description = "ID of the VPC"
  value       = module.networking.vpc_id
}

output "private_subnet_ids" {
  description = "IDs of the private subnets"
  value       = module.networking.private_subnet_ids
}

output "public_subnet_ids" {
  description = "IDs of the public subnets"
  value       = module.networking.public_subnet_ids
}

# ============================================
# EKS Outputs
# ============================================

output "cluster_id" {
  description = "EKS cluster ID"
  value       = module.eks_core.cluster_id
}

output "cluster_endpoint" {
  description = "EKS cluster endpoint"
  value       = module.eks_core.cluster_endpoint
}

output "cluster_name" {
  description = "EKS cluster name"
  value       = module.eks_core.cluster_name
}

output "kubectl_config_command" {
  description = "Command to configure kubectl"
  value       = "aws eks update-kubeconfig --region ${var.aws_region} --name ${module.eks_core.cluster_name}"
}

# ============================================
# RDS Outputs
# ============================================

output "rds_endpoint" {
  description = "RDS instance endpoint"
  value       = module.data_persistence.rds_endpoint
}

output "rds_database_name" {
  description = "RDS database name"
  value       = module.data_persistence.rds_database_name
}

output "secrets_manager_secret_name" {
  description = "AWS Secrets Manager secret name for database credentials"
  value       = module.data_persistence.secrets_manager_secret_name
}

# ============================================
# Platform Services Outputs
# ============================================

output "app_namespace" {
  description = "Application namespace"
  value       = module.platform_services.app_namespace_name
}

output "external_secrets_operator_role_arn" {
  description = "External Secrets Operator IAM role ARN"
  value       = module.platform_services.external_secrets_operator_role_arn
}

output "redis_connection_string" {
  description = "Redis connection string for cart service"
  value       = "${module.data_persistence.redis_endpoint}:${module.data_persistence.redis_port}"
}

# ============================================
# CICD Outputs
# ============================================

output "ecr_repository_urls" {
  description = "Map of ECR repository URLs for all microservices"
  value       = module.cicd.ecr_repository_urls
}

output "ecr_repository_list" {
  description = "List of all ECR repositories"
  value       = module.cicd.all_repository_urls
}

output "github_actions_role_arn" {
  description = "GitHub Actions IAM role ARN"
  value       = module.cicd.github_actions_role_arn
}

output "ecr_login_command" {
  description = "Command to login to ECR"
  value       = module.cicd.ecr_login_command
}

# ============================================
# Bastion Outputs
# ============================================

output "bastion_instance_id" {
  description = "Bastion host instance ID"
  value       = module.bastion.bastion_instance_id
}

output "bastion_connection_command" {
  description = "Command to connect to bastion via Session Manager"
  value       = module.bastion.bastion_connection_command
}

output "bastion_role_arn" {
  description = "Bastion IAM role ARN"
  value       = module.bastion.bastion_role_arn
}

# ============================================
# Observability Outputs
# ============================================

output "monitoring_namespace" {
  description = "Monitoring namespace name"
  value       = module.observability.monitoring_namespace_name
}

output "sns_topic_arn" {
  description = "SNS topic ARN for alerts"
  value       = module.observability.sns_topic_arn
}

output "cloudwatch_log_groups" {
  description = "CloudWatch log groups"
  value       = module.observability.cloudwatch_log_groups
}

# Monitoring is private (no ingress/ALB) - these surface the port-forward
# commands at the environment root so they show up after `terraform apply`.

output "monitoring_port_forward_commands" {
  description = "kubectl port-forward commands to reach Grafana, Prometheus, and Alertmanager locally"
  value       = module.observability.port_forward_commands
}

output "monitoring_local_urls" {
  description = "Local URLs available while the corresponding port-forward is running"
  value       = module.observability.monitoring_local_urls
}

output "grafana_access_info" {
  description = "How to reach Grafana: port-forward command, local URL, and admin credential lookup"
  value       = module.observability.grafana_access_info
}

# ============================================
# Quick Start Commands
# ============================================

output "quick_start_commands" {
  description = "Essential commands for getting started"
  value = {
    configure_kubectl     = "aws eks update-kubeconfig --region ${var.aws_region} --name ${module.eks_core.cluster_name}"
    connect_to_bastion    = module.bastion.bastion_connection_command
    ecr_login             = module.cicd.ecr_login_command
    check_nodes           = "kubectl get nodes"
    check_pods            = "kubectl get pods -A"
    check_external_secret = "kubectl get externalsecret -n ${module.platform_services.app_namespace_name}"
    grafana_port_forward  = module.observability.port_forward_commands.grafana
  }
}

# ============================================
# Important Post-Deployment Notes
# ============================================

output "post_deployment_notes" {
  description = "Important information after deployment"
  value = {
    sns_subscription     = "IMPORTANT: Check email ${var.alert_email_address} to confirm SNS subscription"
    bastion_access       = "Access bastion via: ${module.bastion.bastion_connection_command}"
    database_credentials = "Database credentials stored in: ${module.data_persistence.secrets_manager_secret_name}"
    github_actions_setup = "Configure GitHub Actions with role: ${module.cicd.github_actions_role_arn}"
    ecr_repositories     = "ECR repositories created: ${length(module.cicd.ecr_repository_urls)} microservices"
    check_ecr_repos      = "List all ECR repos: aws ecr describe-repositories --region ${var.aws_region}"
  }
}

# ============================================
# Argo CD Outputs
# ============================================

output "argocd_namespace" {
  description = "Namespace Argo CD is installed in"
  value       = module.argocd.namespace
}

output "argocd_url" {
  description = "URL for the Argo CD UI. Empty until the ALB is provisioned - re-run terraform output after a few minutes."
  value       = module.argocd.ingress_url
}

output "argocd_ingress_hostname" {
  description = "ALB hostname fronting the Argo CD UI"
  value       = module.argocd.ingress_hostname
}

output "argocd_initial_admin_password_command" {
  description = "Command to read the auto-generated Argo CD admin password"
  value       = module.argocd.initial_admin_password_command
}

output "argocd_login_info" {
  description = "How to reach and sign in to Argo CD (username, URL, password lookup, port-forward fallback)"
  value       = module.argocd.login_info
}

output "argocd_application" {
  description = "What the Argo CD Application tracks and where it deploys"
  value       = module.argocd.application_source
}

output "argocd_sync_policy" {
  description = "Effective Argo CD sync policy (automated / self-heal / prune)"
  value       = module.argocd.sync_policy
}
