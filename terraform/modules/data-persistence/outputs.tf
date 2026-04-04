# modules/data-persistence/outputs.tf
# Outputs from the Data-Persistence Module

# RDS Database Outputs
output "rds_endpoint" {
  description = "RDS instance endpoint"
  value       = aws_db_instance.postgres.endpoint
}

output "rds_address" {
  description = "RDS instance address (hostname only, no port)"
  value       = aws_db_instance.postgres.address
}

output "rds_port" {
  description = "RDS instance port"
  value       = aws_db_instance.postgres.port
}

output "rds_database_name" {
  description = "RDS database name"
  value       = aws_db_instance.postgres.db_name
}

output "rds_username" {
  description = "RDS database username"
  value       = aws_db_instance.postgres.username
  sensitive   = false
}

output "rds_arn" {
  description = "ARN of the RDS instance"
  value       = aws_db_instance.postgres.arn
}

# Secrets Manager Outputs
output "secrets_manager_secret_name" {
  description = "AWS Secrets Manager secret name for database credentials"
  value       = aws_secretsmanager_secret.db_password.name
}

output "secrets_manager_secret_arn" {
  description = "AWS Secrets Manager secret ARN for database credentials"
  value       = aws_secretsmanager_secret.db_password.arn
}

# EFS Outputs
output "efs_file_system_id" {
  description = "ID of the EFS file system"
  value       = aws_efs_file_system.shared_storage.id
}

output "efs_dns_name" {
  description = "DNS name of the EFS file system"
  value       = aws_efs_file_system.shared_storage.dns_name
}

output "efs_arn" {
  description = "ARN of the EFS file system"
  value       = aws_efs_file_system.shared_storage.arn
}

# KMS Keys Outputs
output "rds_kms_key_arn" {
  description = "ARN of the RDS KMS encryption key"
  value       = aws_kms_key.rds.arn
}

output "rds_kms_key_id" {
  description = "ID of the RDS KMS encryption key"
  value       = aws_kms_key.rds.key_id
}

output "ebs_kms_key_arn" {
  description = "ARN of the EBS KMS encryption key"
  value       = aws_kms_key.ebs.arn
}

output "ebs_kms_key_id" {
  description = "ID of the EBS KMS encryption key"
  value       = aws_kms_key.ebs.key_id
}

output "efs_kms_key_arn" {
  description = "ARN of the EFS KMS encryption key"
  value       = aws_kms_key.efs.arn
}

output "efs_kms_key_id" {
  description = "ID of the EFS KMS encryption key"
  value       = aws_kms_key.efs.key_id
}

# Redis Outputs
output "redis_endpoint" {
  description = "Redis primary endpoint"
  value       = aws_elasticache_replication_group.redis.primary_endpoint_address
}

output "redis_port" {
  description = "Redis port"
  value       = aws_elasticache_replication_group.redis.port
}

output "redis_connection_string" {
  description = "Redis connection string for cart service"
  value       = "${aws_elasticache_replication_group.redis.primary_endpoint_address}:${aws_elasticache_replication_group.redis.port}"
}

# Identifiers for CloudWatch Alarms
output "rds_instance_identifier" {
  description = "RDS instance identifier"
  value       = aws_db_instance.postgres.identifier
}

output "redis_replication_group_id" {
  description = "ElastiCache Redis replication group ID"
  value       = aws_elasticache_replication_group.redis.replication_group_id
}

# Database Connection Information
output "database_connection_info" {
  description = "Database connection information for applications"
  value = {
    endpoint                   = aws_db_instance.postgres.endpoint
    address                    = aws_db_instance.postgres.address
    port                       = aws_db_instance.postgres.port
    database_name              = aws_db_instance.postgres.db_name
    username                   = aws_db_instance.postgres.username
    secrets_manager_secret     = aws_secretsmanager_secret.db_password.name
    connection_string_template = "postgresql://${aws_db_instance.postgres.username}:[PASSWORD]@${aws_db_instance.postgres.address}:${aws_db_instance.postgres.port}/${aws_db_instance.postgres.db_name}"
  }
}