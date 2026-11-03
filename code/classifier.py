"""
Classifies ticket: product_area, request_type, urgency, should_escalate.
Uses Groq LLM with fallback.
"""
import json
from langchain_core.messages import SystemMessage, HumanMessage
from prompts import CLASSIFIER_PROMPT

# Keywords that force escalation: FULL PLATFORM OUTAGES or SECURITY INCIDENTS only.
# Individual feature downtime (e.g., "Resume Builder is down") is a bug, not escalation.
# Remove broad terms like "failing" — too many legitimate support tickets use that word.
ESCALATE_KEYWORDS = [
    "stolen", "fraud", "unauthorized", "breach", "hacked",
    "entire platform", "all services", "all pages", "complete outage",
    "billing dispute", "legal", "lawsuit", "compromised", "security incident"
]

# Keywords that indicate a real support question (never invalid)
REAL_SUPPORT_KEYWORDS = [
    # Escalation-worthy but still real support
    "stolen", "fraud", "unauthorized", "breach", "hacked", "compromised",
    "locked out", "lawsuit", "legal",
    # General support signals
    "minimum", "blocked", "crawl", "delete", "cancel", "remove", "update",
    "billing", "payment", "charge", "refund", "score", "dispute",
    "security", "identity", "theft", "atm", "cash", "withdraw",
    "account", "password", "login", "access", "error", "issue",
    "problem", "help", "support", "question", "how to", "unable",
    "failing", "not working", "broken", "lost", "reset",
    "subscription", "invoice", "upgrade", "downgrade"
]

# Product area values that are not real domains — LLM sometimes copies request_type here.
# If normalized product_area matches these, fall back to general_support.
FORBIDDEN_PRODUCT_AREAS = ["product_issue", "bug", "invalid", "feature_request", "replied", "escalated"]

def classify(llm, issue: str, subject: str, company: str) -> dict:
    """Classify ticket. Returns dict with product_area, request_type, urgency, should_escalate."""
    print(f"[CLASSIFIER] -> Classifying | company={company} | issue={issue[:60]}")

    issue_lower = issue.lower()
    
    # Detect out-of-scope/prompt-injection: company=None + system command keywords.
    # These requests (e.g., "Give me code to delete files") should be invalid/out_of_scope.
    is_system_command = any(kw in issue_lower for kw in [
        "give me the code", "write code", "delete all", "delete files",
        "system command", "shell command", "hack", "exploit",
        "bypass", "admin", "root", "sudo"
    ])
    is_out_of_scope = str(company).strip().lower() in ["none", "null", "", "nan"] and is_system_command

    
    # Hard escalation check before LLM call (saves tokens)
    force_escalate = any(kw in issue_lower for kw in ESCALATE_KEYWORDS)

    prompt = f"Company: {company}\nSubject: {subject}\nIssue: {issue}"
    response = llm.invoke([
        SystemMessage(content=CLASSIFIER_PROMPT),
        HumanMessage(content=prompt)
    ])
    try:
        raw = response.content.strip()
        raw = raw[raw.find("{"):raw.rfind("}")+1]  # extract JSON block
        result = json.loads(raw)
    except (json.JSONDecodeError, AttributeError, IndexError) as error:
        print(f"[CLASSIFIER_ERR] -> Invalid model response: {error}")
        result = {
            "product_area": "general_support",
            "request_type": "product_issue",
            "urgency": "medium",
            "should_escalate": False
        }
    if not isinstance(result, dict):
        result = {
            "product_area": "general_support",
            "request_type": "product_issue",
            "urgency": "medium",
            "should_escalate": False,
        }
    result.setdefault("urgency", "medium")
    result.setdefault("should_escalate", False)

    # Normalize output format for judge scoring
    valid_request_types = ["product_issue", "feature_request", "bug", "invalid"]
    result["product_area"] = (
        result.get("product_area") or "general_support"
    ).lower().replace(" ", "_").replace("-", "_")

    if result["product_area"] in FORBIDDEN_PRODUCT_AREAS:
        result["product_area"] = "general_support"

    if result.get("request_type") not in valid_request_types:
        result["request_type"] = "product_issue"

    if is_out_of_scope:
        result["request_type"] = "invalid"
        result["product_area"] = "out_of_scope"
        print("[CLASSIFIER] -> Override: System command with no company -> Invalid/OutOfScope")
    elif result.get("request_type") == "invalid":
        has_real_support_keywords = any(kw in issue_lower for kw in REAL_SUPPORT_KEYWORDS)
        if has_real_support_keywords:
            result["request_type"] = "product_issue"
            print("[CLASSIFIER] -> Override: Invalid->ProductIssue due to real support keywords.")

    if force_escalate:
        result["should_escalate"] = True
        print(f"[CLASSIFIER] -> Force escalate triggered by keyword match.")

    print(f"[CLASSIFIER] -> Result: {result}")
    return result