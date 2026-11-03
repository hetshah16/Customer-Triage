"""
Generates final user-facing response or escalation message.
Grounded strictly in corpus chunks retrieved by retriever.py.

Input:  llm, issue str, company str, corpus_chunks list, should_escalate bool, request_type str
Output: dict with keys: status, response, justification
"""
import json
import re
from langchain_core.messages import SystemMessage, HumanMessage
from prompts import RESPONDER_PROMPT

# Standard escalation message — never changes, ensures consistency
ESCALATION_RESPONSE = (
    "This issue requires immediate attention from our support team. "
    "A human agent will contact you shortly."
)

def respond(
    llm,
    issue: str,
    company: str,
    corpus_chunks: list,
    should_escalate: bool,
    request_type: str = "product_issue"
) -> dict:
    """
    Generate user-facing response or escalation message.

    Args:
        llm: LangChain chat model (Groq or Gemini fallback)
        issue: raw ticket issue text
        company: normalized company name (HackerRank, Claude, Visa, None)
        corpus_chunks: list of relevant text chunks from RAG retriever
        should_escalate: bool set by classifier or keyword match
        request_type: classifier output — skips LLM if "invalid"

    Returns:
        dict: {status, response, justification}
    """
    print(f"[RESPONDER] -> Generating response | escalate={should_escalate} | type={request_type}")

    # --- GATE 1: High-risk tickets → immediate escalation, no LLM needed ---
    if should_escalate:
        print("[RESPONDER] -> Escalating ticket.")
        return {
            "status": "escalated",
            "response": ESCALATION_RESPONSE,
            "justification": "Ticket flagged as high-risk or sensitive. Routed to human agent."
        }

    # --- GATE 2: Invalid/out-of-scope tickets → no LLM, safe static response ---
    if request_type == "invalid":
        print("[RESPONDER] -> Invalid request. Skipping LLM.")
        return {
            "status": "replied",
            "response": "This is outside our supported topics. Please contact the relevant support team directly.",
            "justification": "Request classified as invalid or out-of-scope. No corpus lookup performed."
        }

    # --- GATE 3: No corpus chunks → out of scope, no hallucination risk ---
    if not corpus_chunks:
        print("[RESPONDER] -> No corpus chunks found. Returning out-of-scope.")
        return {
            "status": "replied",
            "response": "This is outside our supported topics. Please contact the relevant support team directly.",
            "justification": "No relevant corpus found. Issue is out of scope for this support channel."
        }

    # --- LLM CALL: grounded response using corpus chunks ---
    corpus_text = "\n---\n".join(corpus_chunks)
    prompt = RESPONDER_PROMPT.format(corpus=corpus_text, company=company, issue=issue)

    # Default fallback if LLM fails or returns unparseable output
    result = {
        "status": "replied",
        "response": "This is outside our supported topics. Please contact the relevant support team directly.",
        "justification": "LLM response could not be parsed. Safe fallback used."
    }

    response = llm.invoke([
        SystemMessage(content="You are a grounded support agent. Return JSON only. No preamble."),
        HumanMessage(content=prompt)
    ])

    raw = response.content.strip()

    # Robust JSON extraction — handles LLM preamble/postamble and markdown fences
    json_match = re.search(r'\{.*\}', raw, re.DOTALL)
    if json_match:
        # Fix invalid backslash escapes only — preserve valid JSON escapes (\n \t \\ etc.)
        raw_json = re.sub(r'\\(?!["\\/bfnrtu])', r'', json_match.group(0))
        try:
            result = json.loads(raw_json)
        except json.JSONDecodeError as parse_err:
            print(f"[RESPONDER_ERR] -> JSON parse failed: {parse_err}")
            result = {
                "status": "replied",
                "response": "This is outside our supported topics. Please contact the relevant support team directly.",
                "justification": "JSON parse error in LLM response. Safe fallback used."
            }

        # Enforce allowed status values per problem_statement.md
        if result.get("status") not in ["replied", "escalated"]:
            result["status"] = "replied"

        result["response"] = result.get("response", "").strip()
        result["justification"] = result.get("justification", "").strip()

    elif len(raw) > 20:
        # LLM returned plain text instead of JSON — use directly as response
        result["response"] = raw
        result["justification"] = "LLM returned plain text instead of JSON."

    print(f"[RESPONDER] -> Status: {result.get('status')}")
    return result