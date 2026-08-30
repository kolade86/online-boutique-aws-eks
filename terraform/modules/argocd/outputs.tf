# modules/argocd/outputs.tf
# Outputs from the Argo CD Module

output "namespace" {
  description = "Namespace Argo CD is installed in"
  value       = kubernetes_namespace.argocd.metadata[0].name
}

output "chart_version" {
  description = "Version of the argo-cd Helm chart that was installed"
  value       = helm_release.argocd.version
}

output "release_name" {
  description = "Name of the Argo CD Helm release"
  value       = helm_release.argocd.name
}

output "ingress_hostname" {
  description = "ALB hostname for the Argo CD UI. Empty until the load balancer has been provisioned."
  value = try(
    kubernetes_ingress_v1.argocd_server[0].status[0].load_balancer[0].ingress[0].hostname,
    ""
  )
}

output "ingress_url" {
  description = "URL for the Argo CD UI. Empty until the ALB has been provisioned - re-run terraform output after a few minutes."
  value = try(
    "http://${kubernetes_ingress_v1.argocd_server[0].status[0].load_balancer[0].ingress[0].hostname}",
    ""
  )
}

output "load_balancer_name" {
  description = "Name of the ALB fronting Argo CD"
  value       = var.ingress_enabled ? local.alb_name : ""
}

output "application_name" {
  description = "Name of the Argo CD Application tracking the Online Boutique chart"
  value       = var.app_name
}

output "application_source" {
  description = "Where the Argo CD Application syncs from"
  value = {
    repo_url        = var.repo_url
    target_revision = var.target_revision
    chart_path      = var.app_chart_path
    destination_ns  = var.app_namespace
  }
}

output "sync_policy" {
  description = "Effective sync policy for the Application"
  value = {
    automated = var.enable_automated_sync
    self_heal = var.enable_self_heal
    prune     = var.enable_prune
  }
}

output "initial_admin_password_command" {
  description = "Command to read the auto-generated initial admin password"
  value       = "kubectl -n ${kubernetes_namespace.argocd.metadata[0].name} get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d"
}

output "login_info" {
  description = "How to reach and sign in to Argo CD"
  value = {
    username     = "admin"
    url          = try("http://${kubernetes_ingress_v1.argocd_server[0].status[0].load_balancer[0].ingress[0].hostname}", "(ALB pending)")
    password_cmd = "kubectl -n ${kubernetes_namespace.argocd.metadata[0].name} get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d"
    port_forward = "kubectl port-forward -n ${kubernetes_namespace.argocd.metadata[0].name} svc/argocd-server 8080:80"
    cli_login    = "argocd login <alb-hostname> --username admin --insecure"
    note         = "Change the admin password after first login, then delete the argocd-initial-admin-secret."
  }
}
