import os

# Anthropic model IDs — kept for local (Ollama) backends
MODEL_ADVISOR = "claude-sonnet-4-6"
MODEL_COMPACT = "claude-haiku-4-5-20251001"

# Bedrock model IDs
BEDROCK_ADVISOR = "qwen.qwen3-235b-a22b-2507-v1:0"
BEDROCK_COMPACT = "qwen.qwen3-235b-a22b-2507-v1:0"

# Local Ollama config
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "qwen3:14b")

# AWS Bedrock config
AWS_PROFILE = os.getenv("AWS_PROFILE", "bedrock-dev")
AWS_REGION  = os.getenv("AWS_REGION",  "ap-southeast-2")

# Bedrock pricing — $/M tokens. Verify at aws.amazon.com/bedrock/pricing
BEDROCK_INPUT_PRICE_PER_M  = 0.60
BEDROCK_OUTPUT_PRICE_PER_M = 0.60
