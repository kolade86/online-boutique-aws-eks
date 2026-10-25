#!/bin/bash
# Bootstrap Terraform Backend - Create the S3 bucket for remote state, and
# initialise Terraform against it
# The bucket name includes the AWS account ID, so each account gets its own
# and a new sandbox never collides with an old one's bucket. backend.tf leaves
# the bucket out (partial configuration); this script writes it to
# terraform/environments/<env>/backend.hcl (ignored by Git) and runs
# `terraform init -reconfigure -backend-config=backend.hcl` there.
# State locking uses a lock file in the same bucket (use_lockfile in
# backend.tf), so no DynamoDB table is needed.
# Safe to run again: an existing bucket is kept, and its settings re-applied.

set -e  # Exit on any error

# Configuration
PROJECT_NAME="online-boutique"
ENVIRONMENT="dev"
AWS_REGION="us-east-1"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET_NAME="${PROJECT_NAME}-${ENVIRONMENT}-${AWS_ACCOUNT_ID}-terraform-state"
TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/terraform/environments/${ENVIRONMENT}"

echo "=========================================="
echo "Creating Terraform Backend Resources"
echo "=========================================="
echo "Project: ${PROJECT_NAME}"
echo "Environment: ${ENVIRONMENT}"
echo "Region: ${AWS_REGION}"
echo "Account: ${AWS_ACCOUNT_ID}"
echo "Bucket: ${BUCKET_NAME}"
echo ""

# 1. Create S3 bucket for Terraform state (kept if it already exists)
if aws s3api head-bucket --bucket "${BUCKET_NAME}" 2>/dev/null; then
  echo "📦 S3 bucket already exists: keeping it"
else
  echo "📦 Creating S3 bucket..."
  aws s3api create-bucket \
    --bucket "${BUCKET_NAME}" \
    --region "${AWS_REGION}"
  echo "✅ S3 bucket created"
fi

# 2. Enable versioning (protects against accidental deletions)
echo "🔄 Enabling versioning..."
aws s3api put-bucket-versioning \
  --bucket "${BUCKET_NAME}" \
  --versioning-configuration Status=Enabled

echo "✅ Versioning enabled"

# 3. Enable encryption
echo "🔒 Enabling encryption..."
aws s3api put-bucket-encryption \
  --bucket "${BUCKET_NAME}" \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "AES256"
      }
    }]
  }'

echo "✅ Encryption enabled"

# 4. Block public access
echo "🚫 Blocking public access..."
aws s3api put-public-access-block \
  --bucket "${BUCKET_NAME}" \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

echo "✅ Public access blocked"

# 5. backend.hcl: the bucket for this account, read by terraform init
cat > "${TF_DIR}/backend.hcl" << EOF
# Written by bootstrap-backend.sh for account ${AWS_ACCOUNT_ID}. Not in Git.
bucket = "${BUCKET_NAME}"
EOF
echo "✅ Wrote terraform/environments/${ENVIRONMENT}/backend.hcl"

echo ""
echo "=========================================="
echo "✅ Backend resources created successfully!"
echo "=========================================="
echo ""

# 6. Initialise Terraform against it (Terraform >= 1.16.0)
if command -v terraform > /dev/null; then
  echo "🔧 terraform init -reconfigure -backend-config=backend.hcl (in terraform/environments/${ENVIRONMENT})"
  # TF_CLI_ARGS_init cleared for this call, in case it is already set as below
  (cd "${TF_DIR}" && TF_CLI_ARGS_init= terraform init -reconfigure -backend-config=backend.hcl)
  echo ""
  echo "📝 Next: cd terraform/environments/${ENVIRONMENT} && terraform plan"
else
  echo "📝 Terraform not found. Install Terraform >= 1.16.0, then:"
  echo "   cd terraform/environments/${ENVIRONMENT}"
  echo "   terraform init -reconfigure -backend-config=backend.hcl"
fi

echo ""
echo "So that a plain 'terraform init' works later, and plan and apply wait up to"
echo "5 minutes for a state lock instead of failing at once, add to ~/.bashrc:"
echo ""
# The function passes backend.hcl only where there is one, so other Terraform
# projects are unaffected
cat << 'EOF'
   terraform() {
     if [ -f backend.hcl ]; then
       TF_CLI_ARGS_init="-backend-config=backend.hcl" command terraform "$@"
     else
       command terraform "$@"
     fi
   }
   export TF_CLI_ARGS_plan="-lock-timeout=5m"
   export TF_CLI_ARGS_apply="-lock-timeout=5m"
EOF
echo ""
echo "A stale lock: scripts/tf-unlock.sh"
echo ""