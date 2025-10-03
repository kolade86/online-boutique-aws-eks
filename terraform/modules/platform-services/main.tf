# modules/platform-services/main.tf
# Platform Services Module - External Secrets, Metrics Server, Autoscaling, and Load Balancer
# CORRECTED VERSION

# ============================================
# Application Namespace
# ============================================

resource "kubernetes_namespace" "app" {
  metadata {
    name = var.app_namespace
    labels = {
      name        = var.app_namespace
      environment = var.environment
      managed-by  = "terraform"
    }
  }
}

# ============================================
# External Secrets Operator
# ============================================

# External Secrets System Namespace
resource "kubernetes_namespace" "external_secrets_system" {
  metadata {
    name = "external-secrets-system"
    labels = {
      name = "external-secrets-system"
    }
  }
}

# IAM Role for External Secrets Operator (IRSA)
resource "aws_iam_role" "external_secrets_operator" {
  name = "${var.project_name}-external-secrets-operator"

  assume_role_policy = jsonencode({
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Condition = {
        StringEquals = {
          "${replace(var.cluster_oidc_issuer_url, "https://", "")}:sub" : [
            "system:serviceaccount:external-secrets-system:external-secrets",
            "system:serviceaccount:${var.app_namespace}:external-secrets-sa"
          ]
          "${replace(var.cluster_oidc_issuer_url, "https://", "")}:aud" : "sts.amazonaws.com"
        }
      }
      Principal = {
        Federated = var.oidc_provider_arn
      }
    }]
    Version = "2012-10-17"
  })

  tags = {
    Name        = "${var.project_name}-external-secrets-operator-role"
    Environment = var.environment
  }
}

# IAM Policy for External Secrets Operator
resource "aws_iam_policy" "external_secrets_operator" {
  name        = "${var.project_name}-external-secrets-operator"
  description = "IAM policy for External Secrets Operator to access AWS Secrets Manager"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret"
        ]
        Resource = [
          var.secrets_manager_secret_arn,
          "${var.secrets_manager_secret_arn}:*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:ListSecrets"
        ]
        Resource = "*"
      }
    ]
  })

  tags = {
    Name        = "${var.project_name}-external-secrets-operator-policy"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "external_secrets_operator" {
  policy_arn = aws_iam_policy.external_secrets_operator.arn
  role       = aws_iam_role.external_secrets_operator.name
}

# External Secrets Operator Service Account
resource "kubernetes_service_account" "external_secrets" {
  metadata {
    name      = "external-secrets"
    namespace = kubernetes_namespace.external_secrets_system.metadata[0].name
    labels = {
      "app.kubernetes.io/name"      = "external-secrets"
      "app.kubernetes.io/component" = "controller"
    }
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.external_secrets_operator.arn
    }
  }
}

# Service Account in default namespace
resource "kubernetes_service_account" "external_secrets_default" {
  metadata {
    name      = "external-secrets-sa"
    namespace = "default"
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.external_secrets_operator.arn
    }
  }
}

# Service Account for External Secrets in app namespace
resource "kubernetes_service_account" "external_secrets_app" {
  metadata {
    name      = "external-secrets-sa"
    namespace = kubernetes_namespace.app.metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.external_secrets_operator.arn
    }
  }
}

# Deploy External Secrets Operator using Helm
resource "helm_release" "external_secrets" {
  name       = "external-secrets"
  repository = "https://charts.external-secrets.io"
  chart      = "external-secrets"
  namespace  = kubernetes_namespace.external_secrets_system.metadata[0].name

   # Add these settings for better reliability
  wait            = true
  timeout         = 600
  atomic          = true
  cleanup_on_fail = true
   
  set {
    name  = "installCRDs"
    value = "true"
  }

  set {
    name  = "serviceAccount.create"
    value = "false"
  }

  set {
    name  = "serviceAccount.name"
    value = kubernetes_service_account.external_secrets.metadata[0].name
  }

  set {
    name  = "securityContext.runAsNonRoot"
    value = "true"
  }

  set {
    name  = "securityContext.runAsUser"
    value = "65534"
  }

  set {
    name  = "podSecurityContext.runAsNonRoot"
    value = "true"
  }

  set {
    name  = "podSecurityContext.runAsUser"
    value = "65534"
  }

  set {
    name  = "resources.limits.cpu"
    value = "200m"
  }

  set {
    name  = "resources.limits.memory"
    value = "256Mi"
  }

  set {
    name  = "resources.requests.cpu"
    value = "100m"
  }

  set {
    name  = "resources.requests.memory"
    value = "128Mi"
  }

  depends_on = [
    kubernetes_service_account.external_secrets,
    time_sleep.wait_for_load_balancer_controller
  ]
}

