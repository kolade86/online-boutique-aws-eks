#!/bin/bash
# Bootstrap Terraform Backend - Create the S3 bucket for remote state
# State locking uses a lock file in the same bucket (use_lockfile in
# backend.tf), so no DynamoDB table is needed.

set -e  # Exit on any error

# Configuration
PROJECT_NAME="online-boutique"
ENVIRONMENT="dev"
AWS_REGION="us-east-1"
BUCKET_NAME="${PROJECT_NAME}-${ENVIRONMENT}-terraform-state"

echo "=========================================="
echo "Creating Terraform Backend Resources"
echo "=========================================="
echo "Project: ${PROJECT_NAME}"
echo "Environment: ${ENVIRONMENT}"
echo "Region: ${AWS_REGION}"
echo "Bucket: ${BUCKET_NAME}"
echo ""

# 1. Create S3 bucket for Terraform state
echo "📦 Creating S3 bucket..."
aws s3api create-bucket \
  --bucket "${BUCKET_NAME}" \
  --region "${AWS_REGION}"

echo "✅ S3 bucket created"

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

echo ""
echo "=========================================="
echo "✅ Backend resources created successfully!"
echo "=========================================="
echo ""
echo "📝 Next Steps:"
echo "1. terraform/environments/${ENVIRONMENT}/backend.tf already points at this"
echo "   bucket, with use_lockfile = true for state locking:"
echo ""
echo "-------------------------------------------"
cat << EOF
terraform {
  backend "s3" {
    bucket       = "${BUCKET_NAME}"
    key          = "terraform.tfstate"
    region       = "${AWS_REGION}"
    encrypt      = true
    use_lockfile = true
  }
}
EOF
echo "-------------------------------------------"
echo ""
echo "2. Initialize and apply (Terraform >= 1.16.0):"
echo "   cd terraform/environments/${ENVIRONMENT}"
echo "   terraform init"
echo "   terraform apply"
echo ""