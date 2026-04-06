"""
Langfuse Tracing Setup — centralized tracing configuration for the QA reviewer.

Langfuse gives us full observability over every LLM call:
- What prompt was sent (input)
- What the model returned (output)
- Latency, token usage, cost
- Organized into traces (per filing) and spans (per verification layer)

Required environment variables:
    LANGFUSE_SECRET_KEY  — your Langfuse secret key
    LANGFUSE_PUBLIC_KEY  — your Langfuse public key
    LANGFUSE_HOST        — Langfuse server URL (defaults to cloud: https://cloud.langfuse.com)

Langfuse v4 API:
    - start_observation(name, as_type="span"|"generation", input, output, metadata, model)
    - Observations have .end() method to close them
    - Use as_type="generation" for LLM calls, "span" for grouping
"""

import os
import logging
from langfuse import Langfuse

logger = logging.getLogger(__name__)

# Module-level singleton — created once, reused everywhere
_langfuse_client: Langfuse = None


def get_langfuse() -> Langfuse:
    """
    Get or create the Langfuse client singleton.

    Reads credentials from environment variables. If they're not set,
    returns None (tracing is optional — the pipeline still works without it).
    """
    global _langfuse_client

    if _langfuse_client is not None:
        return _langfuse_client

    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")

    if not secret_key or not public_key:
        logger.warning(
            "Langfuse keys not set (LANGFUSE_SECRET_KEY / LANGFUSE_PUBLIC_KEY). "
            "Tracing disabled."
        )
        return None

    _langfuse_client = Langfuse(
        secret_key=secret_key,
        public_key=public_key,
        host=host,
    )
    logger.info(f"Langfuse tracing enabled (host={host})")
    return _langfuse_client


def create_trace(name: str, input_data: dict = None, metadata: dict = None):
    """
    Create a new trace/span using Langfuse v4 API.
    Returns the observation object or None if tracing is disabled.
    """
    lf = get_langfuse()
    if lf is None:
        return None
    
    try:
        return lf.start_observation(
            name=name,
            as_type="span",
            input=input_data,
            metadata=metadata,
        )
    except Exception as e:
        logger.debug(f"Failed to create trace: {e}")
        return None


def create_generation(name: str, model: str, input_data: str, metadata: dict = None):
    """
    Create a new generation (LLM call) using Langfuse v4 API.
    Returns the observation object or None if tracing is disabled.
    """
    lf = get_langfuse()
    if lf is None:
        return None
    
    try:
        return lf.start_observation(
            name=name,
            as_type="generation",
            model=model,
            input=input_data,
            metadata=metadata,
        )
    except Exception as e:
        logger.debug(f"Failed to create generation: {e}")
        return None


def flush_langfuse():
    """
    Flush any pending Langfuse events before the process exits.
    Langfuse batches events — this ensures nothing is lost.
    """
    if _langfuse_client is not None:
        _langfuse_client.flush()
        logger.info("Langfuse events flushed.")
