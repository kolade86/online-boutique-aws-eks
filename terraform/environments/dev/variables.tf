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

variable "use_private_subnets_for_nodes" {
  description = "Use private subnets for worker nodes (true for production, false for dev/testing)"
  type        = bool
  default     = true
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

variable "app_name" {
  description = "Base application name (used in HPA, PDB, etc.)"
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
