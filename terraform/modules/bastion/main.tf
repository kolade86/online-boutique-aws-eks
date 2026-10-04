# modules/bastion/main.tf
# Bastion Module - Secure access point for cluster management

# Get latest Amazon Linux 2023 AMI
data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

# IAM Role for Bastion Host
resource "aws_iam_role" "bastion" {
  name = "${var.project_name}-${var.environment}-bastion-role"

  assume_role_policy = jsonencode({
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
    }]
    Version = "2012-10-17"
  })

  tags = {
    Name        = "${var.project_name}-bastion-role"
    Environment = var.environment
  }
}

# IAM policies for bastion host
resource "aws_iam_role_policy_attachment" "bastion_ssm" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
  role       = aws_iam_role.bastion.name
}

resource "aws_iam_role_policy_attachment" "bastion_eks_read" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
  role       = aws_iam_role.bastion.name
}

resource "aws_iam_role_policy_attachment" "bastion_ecr_read" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
  role       = aws_iam_role.bastion.name
}

# Custom policy for EKS cluster access
resource "aws_iam_policy" "bastion_eks_access" {
  name        = "${var.project_name}-${var.environment}-bastion-eks-access"
  description = "Policy for bastion host EKS access"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "eks:AccessKubernetesApi",
          "eks:DescribeCluster",
          "eks:DescribeNodegroup",
          "eks:ListClusters",
          "eks:ListNodegroups"
        ]
        Resource = "*"
      }
    ]
  })

  tags = {
    Name        = "${var.project_name}-bastion-eks-access-policy"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "bastion_eks_access" {
  policy_arn = aws_iam_policy.bastion_eks_access.arn
  role       = aws_iam_role.bastion.name
}

# IAM Instance Profile for Bastion
resource "aws_iam_instance_profile" "bastion" {
  name = "${var.project_name}-${var.environment}-bastion-profile"
  role = aws_iam_role.bastion.name

  tags = {
    Name        = "${var.project_name}-${var.environment}-bastion-profile"
    Environment = var.environment
  }
}

# Launch Template for Bastion Host
resource "aws_launch_template" "bastion" {
  name_prefix   = "${var.project_name}-${var.environment}-bastion-"
  description   = "Launch template for bastion host"
  image_id      = data.aws_ami.amazon_linux.id
  instance_type = var.bastion_instance_type

  vpc_security_group_ids = [var.bastion_security_group_id]

  iam_instance_profile {
    name = aws_iam_instance_profile.bastion.name
  }

  # Reference external user data file
  user_data = base64encode(templatefile("${path.module}/bastion-userdata.sh", {
    cluster_name = var.cluster_name
    aws_region   = var.aws_region
  }))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name        = "${var.project_name}-bastion"
      Environment = var.environment
    }
  }

  tags = {
    Name        = "${var.project_name}-bastion-template"
    Environment = var.environment
  }
}

# Bastion Host Instance
resource "aws_instance" "bastion" {
  launch_template {
    id      = aws_launch_template.bastion.id
    version = "$Latest"
  }

  subnet_id = var.private_subnet_ids[0]

  tags = {
    Name        = "${var.project_name}-bastion"
    Environment = var.environment
  }

  # The instance takes its user data from the launch template; this resource
  # never sets user_data itself. The AWS provider (v5.100.0) still plans an
  # in-place user_data change on it (hash ffb9280c... -> 8e161070...) when
  # nothing has changed. Checked on 2026-10-04: the instance, launch template
  # version 1 (its only version, which the instance was launched from) and
  # the script rendered from bastion-userdata.sh all hash to ffb9280c..., and
  # 8e161070... matches no rendering or encoding of the script.
  #
  # Ignoring the attribute hides only that phantom diff. A real change to
  # bastion-userdata.sh still shows up in the plan, on
  # aws_launch_template.bastion. User data runs only at first boot, so a new
  # script does nothing on a running instance anyway: to apply one, replace
  # it (terraform apply -replace=module.bastion.aws_instance.bastion).
  lifecycle {
    ignore_changes = [user_data]
  }
}
