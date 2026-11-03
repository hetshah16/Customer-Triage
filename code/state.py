"""
TypedDict for LangGraph state in support triage agent.
Tracks ticket data through classify -> retrieve -> respond pipeline.
"""
from typing import TypedDict, List, Dict, Any


class SupportState(TypedDict):
    """State for the support triage agent."""
    ticket_id: int
    issue: str
    subject: str
    company: str
    classification: Dict[str, Any]  # product_area, request_type, urgency, should_escalate
    retrieved_chunks: List[str]
    response: Dict[str, Any]  # status, response, justification, request_type
    collection: Any  # ChromaDB collection
    llm: Any  # LangChain chat model, passed through graph to avoid re-init