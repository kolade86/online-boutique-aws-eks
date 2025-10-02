# modules/data-persistence/main.tf
# Data Persistence Module - RDS, Storage Keys, EFS, and Redis (NO Kubernetes resources)

# ============================================
# RDS Database Resources
# ============================================

# RDS Subnet Group
resource "aws_db_subnet_group" "main" {
  name       = "${var.project_name}-${var.environment}-db-subnet-group"
  subnet_ids = var.private_subnet_ids

  tags = {
    Name        = "${var.project_name}-db-subnet-group"
    Environment = var.environment
  }
}

# RDS Parameter Group
resource "aws_db_parameter_group" "postgres" {
  name        = "${var.project_name}-${var.environment}-postgres-params"
  family      = "postgres17"
  description = "Custom parameter group for PostgreSQL 17"

  # Dynamic parameters that can be applied immediately
  parameter {
    name         = "log_statement"
    value        = "all"
    apply_method = "immediate"
  }

  parameter {
    name         = "log_min_duration_statement"
    value        = "1000" # Log queries longer than 1 second
    apply_method = "immediate"
  }

  parameter {
    name         = "max_connections"
    value        = "200"
    apply_method = "pending-reboot"
  }

  # Static parameters that require restart - apply on next reboot
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements"
    apply_method = "pending-reboot"
  }

  tags = {
    Name        = "${var.project_name}-postgres-params"
    Environment = var.environment
  }
}

