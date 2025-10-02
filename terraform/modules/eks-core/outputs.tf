# modules/eks-core/outputs.tf
# Outputs from the EKS-Core Module

# EKS Cluster Information
output "cluster_id" {
  description = "EKS cluster ID"
  value       = aws_eks_cluster.main.id
}

output "cluster_arn" {
  description = "EKS cluster ARN"
  value       = aws_eks_cluster.main.arn
}

output "cluster_endpoint" {
  description = "EKS cluster endpoint"
  value       = aws_eks_cluster.main.endpoint
}

output "cluster_version" {
  description = "The Kubernetes server version for the EKS cluster"
  value       = aws_eks_cluster.main.version
}

output "cluster_ca_certificate" {
  description = "Base64 encoded certificate data required to communicate with the cluster"
  value       = aws_eks_cluster.main.certificate_authority[0].data
}

output "cluster_security_group_id" {
  description = "Security group ID attached to the EKS cluster"
  value       = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
}

# OIDC Provider Information
output "cluster_oidc_issuer_url" {
  description = "The URL on the EKS cluster OIDC Issuer"
  value       = aws_eks_cluster.main.identity[0].oidc[0].issuer
}

output "oidc_provider_arn" {
  description = "The ARN of the OIDC Provider"
  value       = aws_iam_openid_connect_provider.eks.arn
}

# Node Group Information
output "node_group_arn" {
  description = "Amazon Resource Name (ARN) of the EKS Node Group"
  value       = aws_eks_node_group.main.arn
}

output "node_group_status" {
  description = "Status of the EKS Node Group"
  value       = aws_eks_node_group.main.status
}

# IAM Role Information
output "eks_cluster_role_arn" {
  description = "ARN of the EKS cluster IAM role"
  value       = aws_iam_role.eks_cluster.arn
}

output "eks_nodes_role_arn" {
  description = "ARN of the EKS node group IAM role"
  value       = aws_iam_role.eks_nodes.arn
}

output "efs_csi_driver_role_arn" {
  description = "ARN of the EFS CSI driver IAM role"
  value       = aws_iam_role.efs_csi_driver.arn
}

# KMS Information
output "kms_key_arn" {
  description = "ARN of the KMS key used for EKS encryption"
  value       = aws_kms_key.eks.arn
}

output "kms_key_id" {
  description = "ID of the KMS key used for EKS encryption"
  value       = aws_kms_key.eks.key_id
}

# Additional useful outputs
output "cluster_primary_security_group_id" {
  description = "The cluster primary security group ID created by EKS"
  value       = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
}

output "node_group_launch_template_id" {
  description = "ID of the launch template used by the node group"
  value       = aws_launch_template.eks_nodes.id
}

output "cluster_name" {
  description = "Name of the EKS cluster"
  value       = local.cluster_name
}

output "eks_nodes_instance_profile_arn" {
  description = "ARN of the EKS nodes instance profile"
  value       = aws_iam_instance_profile.eks_nodes.arn
}

output "eks_nodes_instance_profile_name" {
  description = "Name of the EKS nodes instance profile"
  value       = aws_iam_instance_profile.eks_nodes.name
}

# Launch Template Information (ADD THIS)
output "launch_template_id" {
  description = "ID of the launch template"
  value       = aws_launch_template.eks_nodes.id
}

output "launch_template_latest_version" {
  description = "Latest version of the launch template"
  value       = aws_launch_template.eks_nodes.latest_version
}

output "eks_nodes_role_name" {
  description = "Name of the EKS nodes IAM role"
  value       = aws_iam_role.eks_nodes.name
}
