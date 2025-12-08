terraform {
  backend "s3" {
    bucket         = "online-boutique-dev-terraform-state"
    key            = "terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "online-boutique-dev-terraform-locks"
  }
}
