# environments/dev/variables.tf
# Variables for Dev Environment - Complete

# ============================================
# Core Project & Environment
# ============================================

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

# ============================================
# Networking
# ============================================

variable "vpc_cidr" {
  description = "CIDR block for VPC"
  type        = string
}

# ============================================
# EKS Configuration
# ============================================

# variable "cluster_name" {
#   description = "Name of the EKS cluster"
#   type        = string
# }

variable "cluster_version" {
  description = "Kubernetes version"
  type        = string
}

# Node Group Configuration
variable "desired_size" {
  description = "Desired number of worker nodes"
  type        = number
}

variable "min_size" {
  description = "Minimum number of worker nodes"
  type        = number
}

variable "max_size" {
  description = "Maximum number of worker nodes"
  type        = number
}

variable "instance_types" {
  description = "List of EC2 instance types for worker nodes"
  type        = list(string)
}

variable "capacity_type" {
  description = "Capacity type for node group (ON_DEMAND or SPOT)"
  type        = string
  validation {
    condition     = contains(["ON_DEMAND", "SPOT"], var.capacity_type)
    error_message = "Capacity type must be either 'ON_DEMAND' or 'SPOT'."
  }
}

variable "node_update_strategy" {
  description = "Node update strategy: 'rolling' or 'blue_green'"
  type        = string
  validation {
    condition     = contains(["rolling", "blue_green"], var.node_update_strategy)
    error_message = "Node update strategy must be either 'rolling' or 'blue_green'."
  }
}

# EKS Endpoint Configuration
variable "endpoint_private_access" {
  description = "Enable private API server endpoint"
  type        = bool
  default     = true
}

variable "endpoint_public_access" {
  description = "Enable public API server endpoint"
  type        = bool
  default     = true
}

variable "public_access_cidrs" {
  description = "List of CIDR blocks that can access the public endpoint"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

# ============================================
# RDS Configuration
# ============================================

variable "postgres_version" {
  description = "PostgreSQL version"
  type        = string
}

variable "db_instance_class" {
  description = "RDS instance class"
  type        = string
}

variable "db_name" {
  description = "Name of the database to create"
  type        = string
}

variable "db_username" {
  description = "Username for the database"
  type        = string
}

variable "db_allocated_storage" {
  description = "Initial allocated storage for RDS instance (GB)"
  type        = number
}

variable "db_max_allocated_storage" {
  description = "Maximum allocated storage for RDS instance (GB)"
  type        = number
}

variable "db_multi_az" {
  description = "Enable Multi-AZ deployment for RDS"
  type        = bool
}

# ============================================
# Application Configuration
# ============================================

variable "app_namespace" {
  description = "Kubernetes namespace for the application"
  type        = string
}

# ============================================
# Platform Services
# ============================================

variable "cluster_autoscaler_version" {
  description = "Version of cluster autoscaler container image"
  type        = string
}

# ============================================
# Infrastructure Instance Types
# ============================================

variable "bastion_instance_type" {
  description = "Instance type for bastion host"
  type        = string
}

# ============================================
# CICD Configuration
# ============================================

variable "github_repo" {
  description = "GitHub repository in format 'owner/repo'"
  type        = string
}

# ============================================
# Observability Configuration
# ============================================

variable "alert_email_address" {
  description = "Email address for AlertManager notifications"
  type        = string
}

# ============================================
# Argo CD Configuration
# ============================================

variable "argocd_namespace" {
  description = "Namespace to install Argo CD into"
  type        = string
  default     = "argocd"
}

variable "argocd_chart_version" {
  description = "Version of the argo-cd Helm chart"
  type        = string
  default     = "10.4.2"
}

variable "argocd_ingress_enabled" {
  description = "Create an ALB Ingress for the Argo CD UI"
  type        = bool
  default     = true
}

variable "argocd_ingress_scheme" {
  description = "ALB scheme for the Argo CD ingress (internet-facing or internal)"
  type        = string
  default     = "internet-facing"
}

variable "argocd_repo_url" {
  description = "Git repository Argo CD syncs the application from"
  type        = string
  default     = "https://github.com/kolade86/online-boutique-aws-eks.git"
}

variable "argocd_target_revision" {
  description = "Git revision Argo CD tracks"
  type        = string
  default     = "main"
}

variable "argocd_app_chart_path" {
  description = "Path to the application Helm chart within the repository"
  type        = string
  default     = "helm/online-boutique"
}

variable "argocd_enable_automated_sync" {
  description = "Enable Argo CD automated sync. Off for now: Argo CD observes drift and reports OutOfSync, but only deploys when synced manually."
  type        = bool
  default     = false
}

variable "argocd_enable_self_heal" {
  description = "Let Argo CD revert out-of-band changes back to the git state"
  type        = bool
  default     = true
}

variable "argocd_enable_prune" {
  description = "Let Argo CD delete resources removed from git. Off for now."
  type        = bool
  default     = false
}

variable "argocd_domain_name" {
  description = "Hostname for Argo CD (e.g. argocd.example.com). Empty keeps the ALB on HTTP:80 using its raw DNS name."
  type        = string
  default     = ""
}

variable "argocd_acm_certificate_arn" {
  description = "ACM certificate ARN covering argocd_domain_name. Set together with argocd_domain_name to enable HTTPS:443 and an HTTP->HTTPS redirect."
  type        = string
  default     = ""
}
