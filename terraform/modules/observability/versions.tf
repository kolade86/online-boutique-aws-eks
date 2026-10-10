# modules/observability/versions.tf
# Version constraints for the Observability Module

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
    # SecretStore / ExternalSecret for the SRE agent's webhook token. Like
    # platform-services, kubectl_manifest rather than kubernetes_manifest, so
    # a plan on a fresh cluster does not need the ESO CRDs to exist yet.
    kubectl = {
      source  = "gavinbunney/kubectl"
      version = ">= 1.7.0"
    }
  }
}