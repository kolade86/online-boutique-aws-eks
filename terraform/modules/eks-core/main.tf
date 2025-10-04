# modules/eks-core/main.tf
# EKS Core Module - Cluster, Nodes, Add-ons, and OIDC

# Data sources
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "tls_certificate" "eks" {
  url = aws_eks_cluster.main.identity[0].oidc[0].issuer
}

# No AMI data source needed - EKS automatically selects the appropriate AMI

locals {
  cluster_name = "${var.project_name}-${var.environment}-cluster"
}

# KMS Key for EKS encryption
resource "aws_kms_key" "eks" {
  description             = "EKS Secret Encryption Key"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  tags = {
    Name        = "${var.project_name}-${var.environment}-kms-key"
    Environment = var.environment
  }
}

resource "aws_kms_alias" "eks" {
  name          = "alias/${var.project_name}-${var.environment}-kms-key"
  target_key_id = aws_kms_key.eks.key_id
}

# IAM Role for EKS Cluster
resource "aws_iam_role" "eks_cluster" {
  name = "${var.project_name}-${var.environment}-cluster-role"

  assume_role_policy = jsonencode({
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "eks.amazonaws.com"
      }
    }]
    Version = "2012-10-17"
  })

  tags = {
    Name        = "${var.project_name}-${var.environment}-cluster-role"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "eks_cluster_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
  role       = aws_iam_role.eks_cluster.name
}

# IAM Role for EKS Node Group
resource "aws_iam_role" "eks_nodes" {
  name = "${var.project_name}-${var.environment}-nodes-role"

  assume_role_policy = jsonencode({
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
    }]
    Version = "2012-10-17"
  })

  tags = {
    Name        = "${var.project_name}-${var.environment}-nodes-role"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "eks_worker_node_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
  role       = aws_iam_role.eks_nodes.name
}

resource "aws_iam_role_policy_attachment" "eks_cni_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
  role       = aws_iam_role.eks_nodes.name
}

resource "aws_iam_role_policy_attachment" "eks_registry_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
  role       = aws_iam_role.eks_nodes.name
}

# SSM policy for Session Manager access to all nodes
resource "aws_iam_role_policy_attachment" "eks_ssm_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
  role       = aws_iam_role.eks_nodes.name
}

# IAM Instance Profile for EKS Nodes
resource "aws_iam_instance_profile" "eks_nodes" {
  name = "${var.project_name}-${var.environment}-nodes-instance-profile"
  role = aws_iam_role.eks_nodes.name

  tags = {
    Name        = "${var.project_name}-${var.environment}-nodes-instance-profile"
    Environment = var.environment
  }
}

# IAM Role for EFS CSI Driver
resource "aws_iam_role" "efs_csi_driver" {
  name = "${var.project_name}-${var.environment}-efs-csi-driver-role"

  assume_role_policy = jsonencode({
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Condition = {
        StringEquals = {
          "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub" : "system:serviceaccount:kube-system:efs-csi-controller-sa"
          "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:aud" : "sts.amazonaws.com"
        }
      }
      Principal = {
        Federated = aws_iam_openid_connect_provider.eks.arn
      }
    }]
    Version = "2012-10-17"
  })

  tags = {
    Name        = "${var.project_name}-${var.environment}-efs-csi-driver-role"
    Environment = var.environment
  }
}

# Attach EFS CSI Driver policy
resource "aws_iam_role_policy_attachment" "efs_csi_driver" {
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy"
  role       = aws_iam_role.efs_csi_driver.name
}

# IAM Role for EBS CSI Driver
resource "aws_iam_role" "ebs_csi_driver" {
  name = "${var.project_name}-${var.environment}-ebs-csi-driver-role"

  assume_role_policy = jsonencode({
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Condition = {
        StringEquals = {
          "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub" : "system:serviceaccount:kube-system:ebs-csi-controller-sa"
          "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:aud" : "sts.amazonaws.com"
        }
      }
      Principal = {
        Federated = aws_iam_openid_connect_provider.eks.arn
      }
    }]
    Version = "2012-10-17"
  })
}

