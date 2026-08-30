# modules/argocd/main.tf
# Argo CD - GitOps continuous delivery for the Online Boutique application

locals {
  alb_name = var.load_balancer_name != "" ? var.load_balancer_name : "${var.project_name}-${var.environment}-argocd-alb"

  # Helm parameters passed to the application chart. See the note in
  # variables.tf: the chart cannot render valid manifests without these.
  app_helm_parameters = [
    { name = "image.registry", value = var.app_image_registry },
    { name = "image.prefix", value = var.app_image_prefix },
    { name = "image.tag", value = var.app_image_tag },
    { name = "redis.addr", value = var.app_redis_addr },
  ]
}

# ============================================
# Namespace
# ============================================

resource "kubernetes_namespace" "argocd" {
  metadata {
    name = var.namespace

    labels = {
      name                           = var.namespace
      "app.kubernetes.io/part-of"    = "argocd"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

# ============================================
# Argo CD Helm Release
# ============================================

resource "helm_release" "argocd" {
  name       = "argocd"
  repository = "https://argoproj.github.io/argo-helm"
  chart      = "argo-cd"
  namespace  = kubernetes_namespace.argocd.metadata[0].name
  version    = var.chart_version

  wait            = true
  timeout         = 900
  atomic          = true
  cleanup_on_fail = true

  values = [
    templatefile("${path.module}/argocd-values.yaml.tftpl", {
      server_domain = local.alb_name
    })
  ]

  depends_on = [kubernetes_namespace.argocd]
}

# Give the CRDs and the API server time to settle before creating an
# Application and an Ingress that reference them.
resource "time_sleep" "wait_for_argocd" {
  depends_on      = [helm_release.argocd]
  create_duration = "60s"
}

# ============================================
# Ingress (ALB via AWS Load Balancer Controller)
# ============================================
# Managed here rather than by the chart so the ALB hostname is available as a
# module output.

resource "kubernetes_ingress_v1" "argocd_server" {
  count = var.ingress_enabled ? 1 : 0

  metadata {
    name      = "argocd-server-ingress"
    namespace = kubernetes_namespace.argocd.metadata[0].name

    annotations = {
      "alb.ingress.kubernetes.io/scheme"             = var.ingress_scheme
      "alb.ingress.kubernetes.io/target-type"        = "ip"
      "alb.ingress.kubernetes.io/load-balancer-name" = local.alb_name
      "alb.ingress.kubernetes.io/listen-ports"       = "[{\"HTTP\": 80}]"
      "alb.ingress.kubernetes.io/backend-protocol"   = "HTTP"
      "alb.ingress.kubernetes.io/healthcheck-path"   = "/healthz"
      "alb.ingress.kubernetes.io/success-codes"      = "200"
      "alb.ingress.kubernetes.io/group.name"         = "${var.project_name}-${var.environment}-argocd"

      # TLS is not configured yet. Add a certificate-arn and an HTTPS listener
      # before this is used for anything beyond a sandbox environment.
    }
  }

  spec {
    ingress_class_name = "alb"

    rule {
      http {
        path {
          path      = "/"
          path_type = "Prefix"

          backend {
            service {
              name = "argocd-server"
              port {
                number = 80
              }
            }
          }
        }
      }
    }
  }

  depends_on = [
    helm_release.argocd,
    time_sleep.wait_for_argocd
  ]
}

# ============================================
# Argo CD Application - Online Boutique
# ============================================

resource "kubectl_manifest" "online_boutique" {
  yaml_body = yamlencode({
    apiVersion = "argoproj.io/v1alpha1"
    kind       = "Application"
    metadata = {
      name      = var.app_name
      namespace = kubernetes_namespace.argocd.metadata[0].name
      # Keeps the Application around if the namespace is torn down out of band.
      finalizers = ["resources-finalizer.argocd.argoproj.io"]
    }
    spec = {
      project = "default"

      source = {
        repoURL        = var.repo_url
        targetRevision = var.target_revision
        path           = var.app_chart_path

        helm = {
          valueFiles = var.app_value_files
          parameters = local.app_helm_parameters
        }
      }

      destination = {
        server    = var.destination_server
        namespace = var.app_namespace
      }

      syncPolicy = merge(
        var.enable_automated_sync ? {
          automated = {
            selfHeal = var.enable_self_heal
            prune    = var.enable_prune
          }
        } : {},
        {
          syncOptions = [
            "CreateNamespace=true",
            "ApplyOutOfSyncOnly=true",
          ]
          retry = {
            limit = 5
            backoff = {
              duration    = "10s"
              factor      = 2
              maxDuration = "3m"
            }
          }
        }
      )
    }
  })

  depends_on = [
    helm_release.argocd,
    time_sleep.wait_for_argocd
  ]
}