# Wait for External Secrets to be ready
resource "time_sleep" "wait_for_external_secrets" {
  depends_on      = [helm_release.external_secrets]
  create_duration = "180s"
}

# SecretStore for accessing AWS Secrets Manager - CORRECTED API VERSION
resource "kubectl_manifest" "secret_store" {
  yaml_body = yamlencode({
    apiVersion = "external-secrets.io/v1"
    kind       = "SecretStore"
    metadata = {
      name      = "${var.project_name}-${var.environment}-secret-store"
      namespace = kubernetes_namespace.app.metadata[0].name
    }
    spec = {
      provider = {
        aws = {
          service = "SecretsManager"
          region  = var.aws_region
          auth = {
            jwt = {
              serviceAccountRef = {
                name = kubernetes_service_account.external_secrets_app.metadata[0].name
              }
            }
          }
        }
      }
    }
  })

  depends_on = [
    helm_release.external_secrets,
    kubernetes_service_account.external_secrets_app,
    kubernetes_namespace.app,
    time_sleep.wait_for_external_secrets
  ]
}

# ExternalSecret to sync database credentials - CORRECTED API VERSION
resource "kubectl_manifest" "external_secret" {
  yaml_body = yamlencode({
    apiVersion = "external-secrets.io/v1"
    kind       = "ExternalSecret"
    metadata = {
      name      = "${var.project_name}-${var.environment}-db-credentials"
      namespace = kubernetes_namespace.app.metadata[0].name
    }
    spec = {
      secretStoreRef = {
        name =  "${var.project_name}-${var.environment}-secret-store"
        kind = "SecretStore"
      }
      target = {
        name           = "${var.project_name}-${var.environment}-db-credentials"
        creationPolicy = "Owner"
      }
      data = [
        {
          secretKey = "DB_HOST"
          remoteRef = {
            key      = var.secrets_manager_secret_name
            property = "host"
          }
        },
        {
          secretKey = "DB_PORT"
          remoteRef = {
            key      = var.secrets_manager_secret_name
            property = "port"
          }
        },
        {
          secretKey = "DB_NAME"
          remoteRef = {
            key      = var.secrets_manager_secret_name
            property = "dbname"
          }
        },
        {
          secretKey = "DB_USERNAME"
          remoteRef = {
            key      = var.secrets_manager_secret_name
            property = "username"
          }
        },
        {
          secretKey = "DB_PASSWORD"
          remoteRef = {
            key      = var.secrets_manager_secret_name
            property = "password"
          }
        }
      ]
      refreshInterval = "1h"
    }
  })

  depends_on = [kubectl_manifest.secret_store]
}

# ============================================
# Metrics Server & HPA
# ============================================

# IAM Role for Metrics Server (IRSA) - CORRECTED OIDC REFERENCE
resource "aws_iam_role" "metrics_server" {
  name = "${var.project_name}-metrics-server-role"

  assume_role_policy = jsonencode({
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Condition = {
        StringEquals = {
          "${replace(var.cluster_oidc_issuer_url, "https://", "")}:sub" : "system:serviceaccount:kube-system:metrics-server"
          "${replace(var.cluster_oidc_issuer_url, "https://", "")}:aud" : "sts.amazonaws.com"
        }
      }
      Principal = {
        Federated = var.oidc_provider_arn
      }
    }]
    Version = "2012-10-17"
  })

  tags = {
    Name        = "${var.project_name}-metrics-server-role"
    Environment = var.environment
  }
}

resource "aws_iam_policy" "metrics_server" {
  name        = "${var.project_name}-metrics-server-policy"
  description = "IAM policy for Metrics Server"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceTypes"
        ]
        Resource = "*"
      }
    ]
  })

  tags = {
    Name        = "${var.project_name}-metrics-server-policy"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "metrics_server" {
  policy_arn = aws_iam_policy.metrics_server.arn
  role       = aws_iam_role.metrics_server.name
}

resource "kubernetes_service_account" "metrics_server" {
  metadata {
    name      = "metrics-server"
    namespace = "kube-system"
    labels = {
      "app.kubernetes.io/name"      = "metrics-server"
      "app.kubernetes.io/component" = "metrics"
    }
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.metrics_server.arn
    }
  }
}

