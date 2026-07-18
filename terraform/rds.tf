# ---- Database: managed PostgreSQL on RDS (free-tier db.t3.micro) ----

# Two PRIVATE subnets in two AZs. RDS demands a subnet group spanning >=2 AZs
# (for failover/relocation) even for a Single-AZ instance. These have NO route
# to the internet gateway, so they're private by default (VPC local route only).
resource "aws_subnet" "private_a" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.2.0/24"
  availability_zone = "${var.aws_region}a"
  tags              = { Name = "${var.project}-private-a" }
}

resource "aws_subnet" "private_b" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.3.0/24"
  availability_zone = "${var.aws_region}b"
  tags              = { Name = "${var.project}-private-b" }
}

# The pool of subnets RDS may place the database in.
resource "aws_db_subnet_group" "main" {
  name       = "${var.project}-db-subnet-group"
  subnet_ids = [aws_subnet.private_a.id, aws_subnet.private_b.id]
  tags       = { Name = "${var.project}-db-subnet-group" }
}

# DB firewall: allow Postgres (5432) ONLY from the app's security group.
resource "aws_security_group" "db" {
  name        = "${var.project}-db-sg"
  description = "RDS Postgres: 5432 only from the app/EC2 security group"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Postgres from the app tier"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.web.id] # source = the app's SG, not an IP
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project}-db-sg" }
}

# Pick a valid, currently-offered default Postgres version (avoids
# "version not available" errors from hardcoding).
data "aws_rds_engine_version" "postgres" {
  engine       = "postgres"
  default_only = true
}

# Generate the DB master password (no special chars, so it's URL-safe in
# DATABASE_URL). Stored in Terraform state, never in the code.
resource "random_password" "db" {
  length  = 24
  special = false
}

resource "aws_db_instance" "main" {
  identifier     = "${var.project}-db"
  engine         = "postgres"
  engine_version = data.aws_rds_engine_version.postgres.version
  instance_class = "db.t3.micro" # free-tier eligible

  allocated_storage = 20    # GB (free-tier max)
  storage_type      = "gp2" # free-tier is General Purpose (SSD) = gp2

  db_name  = "contextflow"
  username = "contextflow"
  password = random_password.db.result

  db_subnet_group_name    = aws_db_subnet_group.main.name
  vpc_security_group_ids  = [aws_security_group.db.id]
  multi_az                = false # Single-AZ (free-tier); flip to true later, no network changes
  publicly_accessible     = false # PRIVATE — no public endpoint
  backup_retention_period = 1 # Free Plan caps backup retention; 1 day is the minimum with backups on
  skip_final_snapshot     = true # dev convenience: no snapshot on destroy
  deletion_protection     = false

  tags = { Name = "${var.project}-db" }
}
