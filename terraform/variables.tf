# terraform/variables.tf — every knob, with the reasoning for its default.

variable "aws_region" {
  description = "AWS region to provision into."
  type        = string
  default     = "eu-west-2" # London — closest to Alpaca/FRED for you; change freely
}

variable "instance_type" {
  description = "EC2 instance size. t3.micro (1GB RAM, free-tier eligible on most new accounts) is what this was written for — the engine's own build is small (a couple of translation units, seconds to compile locally), so building the full stack with `docker compose up --build` on the instance itself should be fine. If it ever isn't, the fallback is building images locally and pushing them to a registry instead of building on the VM."
  type        = string
  default     = "t3.micro"
}

variable "public_key_path" {
  description = "Path to YOUR existing SSH public key. Terraform imports this into AWS as a key pair — it never generates or touches a private key, so nothing sensitive ends up in Terraform state."
  type        = string
  default     = "~/.ssh/id_rsa.pub"
}

variable "allowed_cidr" {
  description = "CIDR allowed to reach SSH (22) and the dashboard API (8000). Defaults to \"your IP only\" — set this explicitly rather than trusting a default of 0.0.0.0/0, which would open the box to the entire internet."
  type        = string
  # No safe default on purpose — force a conscious choice.
  # Find yours with: curl -s https://checkip.amazonaws.com
  # Then pass -var="allowed_cidr=YOUR_IP/32"
}

variable "project_name" {
  description = "Prefix used to tag/name every resource this creates, so they're easy to find (and easy to confirm are all gone after `terraform destroy`)."
  type        = string
  default     = "dualis"
}
