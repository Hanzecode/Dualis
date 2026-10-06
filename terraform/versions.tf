# terraform/versions.tf — provider + Terraform version pins.
# Keeping this separate from main.tf is a convention, not a requirement —
# it's the one file worth looking at first to know what this was tested
# against.

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region
  # Credentials are NOT set here — Terraform reads them from the standard
  # AWS credential chain (environment variables, ~/.aws/credentials via
  # `aws configure`, or an assumed role). Never hardcode a key/secret in
  # a .tf file — anything in this directory can end up committed to git.
}
