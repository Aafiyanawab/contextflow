# ---- Compute: the EC2 box that will run k3s ----

# Look up the latest Ubuntu 22.04 LTS (amd64) AMI dynamically — never hardcode
# an image ID that goes stale. (amd64 to match our locally-built image.)
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical (official Ubuntu publisher)

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# Upload our SSH public key to AWS; the private key stays on your machine.
resource "aws_key_pair" "main" {
  key_name   = "${var.project}-key"
  public_key = file(var.ssh_public_key_path)
}

# The instance itself.
resource "aws_instance" "k3s" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.web.id]
  key_name               = aws_key_pair.main.key_name
  iam_instance_profile   = aws_iam_instance_profile.ec2.name

  root_block_device {
    volume_size = 30 # GB
    volume_type = "gp3"
  }

  # IMDSv2 required (blocks the older, SSRF-prone IMDSv1). hop_limit=2 so that
  # k3s PODS (one extra network hop behind the CNI bridge) can still reach IMDS
  # to assume the instance role for S3/ECR — with hop_limit=1 pod AWS calls fail.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  # Minimal bootstrap only. We install k3s BY HAND in Phase 2b (understand
  # before automate); later that step can move into this script.
  user_data = <<-EOF
    #!/bin/bash
    apt-get update -y
  EOF

  tags = { Name = "${var.project}-k3s" }
}

# No Elastic IP: we use the subnet's auto-assigned public IPv4
# (aws_instance.k3s.public_ip). Same cost while running, but $0 when the
# instance is stopped — better for credit conservation. Add an EIP + Route 53
# later if we want a rock-stable address.
