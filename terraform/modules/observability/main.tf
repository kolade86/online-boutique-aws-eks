# modules/observability/main.tf
# Observability Module - Monitoring, Logging, and Alerting

# ============================================
# Monitoring Namespace
# ============================================

resource "kubernetes_namespace" "monitoring" {
  metadata {
    name = "monitoring"
    labels = {
      name        = "monitoring"
      environment = var.environment
      managed-by  = "terraform"
    }
  }
}

# ============================================
# CloudWatch Log Groups
# ============================================

resource "aws_cloudwatch_log_group" "application_logs" {
  name              = "/aws/containerinsights/${var.cluster_name}/application"
  retention_in_days = 7

  tags = {
    Name        = "${var.project_name}-application-logs"
    Environment = var.environment
    Component   = "logging"
  }
}

resource "aws_cloudwatch_log_group" "dataplane_logs" {
  name              = "/aws/containerinsights/${var.cluster_name}/dataplane"
  retention_in_days = 7

  tags = {
    Name        = "${var.project_name}-dataplane-logs"
    Environment = var.environment
    Component   = "logging"
  }
}

resource "aws_cloudwatch_log_group" "host_logs" {
  name              = "/aws/containerinsights/${var.cluster_name}/host"
  retention_in_days = 7

  tags = {
    Name        = "${var.project_name}-host-logs"
    Environment = var.environment
    Component   = "logging"
  }
}

resource "aws_cloudwatch_log_group" "performance_logs" {
  name              = "/aws/containerinsights/${var.cluster_name}/performance"
  retention_in_days = 7

  tags = {
    Name        = "${var.project_name}-performance-logs"
    Environment = var.environment
    Component   = "logging"
  }
}

# ============================================
# CloudWatch Observability (Fluent Bit + Agent)
# ============================================

# IAM Policy for CloudWatch Observability
resource "aws_iam_policy" "cloudwatch_observability" {
  name        = "${var.project_name}-cloudwatch-observability"
  description = "IAM policy for CloudWatch Observability add-on (Fluent Bit + CloudWatch Agent)"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams",
          "logs:DescribeLogGroups"
        ]
        Resource = [
          "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:/aws/containerinsights/${var.cluster_name}/*",
          "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:/aws/containerinsights/${var.cluster_name}/*:*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData",
          "ec2:DescribeVolumes",
          "ec2:DescribeTags",
          "logs:PutLogEvents",
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:DescribeLogStreams",
          "logs:DescribeLogGroups",
          "logs:PutRetentionPolicy"
        ]
        Resource = "*"
      }
    ]
  })

  tags = {
    Name        = "${var.project_name}-cloudwatch-observability-policy"
    Environment = var.environment
  }
}

