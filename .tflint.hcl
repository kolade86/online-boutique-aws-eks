# tflint configuration — see .github/workflows/terraform.yml
#
# The AWS ruleset catches things `terraform validate` cannot: invalid instance
# types, deprecated arguments, and malformed ARNs.

config {
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.48.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}

# Every variable in this repo carries a description already; keep it that way.
rule "terraform_documented_variables" {
  enabled = true
}

rule "terraform_documented_outputs" {
  enabled = true
}