resource "aws_iam_role_policy_attachment" "ebs_csi_driver" {
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
  role       = aws_iam_role.ebs_csi_driver.name
}

# CloudWatch Log Group for EKS
resource "aws_cloudwatch_log_group" "eks_cluster" {
  name              = "/aws/eks/${local.cluster_name}/cluster"
  retention_in_days = 7

  tags = {
    Name        = "${var.project_name}-${var.environment}-logs"
    Environment = var.environment
  }
}

# EKS Cluster
resource "aws_eks_cluster" "main" {
  name     = local.cluster_name
  role_arn = aws_iam_role.eks_cluster.arn
  version  = var.cluster_version

  vpc_config {
    subnet_ids              = concat(var.private_subnet_ids, var.public_subnet_ids)
    endpoint_private_access = var.endpoint_private_access
    endpoint_public_access  = var.endpoint_public_access
    public_access_cidrs     = var.public_access_cidrs
    security_group_ids      = [var.eks_cluster_security_group_id]
  }

  encryption_config {
    provider {
      key_arn = aws_kms_key.eks.arn
    }
    resources = ["secrets"]
  }

  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  depends_on = [
    aws_iam_role_policy_attachment.eks_cluster_policy,
    aws_cloudwatch_log_group.eks_cluster,
    aws_kms_key.eks
  ]

  tags = {
    Name        = local.cluster_name
    Environment = var.environment
  }
}

# OIDC Identity Provider for EKS
resource "aws_iam_openid_connect_provider" "eks" {
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks.certificates[0].sha1_fingerprint]
  url             = aws_eks_cluster.main.identity[0].oidc[0].issuer

  depends_on = [aws_eks_cluster.main]

  tags = {
    Name        = "${var.project_name}-${var.environment}-eks-irsa"
    Environment = var.environment
  }
}

# Launch Template for EKS Worker Nodes
resource "aws_launch_template" "eks_nodes" {
  name_prefix   = "${var.project_name}-${var.environment}-node-"
  description   = "Launch template for EKS worker nodes"
  #instance_type = var.instance_types[0]

  vpc_security_group_ids = [var.eks_nodes_security_group_id]

  # IAM Instance Profile
  # iam_instance_profile {
  #   name = aws_iam_instance_profile.eks_nodes.name
  # }

  # Block device mapping for root volume
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 20
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  # Metadata options for security
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    instance_metadata_tags      = "enabled"
  }

  # Monitoring
  monitoring {
    enabled = true
  }

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name        = "${var.project_name}-${var.environment}-worker-node"
      Environment = var.environment
      NodeGroup   = "${var.project_name}-${var.environment}-nodes"
      Cluster     = local.cluster_name
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags = {
      Name        = "${var.project_name}-${var.environment}-worker-node-volume"
      Environment = var.environment
    }
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-node-template"
    Environment = var.environment
  }

  # Ensure cluster is created first
  depends_on = [
    aws_eks_cluster.main,
    aws_iam_instance_profile.eks_nodes
  ]
}

# EKS Node Group
resource "aws_eks_node_group" "main" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "${var.project_name}-${var.environment}-nodes"
  node_role_arn   = aws_iam_role.eks_nodes.arn
  subnet_ids      = var.node_subnet_ids
  version         = var.cluster_version

  capacity_type  = var.capacity_type
  instance_types = var.instance_types

  scaling_config {
    desired_size = var.desired_size
    max_size     = var.max_size
    min_size     = var.min_size
  }

  update_config {
    max_unavailable_percentage = var.node_update_strategy == "rolling" ? 25 : 50
  }

  lifecycle {
    create_before_destroy = true
  }

  # Launch template for custom node naming and configuration
  # launch_template {
  #   name    = aws_launch_template.eks_nodes.name
  #   version = aws_launch_template.eks_nodes.latest_version
  # }

  depends_on = [
    aws_eks_cluster.main,
    aws_iam_role_policy_attachment.eks_worker_node_policy,
    aws_iam_role_policy_attachment.eks_cni_policy,
    aws_iam_role_policy_attachment.eks_registry_policy,
    aws_iam_role_policy_attachment.eks_ssm_policy,
    #aws_iam_instance_profile.eks_nodes,
    #aws_launch_template.eks_nodes,
  ]

  timeouts {
    create = "20m"
    update = "30m"
    delete = "20m"
  }

  tags = {
    Name                                               = "${var.project_name}-${var.environment}-nodes"
    Environment                                        = var.environment
    "k8s.io/cluster-autoscaler/enabled"               = "true"
    "k8s.io/cluster-autoscaler/${local.cluster_name}" = "owned"
  }
}

