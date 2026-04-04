# modules/observability/variables.tf
# Variables for the Observability Module

variable "project_name" {
  description = "Project name used as prefix for all resources"
  type        = string
}

variable "environment" {
  description = "Environment name (prod, staging, dev)"
  type        = string
  validation {
    condition     = contains(["prod", "staging", "dev"], var.environment)
    error_message = "Environment must be one of: prod, staging, dev"
  }
}

variable "aws_region" {
  description = "AWS region for deployment"
  type        = string
}

variable "aws_account_id" {
  description = "AWS Account ID"
  type        = string
}

# From EKS-Core Module
variable "cluster_name" {
  description = "Name of the EKS cluster"
  type        = string
}

variable "oidc_provider_arn" {
  description = "ARN of the OIDC provider for EKS"
  type        = string
}

variable "cluster_oidc_issuer_url" {
  description = "OIDC issuer URL for EKS cluster (for IRSA assume role policies)"
  type        = string
}

variable "eks_nodes_role_arn" {
  description = "ARN of the EKS nodes IAM role"
  type        = string
}

# Alerting Configuration
variable "alert_email_address" {
  description = "Email address for AlertManager notifications"
  type        = string
  validation {
    condition     = can(regex("^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$", var.alert_email_address))
    error_message = "Must be a valid email address"
  }
}

variable "eks_nodes_role_name" {
  description = "Name of the EKS nodes IAM role"
  type        = string
}

# ============================================
# Grafana Cloud AIOps Configuration
# ============================================

variable "grafana_cloud_remote_write_url" {
  description = "Grafana Cloud Prometheus remote write endpoint URL"
  type        = string
  default     = ""
}

variable "grafana_cloud_remote_write_username" {
  description = "Grafana Cloud Prometheus remote write username (Metrics instance ID)"
  type        = string
  default     = ""
}

variable "grafana_cloud_remote_write_password" {
  description = "Grafana Cloud API token for remote write"
  type        = string
  sensitive   = true
  default     = ""
}

variable "enable_grafana_cloud" {
  description = "Enable Grafana Cloud remote write for AIOps ML features"
  type        = bool
  default     = false
}

# ============================================
# CloudWatch Alarms — AWS Managed Services
# ============================================

variable "rds_instance_identifier" {
  description = "RDS instance identifier for CloudWatch alarms"
  type        = string
  default     = ""
}

variable "redis_replication_group_id" {
  description = "ElastiCache Redis replication group ID for CloudWatch alarms"
  type        = string
  default     = ""
}

variable "alb_arn_suffix" {
  description = "ALB ARN suffix for CloudWatch alarms (e.g. app/my-alb/1234567890)"
  type        = string
  default     = ""
}