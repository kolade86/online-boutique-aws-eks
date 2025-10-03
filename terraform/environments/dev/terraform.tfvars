# environments/dev/terraform.tfvars
# Variable values for Dev Environment - Complete

# ============================================
# Core Project & Environment
# ============================================

project_name = "online-boutique"
environment  = "dev"
aws_region   = "us-east-1"

# ============================================
# Networking
# ============================================

vpc_cidr = "10.0.0.0/16"

# ============================================
# EKS Configuration
# ============================================

#cluster_name    = "ola-cluster-dev"
cluster_version = "1.33"

# Node Group Configuration
desired_size         = 4
min_size             = 3
max_size             = 12
instance_types       = ["t3.large"]
capacity_type        = "ON_DEMAND"
node_update_strategy = "rolling"

# EKS Endpoint Configuration (Dev - more open for testing)
endpoint_private_access = true
endpoint_public_access  = true
public_access_cidrs     = ["0.0.0.0/0"]

# Node Placement (Dev - can use public subnets for easier access)
use_private_subnets_for_nodes = false

# ============================================
# RDS Configuration
# ============================================

postgres_version         = "17.6"
db_instance_class        = "db.t3.micro"
db_name                  = "onlineboutiquedb"
db_username              = "onlineboutiqueadmin"
db_allocated_storage     = 20
db_max_allocated_storage = 100
db_multi_az              = false # Set to true for production

# ============================================
# Application Configuration
# ============================================

app_namespace = "online-boutique-dev"
app_name      = "online-boutique"

# ============================================
# Platform Services
# ============================================

cluster_autoscaler_version = "v1.33.0"

# ============================================
# Infrastructure Instance Types
# ============================================

bastion_instance_type = "t3.micro"

# ============================================
# CICD Configuration
# ============================================

github_repo = "kolade86/online-boutique-aws-eks"

# ============================================
# Observability Configuration
# ============================================

alert_email_address = "koladeodu20@gmail.com"