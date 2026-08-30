# modules/argocd/variables.tf
# Inputs for the Argo CD Module

# ============================================
# Core Project & Environment
# ============================================

variable "project_name" {
  description = "Project name, used for resource naming"
  type        = string
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
}

variable "namespace" {
  description = "Namespace to install Argo CD into"
  type        = string
  default     = "argocd"
}

variable "chart_version" {
  description = "Version of the argo-cd Helm chart (argoproj.github.io/argo-helm)"
  type        = string
  default     = "10.4.2"
}

# ============================================
# Ingress / Access
# ============================================

variable "ingress_enabled" {
  description = "Create an ALB Ingress for the Argo CD server UI/API"
  type        = bool
  default     = true
}

variable "ingress_scheme" {
  description = "ALB scheme for the Argo CD ingress. internet-facing exposes the UI publicly."
  type        = string
  default     = "internet-facing"

  validation {
    condition     = contains(["internet-facing", "internal"], var.ingress_scheme)
    error_message = "ingress_scheme must be either internet-facing or internal"
  }
}

variable "load_balancer_name" {
  description = "Name for the Argo CD ALB. Defaults to <project>-<env>-argocd-alb when empty."
  type        = string
  default     = ""
}

# ----- TLS / hostname -------------------------------------------------------
# HTTPS requires a domain you control: ACM will not issue a certificate for an
# ALB's *.elb.amazonaws.com name. Leave both empty to stay on HTTP:80, which is
# the current behaviour. Set both to switch the ALB to HTTPS:443 with an
# HTTP -> HTTPS redirect.

variable "domain_name" {
  description = "Hostname for Argo CD, e.g. argocd.example.com. Empty means HTTP-only on the raw ALB DNS name."
  type        = string
  default     = ""
}

variable "acm_certificate_arn" {
  description = "ARN of an ACM certificate covering domain_name. Required to enable HTTPS; empty keeps the HTTP:80 listener."
  type        = string
  default     = ""

  validation {
    condition     = var.acm_certificate_arn == "" || can(regex("^arn:aws[a-zA-Z-]*:acm:", var.acm_certificate_arn))
    error_message = "acm_certificate_arn must be an ACM certificate ARN, or empty"
  }
}

variable "alb_idle_timeout_seconds" {
  description = "ALB idle timeout. Raised above the 60s default so long-lived CLI streams (argocd app logs / watch) are not cut off."
  type        = number
  default     = 300
}

# ============================================
# Argo CD Application - source
# ============================================

variable "repo_url" {
  description = "Git repository URL that Argo CD syncs the application from"
  type        = string
}

variable "target_revision" {
  description = "Git revision (branch, tag, or SHA) Argo CD tracks"
  type        = string
  default     = "main"
}

variable "app_chart_path" {
  description = "Path to the Helm chart within the repository"
  type        = string
  default     = "helm/online-boutique"
}

variable "app_name" {
  description = "Name of the Argo CD Application resource"
  type        = string
  default     = "online-boutique"
}

# ============================================
# Argo CD Application - destination
# ============================================

variable "app_namespace" {
  description = "Namespace Argo CD deploys the application into"
  type        = string
}

variable "destination_server" {
  description = "Kubernetes API server Argo CD deploys to (in-cluster by default)"
  type        = string
  default     = "https://kubernetes.default.svc"
}

# ============================================
# Argo CD Application - Helm values
# ============================================
# The chart in this repo is NOT self-sufficient: image.registry, image.prefix
# and redis.addr are empty in values.yaml and are supplied at deploy time.
# Rendered with bare defaults the chart produces image references like
# "/-frontend:latest" and omits the redis-config ConfigMap that cartservice
# mounts. These parameters supply what the chart needs so Argo CD renders
# valid manifests.

variable "app_value_files" {
  description = "Values files (relative to app_chart_path) for Argo CD to apply"
  type        = list(string)
  default     = ["values-dev.yaml"]
}

variable "app_image_registry" {
  description = "Container image registry, e.g. <account>.dkr.ecr.<region>.amazonaws.com"
  type        = string
}

variable "app_image_prefix" {
  description = "ECR repository name prefix, e.g. online-boutique-dev"
  type        = string
}

variable "app_redis_addr" {
  description = "Redis endpoint (host:port) for the cart service"
  type        = string
}

# ============================================
# Sync policy
# ============================================

variable "enable_automated_sync" {
  description = "Enable Argo CD automated sync. When true, Argo CD continuously reconciles the app namespace toward git."
  type        = bool
  default     = true
}

variable "enable_self_heal" {
  description = "Revert manual/out-of-band changes to the app namespace back to the git state"
  type        = bool
  default     = true
}

variable "enable_prune" {
  description = "Delete resources removed from git. Intentionally disabled for now."
  type        = bool
  default     = false
}
