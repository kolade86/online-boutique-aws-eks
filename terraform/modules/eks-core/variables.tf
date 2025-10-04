# modules/eks-core/variables.tf
# Variables for the EKS-Core Module

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

# EKS Cluster Configuration
# variable "cluster_name" {
#   description = "Name of the EKS cluster"
#   type        = string
# }

variable "cluster_version" {
  description = "Kubernetes version"
  type        = string
}

# Network Configuration (from networking module)
variable "vpc_id" {
  description = "VPC ID where EKS cluster will be created"
  type        = string
}

variable "private_subnet_ids" {
  description = "List of private subnet IDs for EKS cluster"
  type        = list(string)
}

variable "public_subnet_ids" {
  description = "List of public subnet IDs for EKS cluster"
  type        = list(string)
}

variable "eks_cluster_security_group_id" {
  description = "Security group ID for EKS cluster"
  type        = string
}

variable "eks_nodes_security_group_id" {
  description = "Security group ID for EKS worker nodes"
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

# Node Subnet Configuration
variable "node_subnet_ids" {
  description = "Subnet IDs where worker nodes will be placed"
  type        = list(string)
}

# Redis Security Group Configuration
variable "enable_redis_sg_rule" {
  description = "Create SG rule allowing EKS-managed SG to access Redis"
  type        = bool
  default     = false
}

variable "redis_security_group_id" {
  description = "Security group ID for Redis cluster (optional)"
  type        = string
  default     = null
}
