# modules/networking/outputs.tf
# Outputs from the Networking Module

# VPC Information
output "vpc_id" {
  description = "ID of the VPC"
  value       = aws_vpc.main.id
}

output "vpc_cidr_block" {
  description = "CIDR block of the VPC"
  value       = aws_vpc.main.cidr_block
}

# Subnet Information
output "public_subnet_ids" {
  description = "IDs of the public subnets"
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "IDs of the private subnets"
  value       = aws_subnet.private[*].id
}

output "availability_zones" {
  description = "List of availability zones used"
  value       = data.aws_availability_zones.available.names
}

# Security Group IDs
output "eks_cluster_security_group_id" {
  description = "Security group ID for EKS cluster"
  value       = aws_security_group.eks_cluster.id
}

output "eks_nodes_security_group_id" {
  description = "Security group ID for EKS worker nodes"
  value       = aws_security_group.eks_nodes.id
}

output "bastion_security_group_id" {
  description = "Security group ID for bastion host"
  value       = aws_security_group.bastion.id
}

output "rds_security_group_id" {
  description = "Security group ID for RDS database"
  value       = aws_security_group.rds.id
}

output "efs_security_group_id" {
  description = "Security group ID for EFS file system"
  value       = aws_security_group.efs.id
}

output "redis_security_group_id" {
  description = "Security group ID for Redis cluster"
  value       = aws_security_group.redis.id
}

# Gateway Information
output "internet_gateway_id" {
  description = "ID of the Internet Gateway"
  value       = aws_internet_gateway.main.id
}

output "nat_gateway_ids" {
  description = "IDs of the NAT Gateways"
  value       = aws_nat_gateway.main[*].id
}

# Additional outputs that other modules might need
output "public_route_table_id" {
  description = "ID of the public route table"
  value       = aws_route_table.public.id
}

output "private_route_table_ids" {
  description = "IDs of the private route tables"
  value       = aws_route_table.private[*].id
}