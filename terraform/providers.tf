# Terraform + AWS provider setup. `terraform init` reads this block and
# downloads the AWS provider plugin into .terraform/.
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile # authenticate as the dedicated contextflow-terraform user

  # Tag EVERY resource we create — so cost is attributable and cleanup is
  # trivial. Cost-awareness is part of the cloud-engineer job.
  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}
