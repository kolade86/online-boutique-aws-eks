# environments/dev/main.tf
# Dev Environment Configuration - Complete Infrastructure

# ============================================
# 1. Networking Module (Foundation)
# ============================================

module "networking" {
  source = "../../modules/networking"

  project_name = var.project_name
  environment  = var.environment
  vpc_cidr     = var.vpc_cidr
}

# ============================================
# 2. EKS Core Module
# ============================================

module "eks_core" {
  source = "../../modules/eks-core"

  project_name = var.project_name
  environment  = var.environment

  # EKS Configuration
  cluster_version = var.cluster_version

  # Networking inputs from networking module
  vpc_id                        = module.networking.vpc_id
  private_subnet_ids            = module.networking.private_subnet_ids
  public_subnet_ids             = module.networking.public_subnet_ids
  eks_cluster_security_group_id = module.networking.eks_cluster_security_group_id
  eks_nodes_security_group_id   = module.networking.eks_nodes_security_group_id

  # Node Group Configuration
  desired_size         = var.desired_size
  min_size             = var.min_size
  max_size             = var.max_size
  instance_types       = var.instance_types
  capacity_type        = var.capacity_type
  node_update_strategy = var.node_update_strategy

  # EKS Endpoint Configuration
  endpoint_private_access = var.endpoint_private_access
  endpoint_public_access  = var.endpoint_public_access
  public_access_cidrs     = var.public_access_cidrs

  # Node placement
  node_subnet_ids = module.networking.private_subnet_ids

  # Redis Security Group (for adding security group rule after cluster creation)
  enable_redis_sg_rule    = true
  redis_security_group_id = module.networking.redis_security_group_id

  depends_on = [module.networking]
}

# ============================================
# 3. Data Persistence Module
# ============================================

module "data_persistence" {
  source = "../../modules/data-persistence"

  project_name = var.project_name
  environment  = var.environment

  # Networking inputs
  vpc_id                  = module.networking.vpc_id
  private_subnet_ids      = module.networking.private_subnet_ids
  rds_security_group_id   = module.networking.rds_security_group_id
  efs_security_group_id   = module.networking.efs_security_group_id
  redis_security_group_id = module.networking.redis_security_group_id

  # RDS Configuration
  postgres_version         = var.postgres_version
  db_instance_class        = var.db_instance_class
  db_name                  = var.db_name
  db_username              = var.db_username
  db_allocated_storage     = var.db_allocated_storage
  db_max_allocated_storage = var.db_max_allocated_storage
  db_multi_az              = var.db_multi_az

  depends_on = [module.networking]
}

# ============================================
# 4. Platform Services Module
# ============================================

module "platform_services" {
  source = "../../modules/platform-services"

  project_name = var.project_name
  environment  = var.environment
  aws_region   = var.aws_region

  # Application Configuration
  app_namespace = var.app_namespace
  app_name      = var.app_name

  # From Networking
  vpc_id = module.networking.vpc_id

  # From EKS Core
  cluster_name            = module.eks_core.cluster_name
  cluster_version         = var.cluster_version
  oidc_provider_arn       = module.eks_core.oidc_provider_arn
  cluster_oidc_issuer_url = module.eks_core.cluster_oidc_issuer_url
  eks_nodes_role_arn      = module.eks_core.eks_nodes_role_arn
  eks_nodes_role_name     = module.eks_core.eks_nodes_role_name

  # From Data Persistence
  secrets_manager_secret_arn  = module.data_persistence.secrets_manager_secret_arn
  secrets_manager_secret_name = module.data_persistence.secrets_manager_secret_name

  # Storage Configuration - REMOVE THIS LINE:
  # ebs_kms_key_arn    = module.data_persistence.ebs_kms_key_arn

  # Keep this line for EFS:
  efs_file_system_id = module.data_persistence.efs_file_system_id

  # Cluster Autoscaler
  cluster_autoscaler_version = var.cluster_autoscaler_version

  # Redis connection info
  redis_endpoint = module.data_persistence.redis_endpoint
  redis_port     = module.data_persistence.redis_port

  depends_on = [module.eks_core, module.data_persistence]
}

# ============================================
# 5. CICD Module
# ============================================

module "cicd" {
  source = "../../modules/cicd"

  project_name   = var.project_name
  environment    = var.environment
  aws_region     = var.aws_region
  aws_account_id = data.aws_caller_identity.current.account_id

  # From EKS Core
  cluster_name    = module.eks_core.cluster_name
  eks_kms_key_arn = module.eks_core.kms_key_arn

  # GitHub Configuration
  github_repo = var.github_repo

  # SNS for notifications
  sns_topic_arn = module.observability.sns_topic_arn

  depends_on = [module.eks_core, module.observability]
}

# ============================================
# 6. Bastion Module
# ============================================

module "bastion" {
  source = "../../modules/bastion"

  project_name = var.project_name
  environment  = var.environment
  aws_region   = var.aws_region

  # Instance Configuration
  bastion_instance_type = var.bastion_instance_type

  # From Networking
  private_subnet_ids        = module.networking.private_subnet_ids
  bastion_security_group_id = module.networking.bastion_security_group_id

  # From EKS Core
  cluster_name = module.eks_core.cluster_name

  depends_on = [module.networking, module.eks_core]
}

# ============================================
# 7. Observability Module
# ============================================

module "observability" {
  source = "../../modules/observability"

  project_name   = var.project_name
  environment    = var.environment
  aws_region     = var.aws_region
  aws_account_id = data.aws_caller_identity.current.account_id

  # From EKS Core
  cluster_name            = module.eks_core.cluster_name
  oidc_provider_arn       = module.eks_core.oidc_provider_arn
  cluster_oidc_issuer_url = module.eks_core.cluster_oidc_issuer_url
  eks_nodes_role_arn      = module.eks_core.eks_nodes_role_arn
  eks_nodes_role_name     = module.eks_core.eks_nodes_role_name

  # Alerting Configuration
  alert_email_address = var.alert_email_address

  # CloudWatch Alarms — AWS Managed Services
  rds_instance_identifier    = module.data_persistence.rds_instance_identifier
  redis_replication_group_id = module.data_persistence.redis_replication_group_id

  depends_on = [module.eks_core, module.data_persistence]
}

# ============================================
# Data Sources
# ============================================

data "aws_caller_identity" "current" {}