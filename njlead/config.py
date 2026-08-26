"""
Central configuration for the LLM extraction feature.

All tunable settings live here and are read from environment variables, so
credentials and model choice stay OUT of the code. An optional .env file in
the project root is loaded automatically (via python-dotenv) if present.

Two providers are supported for calling Claude:

  - "anthropic"  -> Anthropic's own API, authed with ANTHROPIC_API_KEY
  - "bedrock"    -> AWS Bedrock, authed with your normal AWS credentials
                    (env vars, ~/.aws/credentials, or an EC2 instance role)

Set NJLEAD_LLM_PROVIDER in your .env to pick one. Default is "bedrock",
matching the current project deployment.

Nothing in this module fails when credentials are missing. That only matters
at the moment an LLM call is actually made (see llm_extractor.py), so you can
wire everything up first and add credentials later.
"""

from __future__ import annotations

import os
from pathlib import Path

# Load a .env file from the current working directory, if one exists.
# If python-dotenv isn't installed yet, we silently skip it - real environment
# variables still work, you just won't get .env auto-loading.
try:
    from dotenv import load_dotenv

    load_dotenv(Path.cwd() / ".env")
except Exception:
    pass


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------

# Which backend to route Claude calls through: "bedrock" or "anthropic".
# Lower-cased for easy comparison downstream.
LLM_PROVIDER: str = os.environ.get("NJLEAD_LLM_PROVIDER", "bedrock").strip().lower()


# ---------------------------------------------------------------------------
# Anthropic-direct settings (only used when LLM_PROVIDER == "anthropic")
# ---------------------------------------------------------------------------

# The Anthropic API key. None until you set it (shell export or .env file).
ANTHROPIC_API_KEY: str | None = os.environ.get("ANTHROPIC_API_KEY")

# Model ID for Anthropic's direct API (their naming, not AWS's).
ANTHROPIC_MODEL: str = os.environ.get("NJLEAD_ANTHROPIC_MODEL", "claude-sonnet-4-5")


# ---------------------------------------------------------------------------
# Bedrock settings (only used when LLM_PROVIDER == "bedrock")
# ---------------------------------------------------------------------------

# AWS region where you've been granted Bedrock model access. Sonnet 4.5 is
# widely available in us-east-1, us-east-2, and us-west-2.
AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-2")

# Bedrock's ID for Claude Sonnet 4.5 (which Anthropic calls Sonnet 4.6 on
# their direct API - AWS keeps the original release name).
#
# The "us." prefix means "US inference profile" - Bedrock routes the call
# across US regions for capacity. Using the raw model ID without a prefix
# will fail in most regions with "on-demand throughput isn't supported for
# this model in this region." If you ever need a different region prefix
# (eg "eu."), override this via NJLEAD_BEDROCK_MODEL_ID in .env.
BEDROCK_MODEL_ID: str = os.environ.get(
    "NJLEAD_BEDROCK_MODEL_ID",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
)


# ---------------------------------------------------------------------------
# Provider-neutral model settings
# ---------------------------------------------------------------------------

# Ceiling on the model's response size (tokens). 16384 is Sonnet 4.5's
# maximum for a synchronous (non-streaming) call - going higher forces the
# SDK to require streaming, which the pipeline doesn't currently do.
#
# The schema is tuned (short source_text, tight per-row fields) so that even
# 100+ fixture reports fit comfortably under this cap. If a report ever does
# get truncated (stop_reason=max_tokens), the model returns an empty
# measurements list rather than a partial one - the [llm] status line in the
# ingest output will flag it as TRUNCATED so the file can be reviewed.
LLM_MAX_TOKENS: int = int(os.environ.get("NJLEAD_LLM_MAX_TOKENS", "16384"))

# Rows the model reports below this confidence are sent to human review
# instead of the database. Range 0.0 - 1.0.
CONFIDENCE_THRESHOLD: float = float(os.environ.get("NJLEAD_CONFIDENCE_THRESHOLD", "0.70"))

# Resolution (DPI) for rendering a scanned page to an image for the model.
SCAN_RENDER_DPI: int = int(os.environ.get("NJLEAD_SCAN_DPI", "150"))


# ---------------------------------------------------------------------------
# Compatibility shim
# ---------------------------------------------------------------------------

# Some older code and check-llm output refer to `LLM_MODEL` without knowing
# the provider. Point it at whichever model ID applies to the current provider.
LLM_MODEL: str = BEDROCK_MODEL_ID if LLM_PROVIDER == "bedrock" else ANTHROPIC_MODEL


# ---------------------------------------------------------------------------
# Credential checks (no network calls - just "did the user set the env vars")
# ---------------------------------------------------------------------------

def api_key_is_set() -> bool:
    """
    True if credentials for the *currently selected* provider are present.

    - For "anthropic": ANTHROPIC_API_KEY must be set.
    - For "bedrock":   boto3 will discover credentials in this order:
                       env vars (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY),
                       shared credential file (~/.aws/credentials), or an
                       EC2/ECS instance role. We check env vars and the
                       shared file here - the instance-role case can only be
                       verified by actually making a call.
    """
    if LLM_PROVIDER == "anthropic":
        return bool(ANTHROPIC_API_KEY)

    if LLM_PROVIDER == "bedrock":
        # Bedrock API key (bearer token). Simplest auth path - a single env
        # var that boto3 recognizes for Bedrock calls specifically.
        if os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
            return True
        # Standard IAM access-key pair via env vars
        if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
            return True
        # Shared credentials file (created by `aws configure`)
        shared = Path.home() / ".aws" / "credentials"
        if shared.exists():
            return True
        return False

    # Unknown provider string - treat as unconfigured.
    return False