resource "helm_release" "metrics_server" {
  name       = "metrics-server"
  repository = "https://kubernetes-sigs.github.io/metrics-server/"
  chart      = "metrics-server"
  namespace  = "kube-system"

  wait            = true
  timeout         = 600
  atomic          = true
  cleanup_on_fail = true

  set {
    name  = "serviceAccount.create"
    value = "false"
  }

  set {
    name  = "serviceAccount.name"
    value = kubernetes_service_account.metrics_server.metadata[0].name
  }

  set {
    name  = "args"
    value = "{--kubelet-insecure-tls}"
  }

  depends_on = [
    kubernetes_service_account.metrics_server
  ]
}

resource "time_sleep" "wait_for_metrics_server" {
  depends_on      = [helm_release.metrics_server]
  create_duration = "60s"
}

# ============================================
# Cluster Autoscaler
# ============================================

# IAM Policy for Cluster Autoscaler
resource "aws_iam_policy" "cluster_autoscaler" {
  name        = "${var.project_name}-cluster-autoscaler"
  description = "IAM policy for Cluster Autoscaler"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "autoscaling:DescribeAutoScalingGroups",
          "autoscaling:DescribeAutoScalingInstances",
          "autoscaling:DescribeLaunchConfigurations",
          "autoscaling:DescribeScalingActivities",
          "autoscaling:DescribeTags",
          "ec2:DescribeInstanceTypes",
          "ec2:DescribeLaunchTemplateVersions"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "autoscaling:SetDesiredCapacity",
          "autoscaling:TerminateInstanceInAutoScalingGroup",
          "ec2:DescribeImages",
          "ec2:GetInstanceTypesFromInstanceRequirements",
          "eks:DescribeNodegroup"
        ]
        Resource = "*"
      }
    ]
  })

  tags = {
    Name        = "${var.project_name}-cluster-autoscaler-policy"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "cluster_autoscaler" {
  policy_arn = aws_iam_policy.cluster_autoscaler.arn
  role       = var.eks_nodes_role_name
}

# IAM Role for Cluster Autoscaler Service Account (IRSA) - CORRECTED OIDC REFERENCE
resource "aws_iam_role" "cluster_autoscaler_service_account" {
  name = "${var.project_name}-cluster-autoscaler-role"

  assume_role_policy = jsonencode({
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Condition = {
        StringEquals = {
          "${replace(var.cluster_oidc_issuer_url, "https://", "")}:sub" : "system:serviceaccount:kube-system:cluster-autoscaler"
          "${replace(var.cluster_oidc_issuer_url, "https://", "")}:aud" : "sts.amazonaws.com"
        }
      }
      Principal = {
        Federated = var.oidc_provider_arn
      }
    }]
    Version = "2012-10-17"
  })

  tags = {
    Name        = "${var.project_name}-cluster-autoscaler-service-account-role"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "cluster_autoscaler_service_account" {
  policy_arn = aws_iam_policy.cluster_autoscaler.arn
  role       = aws_iam_role.cluster_autoscaler_service_account.name
}

resource "kubernetes_service_account" "cluster_autoscaler" {
  metadata {
    name      = "cluster-autoscaler"
    namespace = "kube-system"
    labels = {
      "k8s-addon" = "cluster-autoscaler.addons.k8s.io"
      "k8s-app"   = "cluster-autoscaler"
    }
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.cluster_autoscaler_service_account.arn
    }
  }
}

