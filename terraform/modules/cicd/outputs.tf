# modules/cicd/outputs.tf
# Outputs from the CICD Module

# ECR Outputs
output "ecr_repository_urls" {
  description = "Map of ECR repository URLs by service name"
  value = {
    for service, repo in aws_ecr_repository.microservices :
    service => repo.repository_url
  }
}

output "ecr_repository_arns" {
  description = "Map of ECR repository ARNs"
  value = {
    for service, repo in aws_ecr_repository.microservices :
    service => repo.arn
  }
}

output "ecr_repository_names" {
  description = "Map of ECR repository names"
  value = {
    for service, repo in aws_ecr_repository.microservices :
    service => repo.name
  }
}

output "ecr_registry_id" {
  description = "Registry ID (same for all repos)"
  value       = values(aws_ecr_repository.microservices)[0].registry_id
}

# GitHub OIDC Outputs
output "github_oidc_provider_arn" {
  description = "ARN of the GitHub OIDC provider"
  value       = aws_iam_openid_connect_provider.github.arn
}

output "github_actions_role_arn" {
  description = "ARN of the GitHub Actions IAM role"
  value       = aws_iam_role.github_actions.arn
}

output "github_actions_role_name" {
  description = "Name of the GitHub Actions IAM role"
  value       = aws_iam_role.github_actions.name
}

# ECR Login Command
output "ecr_login_command" {
  description = "Command to login to ECR"
  value       = "aws ecr get-login-password --region ${var.aws_region} | docker login --username AWS --password-stdin ${split("/", values(aws_ecr_repository.microservices)[0].repository_url)[0]}"
}

# List all repository URLs for easy reference
output "all_repository_urls" {
  description = "List of all ECR repository URLs"
  value = [
    for service in sort(keys(aws_ecr_repository.microservices)) :
    "${service}: ${aws_ecr_repository.microservices[service].repository_url}"
  ]
}

# GitHub Actions Workflow Information
output "github_actions_workflow_info" {
  description = "Information needed for GitHub Actions workflow configuration"
  value = {
    aws_region   = var.aws_region
    role_arn     = aws_iam_role.github_actions.arn
    cluster_name = var.cluster_name
    ecr_registry = split("/", values(aws_ecr_repository.microservices)[0].repository_url)[0]
    github_repo  = var.github_repo
  }
}

# Verification Commands
output "verification_commands" {
  description = "Commands to verify CICD setup"
  value = {
    list_all_repos     = "aws ecr describe-repositories --region ${var.aws_region} --query 'repositories[*].[repositoryName,repositoryUri]' --output table"
    check_github_oidc  = "aws iam get-openid-connect-provider --open-id-connect-provider-arn ${aws_iam_openid_connect_provider.github.arn}"
    check_github_role  = "aws iam get-role --role-name ${aws_iam_role.github_actions.name}"
    test_github_assume = "# From GitHub Actions, test: aws sts get-caller-identity"
  }
}
output "terraform_ci_role_arn" {
  description = "ARN of the plan-only role GitHub Actions assumes for Terraform CI"
  value       = aws_iam_role.terraform_ci.arn
}

output "terraform_ci_role_name" {
  description = "Name of the Terraform CI (plan-only) role"
  value       = aws_iam_role.terraform_ci.name
}
