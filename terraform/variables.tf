# Inputs to the configuration. Defaults are set here; override per-machine
# in terraform.tfvars (which is gitignored).

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = "Named AWS CLI profile Terraform authenticates with — keeps this project on its own IAM user (contextflow-terraform), separate from the default/Blue-Green profile."
  type        = string
  default     = "contextflow"
}

variable "project" {
  description = "Name prefix and tag applied to all resources."
  type        = string
  default     = "contextflow"
}

variable "my_ip_cidr" {
  description = <<-EOT
    Your public IP in CIDR form (e.g. 1.2.3.4/32), used to restrict SSH and
    the k3s API to just you. Residential IPs change — if SSH stops working,
    update this in terraform.tfvars and re-apply.
  EOT
  type        = string
}

variable "github_repo" {
  description = "GitHub repo (owner/name) allowed to assume the CI role via OIDC — scopes the trust policy to just this repository."
  type        = string
}

variable "ssh_public_key_path" {
  description = "Path to the SSH public key uploaded to AWS for EC2 access (the private key stays local)."
  type        = string
}

variable "instance_type" {
  description = <<-EOT
    EC2 instance type. We default to x86_64 (t3.small) because the image we
    built locally is linux/amd64 — it must match the instance architecture or
    the container won't run. Switching to Graviton (t4g.small, ~20% cheaper)
    is a later optimization once we build multi-arch images in CI.
  EOT
  type        = string
  default     = "t3.small"
}
