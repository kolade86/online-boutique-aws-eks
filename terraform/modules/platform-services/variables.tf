# modules/platform-services/variables.tf
# Variables for the Platform-Services Module

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

# Application Configuration
variable "app_namespace" {
  description = "Kubernetes namespace for the application"
  type        = string
}

# From Networking Module
variable "vpc_id" {
  description = "VPC ID where resources will be created"
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

# From Data-Persistence Module
variable "secrets_manager_secret_arn" {
  description = "ARN of the Secrets Manager secret containing database credentials"
  type        = string
}

variable "secrets_manager_secret_name" {
  description = "Name of the Secrets Manager secret containing database credentials"
  type        = string
}

# Cluster Autoscaler Configuration
variable "cluster_autoscaler_version" {
  description = "Version of cluster autoscaler container image (must match cluster version)"
  type        = string
}

variable "eks_nodes_role_name" {
  description = "Name of the EKS nodes IAM role"
  type        = string
}

# Redis Variables 
variable "redis_endpoint" {
  description = "Redis primary endpoint address from ElastiCache"
  type        = string
}

variable "redis_port" {
  description = "Redis port"
  type        = string
  default     = "6379"
}

# Add these variables to the END of your terraform/modules/platform-services/variables.tf file

# Storage Configuration Variables (from data-persistence module)
# variable "ebs_kms_key_arn" {
#   description = "ARN of the EBS KMS encryption key for storage classes"
#   type        = string
# }

variable "efs_file_system_id" {
  description = "ID of the EFS file system for EFS storage class"
  type        = string
}