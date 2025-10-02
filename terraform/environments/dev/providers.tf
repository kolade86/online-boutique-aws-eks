# environments/dev/providers.tf
# Provider configurations for Dev Environment

# Terraform and provider requirements - MUST COME FIRST
terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.20"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.9"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.1"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.9"
    }
    kubectl = {
      source  = "gavinbunney/kubectl"
      version = ">= 1.7.0"
    }
  }
}

# AWS Provider
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Environment = var.environment
      Project     = var.project_name
      ManagedBy   = "terraform"
    }
  }
}

# Kubernetes provider configuration - depends on EKS cluster
provider "kubernetes" {
  host                   = module.eks_core.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks_core.cluster_ca_certificate)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks_core.cluster_name]
  }
}

# Helm provider configuration
provider "helm" {
  kubernetes {
    host                   = module.eks_core.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks_core.cluster_ca_certificate)
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks_core.cluster_name]
    }
  }
}

# Kubectl provider configuration
provider "kubectl" {
  host                   = module.eks_core.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks_core.cluster_ca_certificate)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks_core.cluster_name]
  }
  load_config_file = false
}