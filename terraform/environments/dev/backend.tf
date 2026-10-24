terraform {
  backend "s3" {
    bucket  = "online-boutique-dev-terraform-state"
    key     = "terraform.tfstate"
    region  = "us-east-1"
    encrypt = true
    # State locking with a lock file in the bucket (terraform.tfstate.tflock),
    # not a DynamoDB table: dynamodb_table is deprecated since Terraform 1.11
    use_lockfile = true
  }
}