resource "kubernetes_cluster_role" "cluster_autoscaler" {
  metadata {
    name = "cluster-autoscaler"
    labels = {
      "k8s-addon" = "cluster-autoscaler.addons.k8s.io"
      "k8s-app"   = "cluster-autoscaler"
    }
  }

  rule {
    api_groups = [""]
    resources  = ["events", "endpoints"]
    verbs      = ["create", "patch"]
  }

  rule {
    api_groups = [""]
    resources  = ["pods/eviction"]
    verbs      = ["create"]
  }

  rule {
    api_groups = [""]
    resources  = ["pods/status"]
    verbs      = ["update"]
  }

  rule {
    api_groups     = [""]
    resources      = ["endpoints"]
    resource_names = ["cluster-autoscaler"]
    verbs          = ["get", "update"]
  }

  rule {
    api_groups = [""]
    resources  = ["nodes"]
    verbs      = ["watch", "list", "get", "update"]
  }

  rule {
    api_groups = [""]
    resources = [
      "namespaces",
      "pods",
      "services",
      "replicationcontrollers",
      "persistentvolumeclaims",
      "persistentvolumes"
    ]
    verbs = ["watch", "list", "get"]
  }

  rule {
    api_groups = ["extensions"]
    resources  = ["replicasets", "daemonsets"]
    verbs      = ["watch", "list", "get"]
  }

  rule {
    api_groups = ["policy"]
    resources  = ["poddisruptionbudgets"]
    verbs      = ["watch", "list"]
  }

  rule {
    api_groups = ["apps"]
    resources  = ["statefulsets", "replicasets", "daemonsets"]
    verbs      = ["watch", "list", "get"]
  }

  rule {
    api_groups = ["storage.k8s.io"]
    resources  = ["storageclasses", "csinodes", "csidrivers", "csistoragecapacities", "volumeattachments"]
    verbs      = ["watch", "list", "get"]
  }

  rule {
    api_groups = ["batch", "extensions"]
    resources  = ["jobs"]
    verbs      = ["get", "list", "watch", "patch"]
  }

  rule {
    api_groups = ["coordination.k8s.io"]
    resources  = ["leases"]
    verbs      = ["create"]
  }

  rule {
    api_groups     = ["coordination.k8s.io"]
    resource_names = ["cluster-autoscaler"]
    resources      = ["leases"]
    verbs          = ["get", "update"]
  }
}

resource "kubernetes_cluster_role_binding" "cluster_autoscaler" {
  metadata {
    name = "cluster-autoscaler"
    labels = {
      "k8s-addon" = "cluster-autoscaler.addons.k8s.io"
      "k8s-app"   = "cluster-autoscaler"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = "cluster-autoscaler"
  }

  subject {
    kind      = "ServiceAccount"
    name      = "cluster-autoscaler"
    namespace = "kube-system"
  }
}

resource "kubernetes_role" "cluster_autoscaler" {
  metadata {
    name      = "cluster-autoscaler"
    namespace = "kube-system"
    labels = {
      "k8s-addon" = "cluster-autoscaler.addons.k8s.io"
      "k8s-app"   = "cluster-autoscaler"
    }
  }

  rule {
    api_groups = [""]
    resources  = ["configmaps"]
    verbs      = ["create", "list", "watch"]
  }

  rule {
    api_groups     = [""]
    resources      = ["configmaps"]
    resource_names = ["cluster-autoscaler-status", "cluster-autoscaler-priority-expander"]
    verbs          = ["delete", "get", "update", "watch"]
  }
}

resource "kubernetes_role_binding" "cluster_autoscaler" {
  metadata {
    name      = "cluster-autoscaler"
    namespace = "kube-system"
    labels = {
      "k8s-addon" = "cluster-autoscaler.addons.k8s.io"
      "k8s-app"   = "cluster-autoscaler"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = "cluster-autoscaler"
  }

  subject {
    kind      = "ServiceAccount"
    name      = "cluster-autoscaler"
    namespace = "kube-system"
  }
}

resource "kubernetes_deployment" "cluster_autoscaler" {
  metadata {
    name      = "cluster-autoscaler"
    namespace = "kube-system"
    labels = {
      app = "cluster-autoscaler"
    }
  }

  spec {
    selector {
      match_labels = {
        app = "cluster-autoscaler"
      }
    }

    template {
      metadata {
        labels = {
          app = "cluster-autoscaler"
        }
      }

      spec {
        priority_class_name              = "system-cluster-critical"
        service_account_name             = "cluster-autoscaler"
        termination_grace_period_seconds = 10

        security_context {
          run_as_non_root = true
          run_as_user     = 65534
          fs_group        = 65534
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }

        container {
          image = "registry.k8s.io/autoscaling/cluster-autoscaler:${var.cluster_autoscaler_version}"
          name  = "cluster-autoscaler"

          resources {
            limits = {
              cpu    = "100m"
              memory = "600Mi"
            }
            requests = {
              cpu    = "100m"
              memory = "600Mi"
            }
          }

          command = [
            "./cluster-autoscaler",
            "--v=4",
            "--stderrthreshold=info",
            "--cloud-provider=aws",
            "--skip-nodes-with-local-storage=false",
            "--expander=least-waste",
            "--node-group-auto-discovery=asg:tag=k8s.io/cluster-autoscaler/enabled,k8s.io/cluster-autoscaler/${var.cluster_name}",
            "--balance-similar-node-groups",
            "--skip-nodes-with-system-pods=false"
          ]

          volume_mount {
            name       = "ssl-certs"
            mount_path = "/etc/ssl/certs/ca-certificates.crt"
            read_only  = true
          }

          image_pull_policy = "Always"

          security_context {
            allow_privilege_escalation = false
            capabilities {
              drop = ["ALL"]
            }
            read_only_root_filesystem = true
          }
        }

        volume {
          name = "ssl-certs"
          host_path {
            path = "/etc/ssl/certs/ca-bundle.crt"
          }
        }

        node_selector = {
          "kubernetes.io/os" = "linux"
        }
      }
    }
  }

  depends_on = [
    kubernetes_service_account.cluster_autoscaler,
    kubernetes_cluster_role_binding.cluster_autoscaler,
    kubernetes_role_binding.cluster_autoscaler
  ]
}

# ============================================
# AWS Load Balancer Controller
# ============================================

# CORRECTED OIDC REFERENCE
resource "aws_iam_role" "aws_load_balancer_controller" {
  name = "${var.project_name}-aws-load-balancer-controller"

  assume_role_policy = jsonencode({
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Condition = {
        StringEquals = {
          "${replace(var.cluster_oidc_issuer_url, "https://", "")}:sub" : "system:serviceaccount:kube-system:aws-load-balancer-controller"
          "${replace(var.cluster_oidc_issuer_url, "https://", "")}:aud" : "sts.amazonaws.com"
        }
      }
      Principal = {
        Federated = var.oidc_provider_arn
      }
    }]
    Version = "2012-10-17"
  })

  tags = {
    Name        = "${var.project_name}-aws-load-balancer-controller-role"
    Environment = var.environment
  }
}

