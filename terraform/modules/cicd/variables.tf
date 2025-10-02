# modules/cicd/variables.tf
# Variables for the CICD Module

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

variable "eks_kms_key_arn" {
  description = "ARN of the EKS KMS key for ECR encryption"
  type        = string
}

# GitHub Configuration
variable "github_repo" {
  description = "GitHub repository in format 'owner/repo'"
  type        = string
  validation {
    condition     = can(regex("^[^/]+/[^/]+$", var.github_repo))
    error_message = "GitHub repo must be in format 'owner/repo'"
  }
}

variable "sns_topic_arn" {
  description = "SNS topic ARN for GitHub Actions notifications"
  type        = string
}
