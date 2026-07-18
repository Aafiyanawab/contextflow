# ---- Storage: private S3 bucket for uploaded documents (the AWS seam) ----

# Bucket names are GLOBALLY unique, so we suffix with the account id.
resource "aws_s3_bucket" "uploads" {
  bucket = "${var.project}-uploads-${data.aws_caller_identity.current.account_id}"
  tags   = { Name = "${var.project}-uploads" }
}

# Uploads are private — served through the app, never public. Block it all.
resource "aws_s3_bucket_public_access_block" "uploads" {
  bucket                  = aws_s3_bucket.uploads.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning: recover overwritten/deleted objects. Good hygiene.
resource "aws_s3_bucket_versioning" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  versioning_configuration {
    status = "Enabled"
  }
}