resource "aws_iam_policy" "aws_load_balancer_controller" {
  name        = "${var.project_name}-AWSLoadBalancerControllerIAMPolicy"
  description = "IAM policy for AWS Load Balancer Controller"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "iam:CreateServiceLinkedRole"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "iam:AWSServiceName" = "elasticloadbalancing.amazonaws.com"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeAccountAttributes",
          "ec2:DescribeAddresses",
          "ec2:DescribeAvailabilityZones",
          "ec2:DescribeInternetGateways",
          "ec2:DescribeVpcs",
          "ec2:DescribeVpcPeeringConnections",
          "ec2:DescribeSubnets",
          "ec2:DescribeSecurityGroups",
          "ec2:GetSecurityGroupsForVpc",
          "ec2:DescribeInstances",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DescribeTags",
          "ec2:GetCoipPoolUsage",
          "ec2:GetIpamPoolCidrs",
          "ec2:DescribeCoipPools",
          "elasticloadbalancing:DescribeLoadBalancers",
          "elasticloadbalancing:DescribeLoadBalancerAttributes",
          "elasticloadbalancing:DescribeListeners",
          "elasticloadbalancing:DescribeListenerCertificates",
          "elasticloadbalancing:DescribeListenerAttributes",
          "elasticloadbalancing:DescribeSSLPolicies",
          "elasticloadbalancing:DescribeRules",
          "elasticloadbalancing:DescribeTargetGroups",
          "elasticloadbalancing:DescribeTargetGroupAttributes",
          "elasticloadbalancing:DescribeTargetHealth",
          "elasticloadbalancing:DescribeTags"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "cognito-idp:DescribeUserPoolClient",
          "acm:ListCertificates",
          "acm:DescribeCertificate",
          "iam:ListServerCertificates",
          "iam:GetServerCertificate",
          "waf-regional:GetWebACL",
          "waf-regional:GetWebACLForResource",
          "waf-regional:AssociateWebACL",
          "waf-regional:DisassociateWebACL",
          "wafv2:GetWebACL",
          "wafv2:GetWebACLForResource",
          "wafv2:AssociateWebACL",
          "wafv2:DisassociateWebACL",
          "shield:DescribeProtection",
          "shield:GetSubscriptionState",
          "shield:DescribeSubscription",
          "shield:CreateProtection",
          "shield:DeleteProtection"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:RevokeSecurityGroupIngress"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:CreateSecurityGroup"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:CreateTags"
        ]
        Resource = "arn:aws:ec2:*:*:security-group/*"
        Condition = {
          StringEquals = {
            "ec2:CreateAction" = "CreateSecurityGroup"
          }
          Null = {
            "aws:RequestTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:CreateTags",
          "ec2:DeleteTags"
        ]
        Resource = "arn:aws:ec2:*:*:security-group/*"
        Condition = {
          Null = {
            "aws:RequestTag/elbv2.k8s.aws/cluster"  = "true"
            "aws:ResourceTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:RevokeSecurityGroupIngress",
          "ec2:DeleteSecurityGroup"
        ]
        Resource = "*"
        Condition = {
          Null = {
            "aws:ResourceTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:CreateLoadBalancer",
          "elasticloadbalancing:CreateTargetGroup"
        ]
        Resource = "*"
        Condition = {
          Null = {
            "aws:RequestTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:CreateListener",
          "elasticloadbalancing:DeleteListener",
          "elasticloadbalancing:CreateRule",
          "elasticloadbalancing:DeleteRule"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:AddTags",
          "elasticloadbalancing:RemoveTags"
        ]
        Resource = [
          "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*",
          "arn:aws:elasticloadbalancing:*:*:loadbalancer/net/*/*",
          "arn:aws:elasticloadbalancing:*:*:loadbalancer/app/*/*"
        ]
        Condition = {
          Null = {
            "aws:RequestTag/elbv2.k8s.aws/cluster"  = "true"
            "aws:ResourceTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:AddTags",
          "elasticloadbalancing:RemoveTags"
        ]
        Resource = [
          "arn:aws:elasticloadbalancing:*:*:listener/net/*/*/*",
          "arn:aws:elasticloadbalancing:*:*:listener/app/*/*/*",
          "arn:aws:elasticloadbalancing:*:*:listener-rule/net/*/*/*",
          "arn:aws:elasticloadbalancing:*:*:listener-rule/app/*/*/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:ModifyLoadBalancerAttributes",
          "elasticloadbalancing:SetIpAddressType",
          "elasticloadbalancing:SetSecurityGroups",
          "elasticloadbalancing:SetSubnets",
          "elasticloadbalancing:DeleteLoadBalancer",
          "elasticloadbalancing:ModifyTargetGroup",
          "elasticloadbalancing:ModifyTargetGroupAttributes",
          "elasticloadbalancing:DeleteTargetGroup"
        ]
        Resource = "*"
        Condition = {
          Null = {
            "aws:ResourceTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:AddTags"
        ]
        Resource = [
          "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*",
          "arn:aws:elasticloadbalancing:*:*:loadbalancer/net/*/*",
          "arn:aws:elasticloadbalancing:*:*:loadbalancer/app/*/*"
        ]
        Condition = {
          StringEquals = {
            "elasticloadbalancing:CreateAction" = [
              "CreateTargetGroup",
              "CreateLoadBalancer"
            ]
          }
          Null = {
            "aws:RequestTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:RegisterTargets",
          "elasticloadbalancing:DeregisterTargets"
        ]
        Resource = "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*"
      }
    ]
  })

  tags = {
    Name        = "${var.project_name}-aws-load-balancer-controller-policy"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "aws_load_balancer_controller" {
  policy_arn = aws_iam_policy.aws_load_balancer_controller.arn
  role       = aws_iam_role.aws_load_balancer_controller.name
}

resource "kubernetes_service_account" "aws_load_balancer_controller" {
  metadata {
    name      = "aws-load-balancer-controller"
    namespace = "kube-system"
    labels = {
      "app.kubernetes.io/component" = "controller"
      "app.kubernetes.io/name"      = "aws-load-balancer-controller"
    }
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.aws_load_balancer_controller.arn
    }
  }
}

resource "helm_release" "aws_load_balancer_controller" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  namespace  = "kube-system"

  set {
    name  = "clusterName"
    value = var.cluster_name
  }

  set {
    name  = "serviceAccount.create"
    value = "false"
  }

  set {
    name  = "serviceAccount.name"
    value = kubernetes_service_account.aws_load_balancer_controller.metadata[0].name
  }

  set {
    name  = "region"
    value = var.aws_region
  }

  set {
    name  = "vpcId"
    value = var.vpc_id
  }

  depends_on = [
    kubernetes_service_account.aws_load_balancer_controller
  ]
}

resource "time_sleep" "wait_for_load_balancer_controller" {
  depends_on      = [helm_release.aws_load_balancer_controller]
  create_duration = "120s"  # Wait for controller to be fully ready
}

#=======================================================
# Simple ConfigMap for Redis connection (non-sensitive)
#=======================================================
resource "kubernetes_config_map" "redis_config" {
  metadata {
    name      = "redis-config"
    namespace = kubernetes_namespace.app.metadata[0].name
  }

  data = {
    REDIS_ADDR = "${var.redis_endpoint}:${var.redis_port}"
  }

  depends_on = [kubernetes_namespace.app]
}

# ============================================
# Kubernetes Storage Classes
# ============================================

# GP3 Storage Class - General Purpose (Default)
resource "kubernetes_storage_class" "gp3_standard" {
  metadata {
    name = "gp3-standard"
    annotations = {
      "storageclass.kubernetes.io/is-default-class" = "true"
    }
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "storage-tier"                 = "standard"
    }
  }

  storage_provisioner    = "ebs.csi.aws.com"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true
  reclaim_policy         = "Delete"

  parameters = {
    type       = "gp3"
    iops       = "3000" # Baseline IOPS for gp3
    throughput = "125"  # MiB/s
    encrypted  = "true"
    kmsKeyId   = var.ebs_kms_key_arn
    fsType     = "ext4"
  }

  depends_on = [helm_release.aws_load_balancer_controller]
}

# GP3 High Performance Storage Class
resource "kubernetes_storage_class" "gp3_high_performance" {
  metadata {
    name = "gp3-high-performance"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "storage-tier"                 = "high-performance"
    }
  }

  storage_provisioner    = "ebs.csi.aws.com"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true
  reclaim_policy         = "Delete"

  parameters = {
    type       = "gp3"
    iops       = "6000" # Higher IOPS for demanding apps
    throughput = "250"  # Higher throughput
    encrypted  = "true"
    kmsKeyId   = var.ebs_kms_key_arn
    fsType     = "ext4"
  }

  depends_on = [helm_release.aws_load_balancer_controller]
}

