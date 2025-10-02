# modules/data-persistence/variables.tf
# Variables for the Data-Persistence Module

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

# Network Configuration (from networking module)
variable "vpc_id" {
  description = "VPC ID where resources will be created"
  type        = string
}

variable "private_subnet_ids" {
  description = "List of private subnet IDs"
  type        = list(string)
}

variable "rds_security_group_id" {
  description = "Security group ID for RDS database"
  type        = string
}

variable "efs_security_group_id" {
  description = "Security group ID for EFS file system"
  type        = string
}

variable "redis_security_group_id" {
  description = "Security group ID for Redis cluster"
  type        = string
}

# RDS Configuration
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