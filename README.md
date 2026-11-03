# Support Triage Agent

A support-ticket triage application for **HackerRank, Claude, and Visa**. It classifies customer requests, searches a company-specific knowledge corpus, and suggests a grounded response or routes sensitive cases for human review. The web app stores tickets and team workflow in a local SQLite database.

## What goes in and what comes out

**Web app input:** company (optional) and customer message (required, up to 4,000 characters). The signed-in user is recorded as the ticket creator.

**Web app result:** ticket ID, triage status, product area, request type, urgency, escalation flag, suggested response, and justification. The ticket is saved before processing, so failed requests remain visible for follow-up.

**CSV batch input:** `support_tickets/support_tickets.csv`, with columns `Issue`, `Subject`, and `Company`.

**CSV batch output:** `support_tickets/output.csv`, with columns `status`, `product_area`, `response`, `justification`, and `request_type`. The batch runner writes this CSV; web tickets are stored separately in SQLite.

## Processing pipeline

```text
Customer message + company
        ↓
Classify with Groq LLM
  product area · request type · urgency · escalation
        ↓
Retrieve up to 3 relevant Markdown chunks from ChromaDB
        ↓
Safety gates
  escalate sensitive cases; do not generate for invalid or unmatched queries
        ↓
Generate a response grounded in retrieved support documentation
        ↓
Save ticket, result, and workflow activity
```

For model-specific failures, the app retries once with a different Groq model. If the retry also fails, web triage marks the saved ticket as failed instead of presenting it as successful.

## Technologies

- **Python** for the application and processing pipeline
- **LangGraph** for the classify → retrieve → respond workflow
- **Groq via LangChain** for classification and response generation
- **ChromaDB** for in-memory semantic search over Markdown files in `data/`
- **SQLite** for web users, tickets, assignments, and activity history
- **HTTP server + HTML/CSS/JavaScript** for the local dashboard
- **Pandas** for CSV batch processing

## Dashboard features and roles

The dashboard displays total, triaged, escalated, and unresolved ticket counts, with per-company totals. Team members can search/filter ticket history, inspect query and response details, assign work, and move tickets through **open**, **in progress**, and **resolved**.

| Role | Main permissions |
|---|---|
| Admin | Manage accounts and roles; view and update tickets; assign agents |
| Supervisor | View/update tickets and assign agents |
| Agent | Submit triage and update unassigned or assigned tickets |
| Viewer | Read-only dashboard and ticket history |

Roles are checked by the server API, not only hidden in the interface.

## Run locally

Install dependencies:

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```env
GROQ_API_KEY=your_groq_api_key
TRIAGE_ADMIN_USERNAME=admin
TRIAGE_ADMIN_PASSWORD=replace-with-a-unique-password-of-at-least-12-characters
GROQ_FALLBACK_MODEL=openai/gpt-oss-120b
```

The primary model is `openai/gpt-oss-20b`; the fallback defaults to `openai/gpt-oss-120b`. Both require access through the same Groq API key. Choose a fallback model enabled for your account.

Start the dashboard:

```powershell
python code/web.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The first run creates the initial admin account from the environment variables above. SQLite data is stored at `support_tickets/triage.db` by default; `TRIAGE_DATABASE` can override the path. The local sign-in session expires after eight hours.

To process the example CSV instead:

```powershell
python code/main.py
```

## Project layout

```text
code/       Classifier, retriever, LangGraph nodes, responder, web app, and storage
code/web/   Dashboard HTML, CSS, and JavaScript
code/tests/ API, role, persistence, and fallback tests
data/       Markdown knowledge base organized by company
support_tickets/  Example/input CSV and batch output
```

Run the tests:

```powershell
python -m unittest discover -s code/tests -v
```

The included HTTP server is intended for local development. Do not expose it directly to the public internet; remote deployment should use HTTPS and a production-ready server.
