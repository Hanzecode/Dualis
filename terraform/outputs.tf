# terraform/outputs.tf

output "instance_public_ip" {
  description = "Public IP of the VM. Also written into ansible/inventory.ini automatically."
  value       = aws_instance.this.public_ip
}

output "ssh_command" {
  description = "Copy-paste command to SSH in directly, for checking on things outside Ansible."
  value       = "ssh -i ${replace(var.public_key_path, ".pub", "")} ubuntu@${aws_instance.this.public_ip}"
}

output "dashboard_url" {
  description = "Once Ansible has deployed the stack, the dashboard API is reachable here (from allowed_cidr only)."
  value       = "http://${aws_instance.this.public_ip}:8000/api/snapshot"
}