# IO2 Storage Class - High IOPS for databases
resource "kubernetes_storage_class" "io2_database" {
  metadata {
    name = "io2-database"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "storage-tier"                 = "database"
    }
  }

  storage_provisioner    = "ebs.csi.aws.com"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true
  reclaim_policy         = "Retain" # Retain for databases

  parameters = {
    type      = "io2"
    iops      = "10000" # High IOPS for database workloads
    encrypted = "true"
    kmsKeyId  = var.ebs_kms_key_arn
    fsType    = "ext4"
  }

  depends_on = [helm_release.aws_load_balancer_controller]
}

# GP3 Retain Storage Class - For critical data
resource "kubernetes_storage_class" "gp3_retain" {
  metadata {
    name = "gp3-retain"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "storage-tier"                 = "retain"
    }
  }

  storage_provisioner    = "ebs.csi.aws.com"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true
  reclaim_policy         = "Retain" # Don't delete volumes when PVC is deleted

  parameters = {
    type       = "gp3"
    iops       = "3000"
    throughput = "125"
    encrypted  = "true"
    kmsKeyId   = var.ebs_kms_key_arn
    fsType     = "ext4"
  }

  depends_on = [helm_release.aws_load_balancer_controller]
}

# EFS Storage Class
resource "kubernetes_storage_class" "efs_shared" {
  metadata {
    name = "efs-shared"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "storage-tier"                 = "shared"
    }
  }

  storage_provisioner    = "efs.csi.aws.com"
  volume_binding_mode    = "Immediate"
  allow_volume_expansion = false
  reclaim_policy         = "Retain"

  parameters = {
    provisioningMode = "efs-ap" # EFS Access Points
    fileSystemId     = var.efs_file_system_id
    directoryPerms   = "0755"
    gidRangeStart    = "1000"
    gidRangeEnd      = "2000"
    basePath         = "/dynamic_provisioning"
  }

  depends_on = [helm_release.aws_load_balancer_controller]
}