# IAM Role for CloudWatch Observability (IRSA)
resource "aws_iam_role" "cloudwatch_observability" {
  name = "${var.project_name}-cloudwatch-observability-role"

  assume_role_policy = jsonencode({
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Condition = {
        StringEquals = {
          "${replace(var.cluster_oidc_issuer_url, "https://", "")}:sub" : "system:serviceaccount:amazon-cloudwatch:cloudwatch-agent"
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
    Name        = "${var.project_name}-cloudwatch-observability-role"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "cloudwatch_observability" {
  policy_arn = aws_iam_policy.cloudwatch_observability.arn
  role       = aws_iam_role.cloudwatch_observability.name
}

# Also attach to node group role for compatibility
resource "aws_iam_role_policy_attachment" "cloudwatch_observability_nodes" {
  policy_arn = aws_iam_policy.cloudwatch_observability.arn
  role       = var.eks_nodes_role_name
}

# CloudWatch Observability EKS Add-on
resource "aws_eks_addon" "cloudwatch_observability" {
  cluster_name                = var.cluster_name
  addon_name                  = "amazon-cloudwatch-observability"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  service_account_role_arn    = aws_iam_role.cloudwatch_observability.arn

  configuration_values = jsonencode({
    # Container logs configuration (Fluent Bit)
    containerLogs = {
      enabled = true
    }

    # CloudWatch Agent configuration
    agent = {
      config = {
        logs = {
          metrics_collected = {
            application_signals = {}
            kubernetes = {
              enhanced_container_insights = true
              accelerated_compute_metrics = false
            }
          }
        }
      }
    }
  })

  tags = {
    Name        = "${var.project_name}-cloudwatch-observability"
    Environment = var.environment
    Component   = "logging"
  }

  depends_on = [
    aws_iam_role_policy_attachment.cloudwatch_observability,
    aws_iam_role_policy_attachment.cloudwatch_observability_nodes,
    aws_cloudwatch_log_group.application_logs,
    aws_cloudwatch_log_group.dataplane_logs,
    aws_cloudwatch_log_group.host_logs,
    aws_cloudwatch_log_group.performance_logs
  ]
}

# ============================================
# Prometheus
# ============================================

# IAM Role for Prometheus (IRSA)
resource "aws_iam_role" "prometheus" {
  name = "${var.project_name}-prometheus-role"

  assume_role_policy = jsonencode({
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Condition = {
        StringEquals = {
          "${replace(var.cluster_oidc_issuer_url, "https://", "")}:sub" : "system:serviceaccount:monitoring:monitoring-kube-prometheus-prometheus"
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
    Name        = "${var.project_name}-prometheus-role"
    Environment = var.environment
  }
}

# IAM Policy for Prometheus (EC2 discovery)
resource "aws_iam_policy" "prometheus" {
  name        = "${var.project_name}-prometheus-policy"
  description = "IAM policy for Prometheus to discover EC2 instances"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceTypes",
          "ec2:DescribeRegions",
          "ec2:DescribeAvailabilityZones",
          "ec2:DescribeTags"
        ]
        Resource = "*"
      }
    ]
  })

  tags = {
    Name        = "${var.project_name}-prometheus-policy"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "prometheus" {
  policy_arn = aws_iam_policy.prometheus.arn
  role       = aws_iam_role.prometheus.name
}

# ============================================
# SNS & AlertManager Integration
# ============================================

# SNS Topic for AlertManager notifications
resource "aws_sns_topic" "alertmanager_notifications" {
  name = "${var.project_name}-${var.environment}-alertmanager-notifications"

  tags = {
    Name        = "${var.project_name}-${var.environment}-alertmanager-notifications"
    Environment = var.environment
    Component   = "alerting"
  }
}

# SNS Topic subscription for email notifications
resource "aws_sns_topic_subscription" "alertmanager_email" {
  topic_arn = aws_sns_topic.alertmanager_notifications.arn
  protocol  = "email"
  endpoint  = var.alert_email_address
}

# IAM Role for AlertManager to publish to SNS
resource "aws_iam_role" "alertmanager_sns" {
  name = "${var.project_name}-alertmanager-sns-role"

  assume_role_policy = jsonencode({
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Condition = {
        StringEquals = {
          "${replace(var.cluster_oidc_issuer_url, "https://", "")}:sub" : "system:serviceaccount:monitoring:monitoring-kube-prometheus-alertmanager"
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
    Name        = "${var.project_name}-alertmanager-sns-role"
    Environment = var.environment
  }
}

# IAM Policy for AlertManager SNS publishing
resource "aws_iam_policy" "alertmanager_sns" {
  name        = "${var.project_name}-alertmanager-sns-policy"
  description = "IAM policy for AlertManager to publish to SNS"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sns:Publish"
        ]
        Resource = aws_sns_topic.alertmanager_notifications.arn
      }
    ]
  })

  tags = {
    Name        = "${var.project_name}-alertmanager-sns-policy"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "alertmanager_sns" {
  policy_arn = aws_iam_policy.alertmanager_sns.arn
  role       = aws_iam_role.alertmanager_sns.name
}

# Kubernetes Secret for AlertManager SNS configuration
resource "kubernetes_secret" "alertmanager_sns_config" {
  metadata {
    name      = "alertmanager-sns-config"
    namespace = kubernetes_namespace.monitoring.metadata[0].name
  }

  data = {
    sns_topic_arn = aws_sns_topic.alertmanager_notifications.arn
    aws_region    = var.aws_region
  }

  depends_on = [
    kubernetes_namespace.monitoring,
    aws_sns_topic.alertmanager_notifications
  ]
}

# Add this to the END of your terraform/modules/observability/main.tf

# ============================================
# kube-prometheus-stack Helm Release (Clean Version)
# ============================================

# Generate a secure password for Grafana admin
resource "random_password" "grafana_admin_password" {
  length  = 16
  special = true
}

# Store Grafana admin password in Kubernetes secret
resource "kubernetes_secret" "grafana_admin_credentials" {
  metadata {
    name      = "grafana-admin-credentials"
    namespace = kubernetes_namespace.monitoring.metadata[0].name
  }

  data = {
    admin-user     = "admin"
    admin-password = random_password.grafana_admin_password.result
  }

  type = "Opaque"

  depends_on = [kubernetes_namespace.monitoring]
}

# Deploy kube-prometheus-stack using external values file
resource "helm_release" "kube_prometheus_stack" {
  name       = "monitoring"
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  namespace  = kubernetes_namespace.monitoring.metadata[0].name
  version    = "~> 55.0"

  wait            = true
  timeout         = 900
  atomic          = true
  cleanup_on_fail = true

  # Use the external values file with template variables
  values = [
    templatefile("${path.module}/prometheus-values.yaml", {
      prometheus_role_arn     = aws_iam_role.prometheus.arn
      alertmanager_role_arn   = aws_iam_role.alertmanager_sns.arn
      grafana_secret_name     = kubernetes_secret.grafana_admin_credentials.metadata[0].name
      sns_topic_arn          = aws_sns_topic.alertmanager_notifications.arn
      project_name           = var.project_name
      environment            = var.environment
      environment_upper      = upper(var.environment)
      cluster_name           = var.cluster_name
    })
  ]

  depends_on = [
    kubernetes_namespace.monitoring,
    kubernetes_secret.grafana_admin_credentials,
    aws_iam_role.prometheus,
    aws_iam_role.alertmanager_sns,
    aws_sns_topic.alertmanager_notifications
  ]
}

# Wait for monitoring stack to be ready
resource "time_sleep" "wait_for_monitoring_stack" {
  depends_on      = [helm_release.kube_prometheus_stack]
  create_duration = "120s"
}

# ============================================
# Shared Monitoring Ingress (ALB with path-based routing)
# ============================================

resource "kubernetes_ingress_v1" "monitoring_ingress" {
  metadata {
    name      = "monitoring-ingress"
    namespace = kubernetes_namespace.monitoring.metadata[0].name
    
    annotations = {
      "kubernetes.io/ingress.class"                    = "alb"
      "alb.ingress.kubernetes.io/scheme"               = "internet-facing"
      "alb.ingress.kubernetes.io/target-type"          = "ip"
      "alb.ingress.kubernetes.io/load-balancer-name"   = "boutique-dev-monitoring-alb"
      "alb.ingress.kubernetes.io/listen-ports"         = "[{\"HTTP\": 80}]"
      "alb.ingress.kubernetes.io/success-codes"        = "200,301,302"
# Remove the specific health check path to let ALB use defaults for each service
      "alb.ingress.kubernetes.io/group.name"           = "${var.project_name}-${var.environment}-monitoring"
      
      # For HTTPS (uncomment when you have certificates)
      # "alb.ingress.kubernetes.io/listen-ports"       = "[{\"HTTPS\": 443}]"
      # "alb.ingress.kubernetes.io/certificate-arn"    = "your-certificate-arn"
      # "alb.ingress.kubernetes.io/ssl-redirect"       = "443"
    }
  }

  spec {
    ingress_class_name = "alb"
    
    rule {
      #host = "monitoring-${var.environment}.${var.project_name}.local"
      
      http {
        # Grafana path
        path {
          path      = "/grafana"
          path_type = "Prefix"
          
          backend {
            service {
              name = "monitoring-grafana"
              port {
                number = 80
              }
            }
          }
        }
        
        # Grafana path with trailing slash
        path {
          path      = "/grafana/"
          path_type = "Prefix"
          
          backend {
            service {
              name = "monitoring-grafana"
              port {
                number = 80
              }
            }
          }
        }
        
        # Prometheus path
        path {
          path      = "/prometheus"
          path_type = "Prefix"
          
          backend {
            service {
              name = "monitoring-kube-prometheus-prometheus"
              port {
                number = 9090
              }
            }
          }
        }
        
        # Prometheus path with trailing slash
        path {
          path      = "/prometheus/"
          path_type = "Prefix"
          
          backend {
            service {
              name = "monitoring-kube-prometheus-prometheus"
              port {
                number = 9090
              }
            }
          }
        }
        
        # AlertManager path (optional)
        path {
          path      = "/alertmanager"
          path_type = "Prefix"
          
          backend {
            service {
              name = "monitoring-kube-prometheus-alertmanager"
              port {
                number = 9093
              }
            }
          }
        }
        
        # Root path redirects to Grafana
        path {
          path      = "/"
          path_type = "Prefix"
          
          backend {
            service {
              name = "monitoring-grafana"
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
    helm_release.kube_prometheus_stack,
    time_sleep.wait_for_monitoring_stack
  ]
}