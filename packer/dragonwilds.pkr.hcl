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

variable "root_volume_size" {
  type        = number
  description = "Root volume size for the resulting AMI in GiB"
  default     = 20
}

locals {
  build_timestamp = regex_replace(timestamp(), "[- TZ:]", "")
}

source "amazon-ebs" "dragonwilds" {
  ami_name        = "dragonwilds-ubuntu24-cis-l1-${local.build_timestamp}"
  ami_description = "Hardened Ubuntu 24.04 image for RuneScape Dragonwilds"
  region          = var.aws_region
  instance_type   = var.build_instance_type
  ssh_username    = "ubuntu"

  source_ami_filter {
    filters = {
      architecture        = "x86_64"
      name                = "ubuntu-minimal/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-minimal-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = ["099720109477"]
  }

  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    delete_on_termination = true
    encrypted             = true
    volume_size           = var.root_volume_size
    volume_type           = "gp3"
  }

  run_tags = {
    Name      = "dragonwilds-packer-build"
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
  name    = "dragonwilds"
  sources = ["source.amazon-ebs.dragonwilds"]

  provisioner "ansible" {
    playbook_file   = "${path.root}/../ansible/playbooks/bake.yml"
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
