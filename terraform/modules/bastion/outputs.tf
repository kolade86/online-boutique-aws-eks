#modules/bastion/outputs.tf
# Outputs from the Bastion Module

# Instance Information
output "bastion_instance_id" {
  description = "Bastion host instance ID"
  value       = aws_instance.bastion.id
}

output "bastion_instance_arn" {
  description = "ARN of the bastion instance"
  value       = aws_instance.bastion.arn
}

output "bastion_private_ip" {
  description = "Private IP address of the bastion host"
  value       = aws_instance.bastion.private_ip
}

# IAM Information
output "bastion_role_arn" {
  description = "ARN of the bastion IAM role"
  value       = aws_iam_role.bastion.arn
}

output "bastion_role_name" {
  description = "Name of the bastion IAM role"
  value       = aws_iam_role.bastion.name
}

output "bastion_instance_profile_name" {
  description = "Name of the bastion instance profile"
  value       = aws_iam_instance_profile.bastion.name
}

output "bastion_instance_profile_arn" {
  description = "ARN of the bastion instance profile"
  value       = aws_iam_instance_profile.bastion.arn
}

# Connection Information
output "bastion_connection_command" {
  description = "Command to connect to bastion host via Session Manager"
  value       = "aws ssm start-session --target ${aws_instance.bastion.id} --region ${var.aws_region}"
}

# Post-deployment Commands
output "bastion_setup_commands" {
  description = "Commands to run after bastion deployment"
  value = {
    connect_to_bastion = "aws ssm start-session --target ${aws_instance.bastion.id} --region ${var.aws_region}"
    configure_kubectl  = "# On bastion: aws eks update-kubeconfig --region ${var.aws_region} --name ${var.cluster_name}"
    verify_cluster     = "# On bastion: kubectl get nodes"
    check_installed    = "# On bastion: kubectl version && helm version && aws --version"
  }
}