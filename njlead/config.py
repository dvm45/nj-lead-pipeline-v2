"""
Central configuration for the LLM extraction feature.

All tunable settings live here and are read from environment variables, so the
API key and model choice stay OUT of the code. An optional .env file in the
project root is loaded automatically (via python-dotenv) if present.

Nothing in this module fails when the key is missing. That only matters at the
moment an LLM call is actually made (see llm_extractor.py), which means you can
wire the whole feature up now and drop the key in later.
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


# The Anthropic API key. None until you set it (shell export or .env file).
ANTHROPIC_API_KEY: str | None = os.environ.get("ANTHROPIC_API_KEY")

# Which model to use. Sonnet is the recommended default - strongest accuracy on
# messy and handwritten scans, which are the hardest reports in this dataset.
LLM_MODEL: str = os.environ.get("NJLEAD_LLM_MODEL", "claude-sonnet-4-6")

# Ceiling on the model's response size (tokens).
LLM_MAX_TOKENS: int = int(os.environ.get("NJLEAD_LLM_MAX_TOKENS", "4096"))

# Rows the model reports below this confidence are sent to human review
# instead of the database. Range 0.0 - 1.0.
CONFIDENCE_THRESHOLD: float = float(os.environ.get("NJLEAD_CONFIDENCE_THRESHOLD", "0.70"))

# Resolution (DPI) for rendering a scanned page to an image for the model.
SCAN_RENDER_DPI: int = int(os.environ.get("NJLEAD_SCAN_DPI", "150"))


def api_key_is_set() -> bool:
    """True if an Anthropic API key is available in the environment."""
    return bool(ANTHROPIC_API_KEY)
