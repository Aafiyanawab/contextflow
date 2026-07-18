# ---- ECR: private registry for the ContextFlow image ----
resource "aws_ecr_repository" "app" {
  name                 = "${var.project}-web"
  image_tag_mutability = "MUTABLE" # allow re-pushing :latest (real CI uses immutable SHA tags)

  image_scanning_configuration {
    scan_on_push = true # CVE-scan every pushed image
  }

  tags = { Name = "${var.project}-web" }
}

# Keep only the last 10 images so storage doesn't grow unbounded.
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep only the last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# ---- Least-privilege ECR PULL permission for the EC2 instance role ----
# Attached to the SAME role the box already uses for S3. The node can now pull
# from ECR using its instance role (temporary creds via IMDS) — no stored keys.
resource "aws_iam_role_policy" "ecr_pull" {
  name = "${var.project}-ecr-pull"
  role = aws_iam_role.ec2.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Get a temporary registry login token. This action is REGISTRY-level
        # and cannot be scoped to a single repo, so Resource must be "*".
        Sid      = "EcrAuthToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        # The actual pull actions — scoped to ONLY our repository (least privilege):
        #   BatchCheckLayerAvailability -> which layers already exist locally
        #   GetDownloadUrlForLayer      -> a URL to download each missing layer blob
        #   BatchGetImage               -> the image manifest (layer/config list)
        Sid    = "EcrPullThisRepoOnly"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage"
        ]
        Resource = aws_ecr_repository.app.arn
      }
    ]
  })
}
