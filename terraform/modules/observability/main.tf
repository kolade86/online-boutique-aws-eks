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

    # Disable auto-injection of OTel instrumentation into pods.
    # Prevents the mutating webhook from adding instrumentation annotations.
    # Services that need tracing can opt in via the CloudWatch console.
    manager = {
      applicationSignals = {
        autoMonitor = {
          monitorAllServices = false
        }
      }
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
    templatefile("${path.module}/prometheus-values.yaml.tftpl", {
      prometheus_role_arn   = aws_iam_role.prometheus.arn
      alertmanager_role_arn = aws_iam_role.alertmanager_sns.arn
      grafana_secret_name   = kubernetes_secret.grafana_admin_credentials.metadata[0].name
      sns_topic_arn         = aws_sns_topic.alertmanager_notifications.arn
      project_name          = var.project_name
      environment           = var.environment
      environment_upper     = upper(var.environment)
      cluster_name          = var.cluster_name
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

# ============================================
# Monitoring Access — private, no ingress
# ============================================
# The monitoring stack is deliberately NOT exposed through an ingress/ALB.
# Grafana, Prometheus, and Alertmanager are ClusterIP services reachable only
# from inside the cluster; reach them locally with kubectl port-forward
# (see the port_forward_commands output). Both subcharts keep
# `ingress.enabled: false` in prometheus-values.yaml.tftpl.

# ============================================
# CloudWatch Alarms — AWS Managed Services
# ============================================
# Monitors RDS, ElastiCache Redis, and ALB metrics
# that Prometheus cannot see (outside the cluster).

# --- RDS Alarms ---

resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  count               = var.rds_instance_identifier != "" ? 1 : 0
  alarm_name          = "${var.project_name}-${var.environment}-rds-high-cpu"
  alarm_description   = "RDS CPU utilization above 80% for 5 minutes"
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  dimensions = {
    DBInstanceIdentifier = var.rds_instance_identifier
  }
  alarm_actions = [aws_sns_topic.alertmanager_notifications.arn]
  ok_actions    = [aws_sns_topic.alertmanager_notifications.arn]

  tags = {
    Name        = "${var.project_name}-${var.environment}-rds-cpu-alarm"
    Environment = var.environment
    Component   = "alerting"
  }
}

resource "aws_cloudwatch_metric_alarm" "rds_free_storage" {
  count               = var.rds_instance_identifier != "" ? 1 : 0
  alarm_name          = "${var.project_name}-${var.environment}-rds-low-storage"
  alarm_description   = "RDS free storage below 2GB"
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 2000000000 # 2GB in bytes
  comparison_operator = "LessThanThreshold"
  dimensions = {
    DBInstanceIdentifier = var.rds_instance_identifier
  }
  alarm_actions = [aws_sns_topic.alertmanager_notifications.arn]
  ok_actions    = [aws_sns_topic.alertmanager_notifications.arn]

  tags = {
    Name        = "${var.project_name}-${var.environment}-rds-storage-alarm"
    Environment = var.environment
    Component   = "alerting"
  }
}

resource "aws_cloudwatch_metric_alarm" "rds_connections" {
  count               = var.rds_instance_identifier != "" ? 1 : 0
  alarm_name          = "${var.project_name}-${var.environment}-rds-high-connections"
  alarm_description   = "RDS database connections above 80"
  namespace           = "AWS/RDS"
  metric_name         = "DatabaseConnections"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  dimensions = {
    DBInstanceIdentifier = var.rds_instance_identifier
  }
  alarm_actions = [aws_sns_topic.alertmanager_notifications.arn]
  ok_actions    = [aws_sns_topic.alertmanager_notifications.arn]

  tags = {
    Name        = "${var.project_name}-${var.environment}-rds-connections-alarm"
    Environment = var.environment
    Component   = "alerting"
  }
}

resource "aws_cloudwatch_metric_alarm" "rds_free_memory" {
  count               = var.rds_instance_identifier != "" ? 1 : 0
  alarm_name          = "${var.project_name}-${var.environment}-rds-low-memory"
  alarm_description   = "RDS freeable memory below 128MB"
  namespace           = "AWS/RDS"
  metric_name         = "FreeableMemory"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 134217728 # 128MB in bytes
  comparison_operator = "LessThanThreshold"
  dimensions = {
    DBInstanceIdentifier = var.rds_instance_identifier
  }
  alarm_actions = [aws_sns_topic.alertmanager_notifications.arn]
  ok_actions    = [aws_sns_topic.alertmanager_notifications.arn]

  tags = {
    Name        = "${var.project_name}-${var.environment}-rds-memory-alarm"
    Environment = var.environment
    Component   = "alerting"
  }
}

# --- ElastiCache Redis Alarms ---

resource "aws_cloudwatch_metric_alarm" "redis_cpu" {
  count               = var.redis_replication_group_id != "" ? 1 : 0
  alarm_name          = "${var.project_name}-${var.environment}-redis-high-cpu"
  alarm_description   = "Redis CPU utilization above 70% for 5 minutes"
  namespace           = "AWS/ElastiCache"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 70
  comparison_operator = "GreaterThanThreshold"
  dimensions = {
    ReplicationGroupId = var.redis_replication_group_id
  }
  alarm_actions = [aws_sns_topic.alertmanager_notifications.arn]
  ok_actions    = [aws_sns_topic.alertmanager_notifications.arn]

  tags = {
    Name        = "${var.project_name}-${var.environment}-redis-cpu-alarm"
    Environment = var.environment
    Component   = "alerting"
  }
}

resource "aws_cloudwatch_metric_alarm" "redis_memory" {
  count               = var.redis_replication_group_id != "" ? 1 : 0
  alarm_name          = "${var.project_name}-${var.environment}-redis-high-memory"
  alarm_description   = "Redis memory usage above 80%"
  namespace           = "AWS/ElastiCache"
  metric_name         = "DatabaseMemoryUsagePercentage"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  dimensions = {
    ReplicationGroupId = var.redis_replication_group_id
  }
  alarm_actions = [aws_sns_topic.alertmanager_notifications.arn]
  ok_actions    = [aws_sns_topic.alertmanager_notifications.arn]

  tags = {
    Name        = "${var.project_name}-${var.environment}-redis-memory-alarm"
    Environment = var.environment
    Component   = "alerting"
  }
}

resource "aws_cloudwatch_metric_alarm" "redis_evictions" {
  count               = var.redis_replication_group_id != "" ? 1 : 0
  alarm_name          = "${var.project_name}-${var.environment}-redis-evictions"
  alarm_description   = "Redis is evicting keys — memory pressure detected"
  namespace           = "AWS/ElastiCache"
  metric_name         = "Evictions"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 100
  comparison_operator = "GreaterThanThreshold"
  dimensions = {
    ReplicationGroupId = var.redis_replication_group_id
  }
  alarm_actions = [aws_sns_topic.alertmanager_notifications.arn]
  ok_actions    = [aws_sns_topic.alertmanager_notifications.arn]

  tags = {
    Name        = "${var.project_name}-${var.environment}-redis-evictions-alarm"
    Environment = var.environment
    Component   = "alerting"
  }
}

# --- ALB Alarms ---

resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  count               = var.alb_arn_suffix != "" ? 1 : 0
  alarm_name          = "${var.project_name}-${var.environment}-alb-5xx-errors"
  alarm_description   = "ALB returning more than 10 HTTP 5xx errors in 5 minutes"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_ELB_5XX_Count"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }
  alarm_actions = [aws_sns_topic.alertmanager_notifications.arn]
  ok_actions    = [aws_sns_topic.alertmanager_notifications.arn]

  tags = {
    Name        = "${var.project_name}-${var.environment}-alb-5xx-alarm"
    Environment = var.environment
    Component   = "alerting"
  }
}

resource "aws_cloudwatch_metric_alarm" "alb_target_5xx" {
  count               = var.alb_arn_suffix != "" ? 1 : 0
  alarm_name          = "${var.project_name}-${var.environment}-alb-target-5xx-errors"
  alarm_description   = "ALB targets returning more than 10 HTTP 5xx errors in 5 minutes"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }
  alarm_actions = [aws_sns_topic.alertmanager_notifications.arn]
  ok_actions    = [aws_sns_topic.alertmanager_notifications.arn]

  tags = {
    Name        = "${var.project_name}-${var.environment}-alb-target-5xx-alarm"
    Environment = var.environment
    Component   = "alerting"
  }
}

resource "aws_cloudwatch_metric_alarm" "alb_unhealthy_targets" {
  count               = var.alb_arn_suffix != "" ? 1 : 0
  alarm_name          = "${var.project_name}-${var.environment}-alb-unhealthy-targets"
  alarm_description   = "ALB has unhealthy targets for more than 5 minutes"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }
  alarm_actions = [aws_sns_topic.alertmanager_notifications.arn]
  ok_actions    = [aws_sns_topic.alertmanager_notifications.arn]

  tags = {
    Name        = "${var.project_name}-${var.environment}-alb-unhealthy-alarm"
    Environment = var.environment
    Component   = "alerting"
  }
}

