#!/bin/bash
# Bootstrap Terraform Backend - Create S3 Bucket and DynamoDB Table
# This script creates the required AWS resources for Terraform remote state

set -e  # Exit on any error

# Configuration
PROJECT_NAME="online-boutique"
ENVIRONMENT="dev"
AWS_REGION="us-east-1"
BUCKET_NAME="${PROJECT_NAME}-${ENVIRONMENT}-terraform-state"
DYNAMODB_TABLE="${PROJECT_NAME}-${ENVIRONMENT}-terraform-locks"

echo "=========================================="
echo "Creating Terraform Backend Resources"
echo "=========================================="
echo "Project: ${PROJECT_NAME}"
echo "Environment: ${ENVIRONMENT}"
echo "Region: ${AWS_REGION}"
echo "Bucket: ${BUCKET_NAME}"
echo "DynamoDB Table: ${DYNAMODB_TABLE}"
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

# 5. Create DynamoDB table for state locking
echo "🗄️  Creating DynamoDB table..."
aws dynamodb create-table \
  --table-name "${DYNAMODB_TABLE}" \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region "${AWS_REGION}"

echo "✅ DynamoDB table created"

# Wait for table to be active
echo "⏳ Waiting for DynamoDB table to be active..."
aws dynamodb wait table-exists --table-name "${DYNAMODB_TABLE}" --region "${AWS_REGION}"

echo ""
echo "=========================================="
echo "✅ Backend resources created successfully!"
echo "=========================================="
echo ""
echo "📝 Next Steps:"
echo "1. Create backend.tf in terraform/environments/${ENVIRONMENT}/"
echo "2. Add the following configuration:"
echo ""
echo "-------------------------------------------"
cat << EOF
terraform {
  backend "s3" {
    bucket         = "${BUCKET_NAME}"
    key            = "terraform.tfstate"
    region         = "${AWS_REGION}"
    encrypt        = true
    dynamodb_table = "${DYNAMODB_TABLE}"
  }
}
EOF
echo "-------------------------------------------"
echo ""
echo "3. Initialize Terraform:"
echo "   cd terraform/environments/${ENVIRONMENT}"
echo "   terraform init"
echo "   terraform apply"
echo ""