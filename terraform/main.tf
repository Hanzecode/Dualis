# terraform/main.tf — one VM, one firewall rule, one imported SSH key.
# Deliberately this small: the goal is to demonstrate real IaC (state,
# plan/apply, a resource graph) for infrastructure that actually runs this
# project, not to build a production VPC in a weekend.

# ── Find the latest Ubuntu 22.04 LTS AMI ────────────────────────────────────
# AMI IDs are region-specific and change over time as Canonical publishes
# new builds — hardcoding one would silently go stale. This data source
# always resolves to whatever's current in the chosen region at apply time.
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical's official AWS account ID

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# ── SSH key: import YOUR public key, never generate a private one here ─────
resource "aws_key_pair" "this" {
  key_name   = "${var.project_name}-key"
  public_key = file(var.public_key_path)
}

# ── Firewall: only SSH and the dashboard API, only from allowed_cidr ───────
# ZMQ (5556/5557) and TimescaleDB (5434) are deliberately NOT opened here —
# they only need to be reachable inside the VM's own Docker network, the
# same "don't expose more than the job needs" principle as everything else
# in this project's risk design.
resource "aws_security_group" "this" {
  name        = "${var.project_name}-sg"
  description = "SSH + dashboard API, restricted to allowed_cidr"

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }

  ingress {
    description = "Dashboard API"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }

  egress {
    description = "All outbound (yfinance, FRED, Alpaca, apt, docker pull, github)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-sg"
  }
}

# ── The VM itself ───────────────────────────────────────────────────────────
resource "aws_instance" "this" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  key_name               = aws_key_pair.this.key_name
  vpc_security_group_ids = [aws_security_group.this.id]

  root_block_device {
    volume_size = 16 # GB — Docker images + build cache for 3 services add up fast on the 8GB default
    volume_type = "gp3"
  }

  tags = {
    Name = "${var.project_name}-vm"
  }
}

# ── Write an Ansible inventory automatically once the VM has an IP ─────────
# This is the hand-off point to the next tool: Ansible needs to know the
# instance's address, and this generates that file instead of you copying
# an IP between two terminals by hand.
resource "local_file" "ansible_inventory" {
  filename = "${path.module}/../ansible/inventory.ini"
  content  = <<-EOT
    [dualis]
    ${aws_instance.this.public_ip} ansible_user=ubuntu ansible_ssh_private_key_file=${replace(var.public_key_path, ".pub", "")}
  EOT
}
