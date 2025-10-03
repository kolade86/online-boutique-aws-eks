# modules/cicd/main.tf
# CICD Module - ECR and GitHub Actions Integration

# ============================================
# ECR Container Registry
# ============================================

# ECR Repository
# Replace single repository with multiple repositories
locals {
  microservices = [
    "frontend",
    "cartservice",
    "productcatalogservice",
    "currencyservice",
    "paymentservice",
    "shippingservice",
    "emailservice",
    "checkoutservice",
    "recommendationservice",
    "adservice",
    "loadgenerator"
  ]
}

resource "aws_ecr_repository" "microservices" {
  for_each = toset(local.microservices)
  
  name                 = "${var.project_name}-${var.environment}-${each.value}"
  image_tag_mutability = "MUTABLE"
  
  image_scanning_configuration {
    scan_on_push = true
  }
  
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.eks_kms_key_arn
  }
  
  tags = {
    Name        = "${var.project_name}-${var.environment}-${each.value}"
    Environment = var.environment
    Service     = each.value
  }
}

# Lifecycle policy for each repository
resource "aws_ecr_lifecycle_policy" "microservices" {
  for_each   = aws_ecr_repository.microservices
  repository = each.value.name
  
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 10 images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

# ============================================
# GitHub OIDC Integration
# ============================================

# GitHub OIDC Identity Provider
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1", "1c58a3a8518e8759bf075b76b750d4f2df264fcd"]

  tags = {
    Name        = "${var.project_name}-github-oidc"
    Environment = var.environment
  }
}

# IAM Role for GitHub Actions
resource "aws_iam_role" "github_actions" {
  name = "${var.project_name}-github-actions-role"

  assume_role_policy = jsonencode({
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:*"
        }
      }
      Principal = {
        Federated = aws_iam_openid_connect_provider.github.arn
      }
    }]
    Version = "2012-10-17"
  })

  tags = {
    Name        = "${var.project_name}-github-actions-role"
    Environment = var.environment
  }
}

# GitHub Actions EKS permissions
resource "aws_iam_role_policy" "github_actions_eks" {
  name = "${var.project_name}-github-actions-eks-policy"
  role = aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "eks:DescribeCluster",
          "eks:ListClusters",
          "eks:AccessKubernetesApi",
          "eks:DescribeNodegroup",
          "eks:ListNodegroups"
        ]
        Resource = [
          "arn:aws:eks:${var.aws_region}:${var.aws_account_id}:cluster/${var.cluster_name}",
          "arn:aws:eks:${var.aws_region}:${var.aws_account_id}:nodegroup/${var.cluster_name}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "eks:ListClusters"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "sts:GetCallerIdentity"
        ]
        Resource = "*"
      }
    ]
  })
}


# GitHub Actions ECR permissions - CORRECTED
resource "aws_iam_role_policy" "github_actions_ecr" {
  name = "${var.project_name}-github-actions-ecr-policy"
  role = aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
          "ecr:DescribeRepositories",
          "ecr:CreateRepository",
          "ecr:ListImages",
          "ecr:DescribeImages"
        ]
        Resource = [
          for repo in aws_ecr_repository.microservices : repo.arn
        ]
      }
    ]
  })
}

# GitHub Actions IAM read permissions (for Prometheus role verification)
resource "aws_iam_role_policy" "github_actions_iam_read" {
  name = "${var.project_name}-github-actions-iam-read"
  role = aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "iam:GetRole",
          "iam:ListRoles"
        ]
        Resource = [
          "arn:aws:iam::${var.aws_account_id}:role/${var.project_name}-*"
        ]
      }
    ]
  })
}

# SNS permissions for GitHub Actions
resource "aws_iam_role_policy" "github_actions_sns" {
  name = "${var.project_name}-${var.environment}-github-actions-sns-policy"
  role = aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sns:ListTopics",
          "sns:Publish"
        ]
        Resource = [
          var.sns_topic_arn,
          "arn:aws:sns:${var.aws_region}:${var.aws_account_id}:${var.project_name}-*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "sns:ListTopics"
        ]
        Resource = "*"
      }
    ]
  })
}

# GitHub Actions for ElastiCache permissions (NEW - ADD THIS)
resource "aws_iam_role_policy" "github_actions_elasticache" {
  name = "${var.project_name}-github-actions-elasticache-policy"
  role = aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "elasticache:DescribeReplicationGroups",
          "elasticache:DescribeCacheClusters"
        ]
        Resource = "*"
      }
    ]
  })
}