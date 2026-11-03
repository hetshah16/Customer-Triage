"""Primary and fallback Groq model factories."""
import os
from langchain_groq import ChatGroq
from langchain_core.language_models import BaseChatModel


def get_model(tools: list = []) -> BaseChatModel:
    """Return the primary Groq model."""
    print("[MODEL_INIT] -> Loading Groq (openai/gpt-oss-20b) as primary.")
    return ChatGroq(
        model="openai/gpt-oss-20b",
        temperature=0,
        api_key=os.environ["GROQ_API_KEY"],
        request_timeout=20  # Timeout for API calls
    ).bind_tools(tools)


def get_fallback_model(tools: list = []) -> BaseChatModel:
    """Return a separate Groq model used after primary-model failures."""
    model = os.environ.get("GROQ_FALLBACK_MODEL", "openai/gpt-oss-120b").strip()
    model = model or "openai/gpt-oss-120b"
    if model == "openai/gpt-oss-20b":
        raise ValueError("GROQ_FALLBACK_MODEL must differ from the primary model.")
    print(f"[MODEL_INIT] -> Loading Groq ({model}) as fallback.")
    return ChatGroq(
        model=model,
        temperature=0,
        api_key=os.environ["GROQ_API_KEY"],
        request_timeout=20
    ).bind_tools(tools)