# Generate random password for RDS
resource "random_password" "db_password" {
  length           = 16
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

# KMS Key for RDS encryption
resource "aws_kms_key" "rds" {
  description             = "RDS encryption key for ${var.project_name}-${var.environment}"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  tags = {
    Name        = "${var.project_name}-rds-kms-key"
    Environment = var.environment
  }
}

resource "aws_kms_alias" "rds" {
  name          = "alias/${var.project_name}-${var.environment}-rds-key"
  target_key_id = aws_kms_key.rds.key_id
}

# Store database password in AWS Secrets Manager
resource "aws_secretsmanager_secret" "db_password" {
  name        = "${var.project_name}-${var.environment}-db-credentials"
  description = "Database credentials for ${var.project_name}-${var.environment}"

  tags = {
    Name        = "${var.project_name}-db-credentials"
    Environment = var.environment
  }
}

resource "aws_secretsmanager_secret_version" "db_password" {
  secret_id = aws_secretsmanager_secret.db_password.id
  secret_string = jsonencode({
    username = var.db_username
    password = random_password.db_password.result
    engine   = "postgres"
    host     = aws_db_instance.postgres.address
    port     = aws_db_instance.postgres.port
    dbname   = var.db_name
  })
}

# IAM Role for RDS Enhanced Monitoring
resource "aws_iam_role" "rds_monitoring" {
  name = "${var.project_name}-rds-monitoring-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "monitoring.rds.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name        = "${var.project_name}-rds-monitoring-role"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

# RDS PostgreSQL Instance
resource "aws_db_instance" "postgres" {
  # Basic Configuration
  identifier     = "${var.project_name}-${var.environment}-postgres"
  engine         = "postgres"
  engine_version = var.postgres_version
  instance_class = var.db_instance_class

  # Database Configuration
  db_name  = var.db_name
  username = var.db_username
  password = random_password.db_password.result

  # Storage Configuration
  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.rds.arn

  # Network Configuration
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [var.rds_security_group_id]
  publicly_accessible    = false

  # High Availability & Backup
  multi_az                 = var.db_multi_az
  backup_retention_period  = 7
  backup_window            = "03:00-04:00"         # UTC
  maintenance_window       = "Sun:04:00-Sun:05:00" # UTC
  delete_automated_backups = false

  # Performance & Monitoring
  parameter_group_name                  = aws_db_parameter_group.postgres.name
  monitoring_interval                   = 60
  monitoring_role_arn                   = aws_iam_role.rds_monitoring.arn
  performance_insights_enabled          = true
  performance_insights_retention_period = 7

  # Security
  deletion_protection       = var.environment == "production" ? true : false
  skip_final_snapshot       = var.environment == "production" ? false : true
  final_snapshot_identifier = var.environment == "production" ? "${var.project_name}-${var.environment}-postgres-final-snapshot-${formatdate("YYYY-MM-DD-hhmm", timestamp())}" : null

  # Enable logging
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  tags = {
    Name        = "${var.project_name}-postgres"
    Environment = var.environment
  }

  depends_on = [
    aws_db_subnet_group.main,
    aws_db_parameter_group.postgres,
    aws_kms_key.rds
  ]
}

# ============================================
# Storage Resources - KMS Keys
# ============================================

# KMS Key for EBS volumes
resource "aws_kms_key" "ebs" {
  description             = "EBS encryption key for ${var.project_name}-${var.environment}"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  tags = {
    Name        = "${var.project_name}-ebs-kms-key"
    Environment = var.environment
  }
}

resource "aws_kms_alias" "ebs" {
  name          = "alias/${var.project_name}-${var.environment}-ebs-key"
  target_key_id = aws_kms_key.ebs.key_id
}

# KMS Key for EFS
resource "aws_kms_key" "efs" {
  description             = "EFS encryption key for ${var.project_name}-${var.environment}"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  tags = {
    Name        = "${var.project_name}-efs-kms-key"
    Environment = var.environment
  }
}

resource "aws_kms_alias" "efs" {
  name          = "alias/${var.project_name}-${var.environment}-efs-key"
  target_key_id = aws_kms_key.efs.key_id
}

# ============================================
# Redis Resources 
# ============================================

# ElastiCache Subnet Group
resource "aws_elasticache_subnet_group" "redis" {
  name       = "${var.project_name}-${var.environment}-redis-subnet-group"
  subnet_ids = var.private_subnet_ids

  tags = {
    Name        = "${var.project_name}-redis-subnet-group"
    Environment = var.environment
  }
}

# ElastiCache Redis Cluster
resource "aws_elasticache_replication_group" "redis" {
  replication_group_id = "${var.project_name}-${var.environment}-redis"
  description          = "Redis cluster for Online Boutique cart service"  
  engine               = "redis"
  engine_version       = "7.0"
  node_type            = "cache.t3.micro"
  num_cache_clusters   = var.environment == "prod" ? 2 : 1
  parameter_group_name = "default.redis7"
  port                 = 6379
  
  subnet_group_name = aws_elasticache_subnet_group.redis.name
  # Use only the custom Redis security group initially
  security_group_ids = [
    var.redis_security_group_id
  ]
  
  at_rest_encryption_enabled = true
  transit_encryption_enabled = false
  
  automatic_failover_enabled = var.environment == "prod" ? true : false
  multi_az_enabled          = var.environment == "prod" ? true : false
  
  snapshot_retention_limit = 5
  snapshot_window         = "03:00-05:00"
  
  tags = {
    Name        = "${var.project_name}-redis"
    Environment = var.environment
  }
}

# ============================================
# EFS Resources
# ============================================

resource "aws_efs_file_system" "shared_storage" {
  creation_token                  = "${var.project_name}-${var.environment}-efs"
  performance_mode                = "generalPurpose"
  throughput_mode                 = "provisioned"
  provisioned_throughput_in_mibps = 100
  encrypted                       = true
  kms_key_id                      = aws_kms_key.efs.arn

  tags = {
    Name        = "${var.project_name}-${var.environment}-efs"
    Environment = var.environment
  }
}

# EFS Mount Targets
resource "aws_efs_mount_target" "shared_storage" {
  count           = length(var.private_subnet_ids)
  file_system_id  = aws_efs_file_system.shared_storage.id
  subnet_id       = var.private_subnet_ids[count.index]
  security_groups = [var.efs_security_group_id]
}