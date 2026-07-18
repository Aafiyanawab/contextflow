# Handy values printed after `terraform apply`.

output "public_ip" {
  description = "Public IPv4 of the k3s box (auto-assigned; changes on stop/start)."
  value       = aws_instance.k3s.public_ip
}

output "ssh_command" {
  description = "Copy-paste to SSH into the box."
  value       = "ssh -i ~/.ssh/contextflow ubuntu@${aws_instance.k3s.public_ip}"
}

output "s3_bucket" {
  description = "Uploads bucket name (set S3_BUCKET to this in prod)."
  value       = aws_s3_bucket.uploads.bucket
}

output "ecr_repo_url" {
  description = "ECR repository URL to tag/push the image to."
  value       = aws_ecr_repository.app.repository_url
}

output "github_actions_role_arn" {
  description = "IAM role ARN the GitHub Actions workflow assumes via OIDC."
  value       = aws_iam_role.github_actions.arn
}

output "rds_endpoint" {
  description = "RDS Postgres endpoint (host:port) — reachable only inside the VPC."
  value       = aws_db_instance.main.endpoint
}

output "database_url" {
  description = "DATABASE_URL for the app (contains the DB password)."
  value       = "postgresql+psycopg2://contextflow:${random_password.db.result}@${aws_db_instance.main.endpoint}/contextflow"
  sensitive   = true
}
