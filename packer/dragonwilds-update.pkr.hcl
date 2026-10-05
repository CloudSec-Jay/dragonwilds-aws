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

variable "base_ami_id" {
  type        = string
  description = "Existing hardened Dragonwilds AMI to update"
}

variable "aws_region" {
  type        = string
  description = "AWS region in which to build the AMI"
  default     = "us-east-1"
}

variable "build_instance_type" {
  type        = string
  description = "Temporary EC2 instance type used by Packer"
  default     = "t3.medium"
}

locals {
  build_timestamp = regex_replace(timestamp(), "[- TZ:]", "")
}

source "amazon-ebs" "dragonwilds_update" {
  ami_name        = "dragonwilds-ubuntu24-cis-l1-${local.build_timestamp}"
  ami_description = "Updated hardened Ubuntu 24.04 image for RuneScape Dragonwilds"
  region          = var.aws_region
  instance_type   = var.build_instance_type
  source_ami      = var.base_ami_id
  ssh_username    = "ubuntu"

  run_tags = {
    Name      = "dragonwilds-packer-update"
    ManagedBy = "Packer"
  }

  tags = {
    Name          = "dragonwilds-ubuntu24-cis-l1-${local.build_timestamp}"
    Application   = "dragonwilds"
    BaseOS        = "ubuntu-24.04-minimal"
    Hardening     = "CIS-Level-1"
    ManagedBy     = "Packer"
    SourceVersion = "${local.build_timestamp}"
  }
}

build {
  name    = "dragonwilds-update"
  sources = ["source.amazon-ebs.dragonwilds_update"]

  provisioner "ansible" {
    playbook_file   = "${path.root}/../ansible/image.yml"
    user            = "ubuntu"
    extra_arguments = ["--become"]
    ansible_env_vars = [
      "ANSIBLE_HOST_KEY_CHECKING=False",
      "ANSIBLE_NOCOWS=1",
    ]
  }

  provisioner "ansible" {
    playbook_file   = "${path.root}/../ansible/finalize.yml"
    user            = "ubuntu"
    extra_arguments = ["--become"]
    ansible_env_vars = [
      "ANSIBLE_HOST_KEY_CHECKING=False",
      "ANSIBLE_NOCOWS=1",
    ]
  }

  post-processor "manifest" {
    output     = "${path.root}/manifest.json"
    strip_path = true
    custom_data = {
      region = var.aws_region
    }
  }
}
