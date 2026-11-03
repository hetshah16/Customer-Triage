"""
Entry point. Reads support_tickets.csv → runs triage agent → writes output.csv.
Deterministic: fixed model temperature=0.
"""
import os


os.environ["LANGCHAIN_TRACING_V2"] = "false"   # disable LangSmith tracing
os.environ["LANGCHAIN_HANDLER_V2"] = "false"
os.environ.pop("LANGCHAIN_API_KEY", None)
os.environ.pop("LANGCHAIN_PROJECT", None)

import pandas as pd
from dotenv import load_dotenv
load_dotenv()

from classifier import classify

from responder import respond
from retriever import build_retriever, normalize_company, retrieve
from models import get_model, get_fallback_model
from groq import RateLimitError
import httpx
import time
from langgraph.graph import StateGraph
from state import SupportState
from nodes import classify_node, retrieve_node, respond_node
# Paths (relative to repo root)
INPUT_CSV  = os.path.join(os.path.dirname(__file__), "..", "support_tickets", "support_tickets.csv")
OUTPUT_CSV = os.path.join(os.path.dirname(__file__), "..", "support_tickets", "output.csv")

# Ensure output directory exists
os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)

def get_llm():
    """Return primary LLM with fallback on failure."""
    try:
        return get_model([])  # no tools needed for this task
    except Exception:
        return get_fallback_model([])


def build_graph():
    """Build the LangGraph for support triage: classify -> retrieve -> respond."""
    graph = StateGraph(SupportState)
    graph.add_node("classify", classify_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("respond", respond_node)
    graph.add_edge("classify", "retrieve")
    graph.add_edge("retrieve", "respond")
    graph.set_entry_point("classify")
    return graph.compile()


def process_ticket(graph, collection, row, ticket_id, include_details=False) -> dict:
    """Run one ticket through the graph, optionally returning urgency and escalation details."""
    issue = str(row.get("Issue", "") or "")
    subject = str(row.get("Subject", "") or "")
    raw_company = row.get("Company", "")
    normalized_company = normalize_company(raw_company)
    company_prompt = normalized_company or "None"

    initial_state = {
        "ticket_id": ticket_id,
        "issue": issue,
        "subject": subject,
        "company": company_prompt,
        "classification": {},
        "retrieved_chunks": [],
        "response": {},
        "collection": collection,
        "llm": get_llm()
    }

    final_state = graph.invoke(initial_state)
    result = {
        "status": final_state["response"]["status"],
        "product_area": final_state["classification"]["product_area"],
        "response": final_state["response"]["response"],
        "justification": final_state["response"]["justification"],
        "request_type": final_state["classification"]["request_type"]
    }
    if include_details:
        result["urgency"] = final_state["classification"]["urgency"]
        result["should_escalate"] = final_state["classification"]["should_escalate"]
    return result

def main():
    """Main entry point: build index, process all tickets, write output.csv."""
    print("[MAIN] -> Starting support triage agent.")

    # Build corpus index once
    collection = build_retriever()

    # Build the LangGraph
    graph = build_graph()

    # Load tickets
    df = pd.read_csv(INPUT_CSV)
    print(f"[MAIN] -> Loaded {len(df)} tickets from {INPUT_CSV}")

    results = []
    for i, row in df.iterrows():
        print(f"\n[MAIN] -> Processing ticket {i+1}/{len(df)}")
        time.sleep(3)  # Rate limit buffer
        try:
            out = process_ticket(graph, collection, row, i)
        except (RateLimitError, httpx.TimeoutException) as e:
            print(f"[MAIN] -> Model switch triggered: {e}")
            # Fallback to sequential processing when graph fails due to rate limits
            llm = get_fallback_model([])
            # Fallback to sequential for now
            issue = str(row.get("Issue", "") or "")
            subject = str(row.get("Subject", "") or "")
            raw_company = row.get("Company", "")
            normalized_company = normalize_company(raw_company)
            company_prompt = normalized_company or "None"
            classification = classify(llm, issue, subject, company_prompt)
            query = f"{subject} {issue}".strip()
            chunks = retrieve(collection, query, normalized_company, n=3)
            result = respond(llm, issue, company_prompt, chunks, classification["should_escalate"], classification["request_type"])
            out = {
                "status": result["status"],
                "product_area": classification["product_area"],
                "response": result["response"],
                "justification": result["justification"],
                "request_type": classification["request_type"]
            }
        except Exception as e:
            print(f"[MAIN_ERR] -> Ticket {i} failed: {e}")
            out = {
                "status": "escalated",
                "product_area": "general_support",
                "response": "Unable to process this ticket. Escalating to human agent.",
                "justification": f"Processing error: {str(e)[:100]}",
                "request_type": "product_issue"
            }
        results.append(out)

    # Write output
    out_df = pd.DataFrame(results)
    out_df.to_csv(OUTPUT_CSV, index=False)
    print(f"\n[MAIN] -> Done. Output written to {OUTPUT_CSV}")
    print(out_df.head())

if __name__ == "__main__":
    main()