# Partial backend configuration: the bucket name contains the AWS account ID
# (online-boutique-dev-<account-id>-terraform-state), so a new sandbox account
# never collides with an old one's bucket. Supply it at init time:
#   terraform init -reconfigure -backend-config="bucket=<name>"
# bootstrap-backend.sh prints the exact command; the CI workflows build the
# name from the AWS_ACCOUNT_ID repo variable.
terraform {
  backend "s3" {
    key     = "terraform.tfstate"
    region  = "us-east-1"
    encrypt = true
    # State locking with a lock file in the bucket (terraform.tfstate.tflock),
    # not a DynamoDB table: dynamodb_table is deprecated since Terraform 1.11
    use_lockfile = true
  }
}
