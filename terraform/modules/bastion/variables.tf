# modules/bastion/variables.tf
# Variables for the Bastion Module

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

# Instance Configuration
variable "bastion_instance_type" {
  description = "Instance type for bastion host"
  type        = string
  default     = "t3.micro"
}

# From Networking Module
variable "private_subnet_ids" {
  description = "List of private subnet IDs (bastion will use the first one)"
  type        = list(string)
}

variable "bastion_security_group_id" {
  description = "Security group ID for bastion host"
  type        = string
}

# From EKS-Core Module
variable "cluster_name" {
  description = "Name of the EKS cluster"
  type        = string
}
