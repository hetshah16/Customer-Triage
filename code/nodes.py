"""
LangGraph nodes for support triage agent.
Each node wraps existing functions for the classify -> retrieve -> respond pipeline.
Nodes: classify -> retrieve -> respond (linear graph, no branching).

Input state keys:  ticket_id, issue, subject, company, collection, llm
Output state keys: classification, retrieved_chunks, response
"""
from typing import Dict, Any
from classifier import classify
from retriever import retrieve
from responder import respond
from models import get_model, get_fallback_model


def _get_llm(state: Dict[str, Any]):
    """Get LLM from state if available, else initialize primary."""
    return state.get("llm") or get_model([])


def _with_fallback(state, operation, node_name):
    llm = _get_llm(state)
    try:
        return operation(llm)
    except Exception as error:
        if state.get("fallback_active"):
            raise
        print(f"[{node_name}] -> Primary model failed ({error}); switching to fallback.")
        fallback = get_fallback_model([])
        state["llm"] = fallback
        state["fallback_active"] = True
        return operation(fallback)


def classify_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 1: Classify ticket via LLM.
    Sets: classification (product_area, request_type, urgency, should_escalate)
    """
    print(f"[NODE_CLASSIFY] -> ticket_id={state.get('ticket_id')}")
    classification = _with_fallback(
        state,
        lambda llm: classify(llm, state["issue"], state["subject"], state["company"]),
        "NODE_CLASSIFY",
    )
    state["classification"] = classification
    return state

def retrieve_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 2: Retrieve relevant corpus chunks from ChromaDB.
    Sets: retrieved_chunks (list of text strings)
    """
    print(f"[NODE_RETRIEVE] -> ticket_id={state.get('ticket_id')}")
    query = f"{state['subject']} {state['issue']}".strip()
    chunks = retrieve(state["collection"], query, state["company"], n=3)
    state["retrieved_chunks"] = chunks
    return state

def respond_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 3: Generate grounded user-facing response.
    Sets: response (status, response, justification)
    """
    print(f"[NODE_RESPOND] -> ticket_id={state.get('ticket_id')}")
    result = _with_fallback(
        state,
        lambda llm: respond(
            llm,
            state["issue"],
            state["company"],
            state["retrieved_chunks"],
            state["classification"]["should_escalate"],
            state["classification"]["request_type"],
        ),
        "NODE_RESPOND",
    )
    state["response"] = result
    return state