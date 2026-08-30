# modules/argocd/main.tf
# Argo CD - GitOps continuous delivery for the Online Boutique application

locals {
  alb_name = var.load_balancer_name != "" ? var.load_balancer_name : "${var.project_name}-${var.environment}-argocd-alb"

  # HTTPS needs both a hostname and a certificate for it. Without a domain,
  # ACM cannot issue (it will not sign an ALB's *.elb.amazonaws.com name), so
  # the ALB stays on HTTP:80 and the CLI must use gRPC-Web.
  tls_enabled = var.domain_name != "" && var.acm_certificate_arn != ""

  listen_ports = local.tls_enabled ? "[{\"HTTP\": 80}, {\"HTTPS\": 443}]" : "[{\"HTTP\": 80}]"

  # Argo CD serves the UI (HTTP/1.1) and gRPC-Web (also HTTP/1.1) on the same
  # port. Native gRPC needs HTTP/2, which an ALB only offers over TLS via ALPN,
  # so HTTP1 is correct here in both modes and the CLI uses --grpc-web.
  tls_annotations = local.tls_enabled ? {
    "alb.ingress.kubernetes.io/certificate-arn" = var.acm_certificate_arn
    "alb.ingress.kubernetes.io/ssl-redirect"    = "443"
  } : {}

  argocd_url = local.tls_enabled ? "https://${var.domain_name}" : ""

  # Helm parameters passed to the application chart. See the note in
  # variables.tf: the chart cannot render valid manifests without these.
  #
  # image.tag is deliberately NOT here. It lives in values-dev.yaml so CI can
  # commit it and Argo CD can deploy the commit. Argo CD parameters override
  # valueFiles, so setting it here would silently pin the tag and stop every
  # future release from rolling out.
  app_helm_parameters = [
    { name = "image.registry", value = var.app_image_registry },
    { name = "image.prefix", value = var.app_image_prefix },
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
      server_domain = var.domain_name
      server_url    = local.argocd_url
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

    annotations = merge({
      "alb.ingress.kubernetes.io/scheme"                   = var.ingress_scheme
      "alb.ingress.kubernetes.io/target-type"              = "ip"
      "alb.ingress.kubernetes.io/load-balancer-name"       = local.alb_name
      "alb.ingress.kubernetes.io/listen-ports"             = local.listen_ports
      "alb.ingress.kubernetes.io/backend-protocol"         = "HTTP"
      "alb.ingress.kubernetes.io/backend-protocol-version" = "HTTP1"
      "alb.ingress.kubernetes.io/healthcheck-path"         = "/healthz"
      "alb.ingress.kubernetes.io/success-codes"            = "200"
      "alb.ingress.kubernetes.io/group.name"               = "${var.project_name}-${var.environment}-argocd"

      # The 60s default cuts off long-lived CLI streams (argocd app logs, and
      # any --watch command).
      "alb.ingress.kubernetes.io/load-balancer-attributes" = "idle_timeout.timeout_seconds=${var.alb_idle_timeout_seconds}"
    }, local.tls_annotations)
  }

  spec {
    ingress_class_name = "alb"

    dynamic "tls" {
      for_each = local.tls_enabled ? [1] : []
      content {
        hosts = [var.domain_name]
      }
    }

    rule {
      host = var.domain_name != "" ? var.domain_name : null

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
            # Namespace is created by modules/platform-services.
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