# EKS Add-ons (versions will be auto-selected based on cluster version)
resource "aws_eks_addon" "coredns" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "coredns"
  resolve_conflicts_on_create = "OVERWRITE"

  depends_on = [aws_eks_node_group.main]

  timeouts { 
    create = "20m" 
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-coredns"
    Environment = var.environment
  }
}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "kube-proxy"
  resolve_conflicts_on_create = "OVERWRITE"

  depends_on = [aws_eks_node_group.main]

  timeouts { 
    create = "20m" 
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-kube-proxy"
    Environment = var.environment
  }
}

resource "aws_eks_addon" "vpc_cni" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "vpc-cni"
  resolve_conflicts_on_create = "OVERWRITE"

  depends_on = [aws_eks_node_group.main]

  timeouts { 
    create = "20m" 
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-vpc-cni"
    Environment = var.environment
  }
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "aws-ebs-csi-driver"
  resolve_conflicts_on_create = "OVERWRITE"
  service_account_role_arn    = aws_iam_role.ebs_csi_driver.arn  # Add this line

  depends_on = [
    aws_iam_role_policy_attachment.ebs_csi_driver,  # Add this
    aws_eks_node_group.main
  ]
  #depends_on = [aws_eks_node_group.main]

  timeouts { 
    create = "20m" 
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-ebs-csi"
    Environment = var.environment
  }
}

# EFS CSI Driver Add-on
resource "aws_eks_addon" "efs_csi" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "aws-efs-csi-driver"
  resolve_conflicts_on_create = "OVERWRITE"
  service_account_role_arn    = aws_iam_role.efs_csi_driver.arn

  depends_on = [
    aws_iam_role_policy_attachment.efs_csi_driver,
    aws_eks_node_group.main
  ]

  timeouts { 
    create = "20m" 
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-efs-csi"
    Environment = var.environment
  }
}

# Kubernetes AWS Auth ConfigMap
resource "kubernetes_config_map_v1_data" "aws_auth" {
  metadata {
    name      = "aws-auth"
    namespace = "kube-system"
  }

  data = {
    mapRoles = yamlencode([
      {
        rolearn  = aws_iam_role.eks_nodes.arn
        username = "system:node:{{EC2PrivateDNSName}}"
        groups   = ["system:bootstrappers", "system:nodes"]
      },
      {
        rolearn  = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/online-boutique-github-actions-role"
        username = "github-actions"
        groups   = ["system:masters"]
      }
    ])
  }

  force = true

  depends_on = [
    aws_eks_cluster.main,
    aws_eks_node_group.main
  ]
}


# Add EKS cluster-managed security group to Redis security group (if Redis SG ID is provided)
resource "aws_security_group_rule" "redis_from_eks_managed" {
  count = var.enable_redis_sg_rule ? 1 : 0

  type                     = "ingress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  source_security_group_id = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
  security_group_id        = var.redis_security_group_id
  description              = "Redis access from EKS cluster-managed security group"

  # Optional: fail fast if enabled but no ID passed
  lifecycle {
    precondition {
      condition     = var.redis_security_group_id != null && var.redis_security_group_id != ""
      error_message = "Set redis_security_group_id when enable_redis_sg_rule=true."
    }
  }
}
