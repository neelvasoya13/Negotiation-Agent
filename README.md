# Negotiation Chatbot

Construction materials price negotiation chatbot built with LangGraph (LangChain), FastAPI, and React.

## Setup

### 1. Backend (Python)

```bash
# Create virtual environment (optional but recommended)
python -m venv venv
# On Windows: venv\Scripts\activate
# On macOS/Linux: source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set environment variables in .env:
# GROQ_API_KEY=your_groq_api_key
# DATABASE_URL=postgresql://user:pass@localhost:5432/dbname
```

### 2. Frontend (React)

```bash
cd frontend
npm install
```

### 3. Database

Ensure PostgreSQL is running and the database has the required tables. Run `ensure_schema()` if needed (or use the schema from `db.py`).

## Running

### Start Backend

```bash
python api.py
# Or: uvicorn api:app --reload --host 0.0.0.0 --port 8000
```

Backend runs at http://localhost:8000

### Start Frontend

```bash
cd frontend
npm run dev
```

Frontend runs at http://localhost:3000

## Features

- **Login**: Email + password authentication (uses `builders` table)
- **Chat UI**: ChatGPT-style interface with user/AI bubbles
- **LangGraph workflow**: Interrupts at `User_input_1` and `User_input_2` to wait for user messages
- **builder_info**: Fetched at login and injected into state before graph runs
- **Conversation end**: When graph reaches END (deal_win, deal_lose, non_inquiry), input is disabled and "Start New Chat" button appears
