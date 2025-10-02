#!/bin/bash
# Bastion Host User Data Script
# This script runs on first boot to configure the bastion host

# Update system packages
dnf update -y

# Install SSM Agent
dnf install -y amazon-ssm-agent

# Enable and start SSM Agent
systemctl enable amazon-ssm-agent
systemctl start amazon-ssm-agent

# Install essential utilities
dnf install -y wget curl unzip git

# Install network and database tools 
dnf install -y postgresql16 bind-utils telnet nc

# Install AWS CLI v2
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
./aws/install
rm -rf awscliv2.zip aws/

# Install kubectl
curl -LO "https://dl.k8s.io/release/v1.32.0/bin/linux/amd64/kubectl"
chmod +x kubectl
sudo mv kubectl /usr/local/bin/kubectl

# Install helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# Configure kubectl for EKS cluster
aws eks update-kubeconfig --region ${aws_region} --name ${cluster_name}

# Log completion
echo "$(date): Bastion host initialization completed" >> /var/log/user-data.log
echo "Installed tools: AWS CLI, kubectl, helm, postgresql16, network utilities" >> /var/log/user-data.log