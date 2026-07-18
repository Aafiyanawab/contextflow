data "aws_caller_identity" "current" {}

# ---- IAM: let the EC2 reach the uploads bucket WITHOUT stored access keys ----
# The instance ASSUMES a role; boto3 in the app picks up temporary creds from
# the instance metadata automatically. This is the production pattern.

# 1. A role that EC2 instances are allowed to assume.
resource "aws_iam_role" "ec2" {
  name = "${var.project}-ec2-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Name = "${var.project}-ec2-role" }
}

# 2. A policy granting access to ONLY the uploads bucket (least privilege).
resource "aws_iam_role_policy" "s3" {
  name = "${var.project}-s3-access"
  role = aws_iam_role.ec2.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.uploads.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.uploads.arn}/*"
      }
    ]
  })
}

# 3. Instance profile — the wiring that attaches the role to the EC2.
resource "aws_iam_instance_profile" "ec2" {
  name = "${var.project}-ec2-profile"
  role = aws_iam_role.ec2.name
}
