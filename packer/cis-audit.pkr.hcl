packer {
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = "~> 1.8"
    }
    ansible = {
      source  = "github.com/hashicorp/ansible"
      version = "~> 1.1"
    }
  }
}

variable "ami_id" {
  type        = string
  description = "Existing AMI to boot and audit"
}

variable "aws_region" {
  type        = string
  description = "AWS region containing the AMI"
  default     = "us-east-1"
}

source "amazon-ebs" "cis_audit" {
  ami_name                                  = "cis-audit-only"
  instance_type                             = "t3.medium"
  region                                    = var.aws_region
  source_ami                                = var.ami_id
  ssh_username                              = "ubuntu"
  skip_create_ami                           = true
  temporary_security_group_source_public_ip = true

  run_tags = {
    Name      = "dragonwilds-cis-audit"
    ManagedBy = "Packer"
    Purpose   = "Temporary CIS audit"
  }
}

build {
  name    = "cis-audit"
  sources = ["source.amazon-ebs.cis_audit"]

  provisioner "ansible" {
    playbook_file = "${path.root}/../ansible/harden.yml"
    user          = "ubuntu"
    extra_arguments = [
      "--become",
      "--extra-vars",
      jsonencode({
        audit_only         = true
        setup_audit        = true
        run_audit          = true
        fetch_audit_output = true
      }),
    ]
    ansible_env_vars = [
      "ANSIBLE_HOST_KEY_CHECKING=False",
      "ANSIBLE_NOCOWS=1",
    ]
  }
}
