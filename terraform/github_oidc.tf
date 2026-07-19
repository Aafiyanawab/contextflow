# ---- GitHub Actions → AWS via OIDC (keyless CI/CD authentication) ----
# Replaces the old static-access-key approach. No AWS secret is ever stored in
# GitHub: each workflow run presents a short-lived, GitHub-signed identity token,
# and AWS STS exchanges it for ~1h temporary credentials — but only for OUR repo.

# GitHub's OIDC discovery certificate. We read its SHA-1 thumbprint dynamically
# instead of hardcoding it, so a GitHub cert rotation doesn't silently break us.
data "tls_certificate" "github" {
  url = "https://token.actions.githubusercontent.com/.well-known/openid-configuration"
}

# Tell AWS to TRUST identity tokens signed by GitHub Actions, for audience
# sts.amazonaws.com. This is the account-level "we recognize this IdP" record.
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.github.certificates[0].sha1_fingerprint]
}

# The role GitHub Actions assumes.
#   assume_role_policy = the TRUST policy: WHO may assume it (and under what conditions)
#   aws_iam_role_policy below = the PERMISSIONS policy: WHAT they can then do
resource "aws_iam_role" "github_actions" {
  name = "${var.project}-github-actions"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        # audience must be AWS STS (set by aws-actions/configure-aws-credentials)
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        # subject must be OUR repo on the main branch. A fork, a PR, another
        # branch, or any other repo produces a different `sub` and is DENIED.
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:ref:refs/heads/main"
        }
      }
    }]
  })

  tags = { Name = "${var.project}-github-actions" }
}

# Permissions for the CI role: PUSH images to our ECR repo — and nothing else.
# (Deploy-to-cluster permissions are added at the deploy milestone, once we pick
#  the mechanism, so this role stays least-privilege in the meantime.)
resource "aws_iam_role_policy" "github_ecr_push" {
  name = "${var.project}-github-ecr-push"
  role = aws_iam_role.github_actions.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Registry-level login token — cannot be scoped to one repo.
        Sid      = "EcrAuthToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        # The push verbs (upload layers + write the manifest), scoped to our repo:
        #   InitiateLayerUpload / UploadLayerPart / CompleteLayerUpload -> send layer blobs
        #   PutImage                                                    -> write the manifest/tag
        #   BatchCheckLayerAvailability                                 -> skip layers already there
        #   BatchGetImage / GetDownloadUrlForLayer                      -> read (cache/inspect)
        Sid    = "EcrPushThisRepoOnly"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer"
        ]
        Resource = aws_ecr_repository.app.arn
      }
    ]
  })
}

# Deploy permission for the CI role: run the rolling update on the k3s node via
# SSM Run Command — no SSH, no open ports, no stored keys. Least-privilege:
# SendCommand is scoped to exactly OUR instance and ONLY the managed
# AWS-RunShellScript document; the poll action can't be resource-scoped.
resource "aws_iam_role_policy" "github_ssm_deploy" {
  name = "${var.project}-github-ssm-deploy"
  role = aws_iam_role.github_actions.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "SsmSendCommandToDeployTarget"
        Effect   = "Allow"
        Action   = "ssm:SendCommand"
        Resource = [
          aws_instance.k3s.arn,
          "arn:aws:ssm:${var.aws_region}::document/AWS-RunShellScript"
        ]
      },
      {
        # Poll the command's terminal status + output. GetCommandInvocation
        # does not support resource-level scoping, so Resource must be "*".
        Sid      = "SsmReadCommandResult"
        Effect   = "Allow"
        Action   = "ssm:GetCommandInvocation"
        Resource = "*"
      }
    ]
  })
}
