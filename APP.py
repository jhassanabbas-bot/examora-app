import os
import re
import json
import time
import random
import io
import csv
import secrets
import hashlib
from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus

import streamlit as st
from pypdf import PdfReader
from dotenv import load_dotenv
from openai import OpenAI
from openai import AuthenticationError, RateLimitError, BadRequestError


# ============================================================
# Examora (Beta) — Streamlit MVP
# - Local login/registration (hashed passwords)
# - Usage dashboard (user + admin)
# - PDF -> Summary + Exam (one question at a time)
# - Beta limiter (sessions per email)
# - Feedback button (Google Form) + local feedback fallback log
# - Study type selector (3 buttons)
# - Book mode: CUSTOM PAGE RANGE per chapter (NO autodetect in Beta)
# - NEW: Forgot password reset (admin reset code for Beta)
# ============================================================

# --- MUST be first Streamlit call ---
st.set_page_config(page_title="Examora (Beta)", layout="wide")
# -----------------------------
# Google Analytics (GA4)
# -----------------------------
GA_MEASUREMENT_ID = "G-GGZNKBCS1E"

st.markdown(
    f"""
    <!-- Google tag (gtag.js) -->
    <script async src="https://www.googletagmanager.com/gtag/js?id={GA_MEASUREMENT_ID}"></script>
    <script>
      window.dataLayer = window.dataLayer || [];
      function gtag(){{dataLayer.push(arguments);}}
      gtag('js', new Date());
      gtag('config', '{GA_MEASUREMENT_ID}');
    </script>
    """,
    unsafe_allow_html=True,
)
# --- Your Google Form link (base) ---
FEEDBACK_URL = "https://docs.google.com/forms/d/e/1FAIpQLSc1n_uvwsnr1NpXiY_1SCg5_t_6MnsWVgG54z2NZHgVJOrkVw/viewform?usp=header"


# -----------------------------
# Env + OpenAI client
# -----------------------------
load_dotenv(dotenv_path=r".\.env", override=True)

API_KEY = os.getenv("OPENAI_API_KEY") or ""
BETA_LIMIT = int(os.getenv("EXAMORA_BETA_LIMIT") or "20")

ADMIN_EMAIL = (os.getenv("EXAMORA_ADMIN_EMAIL") or "").strip().lower()
ADMIN_PASSWORD = os.getenv("EXAMORA_ADMIN_PASSWORD") or ""

# NEW (Beta): simple reset code (admin shares manually)
RESET_CODE = (os.getenv("EXAMORA_RESET_CODE") or "").strip()

client = OpenAI()  # reads OPENAI_API_KEY from environment


# -----------------------------
# Local data store
# -----------------------------
DATA_DIR = Path(".examora")
DATA_DIR.mkdir(exist_ok=True)

USAGE_FILE = DATA_DIR / "beta_usage.json"
USERS_FILE = DATA_DIR / "users.json"
FEEDBACK_LOG_FILE = DATA_DIR / "feedback_log.json"   # NEW: local fallback log


# -----------------------------
# Utilities
# -----------------------------
def now_ts() -> int:
    return int(time.time())


def ts_to_str(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def looks_like_email(email: str) -> bool:
    email = normalize_email(email)
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


def build_prefilled_feedback_url(base_url: str, user_email: str) -> str:
    """
    OPTIONAL: Prefill an email field if your Google Form supports prefill query params.
    This will NOT work unless you create a prefilled link in Google Forms and use the correct entry.<id>.
    """
    EMAIL_ENTRY_ID = os.getenv("EXAMORA_FEEDBACK_EMAIL_ENTRY_ID", "").strip()  # e.g., "1234567890"

    if not user_email or not looks_like_email(user_email):
        return base_url
    if not EMAIL_ENTRY_ID:
        return base_url

    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}usp=pp_url&entry.{EMAIL_ENTRY_ID}={quote_plus(user_email)}"


# -----------------------------
# Safe file IO
# -----------------------------
def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# -----------------------------
# Feedback fallback log (NEW)
# -----------------------------
def load_feedback_log() -> list:
    data = _read_json(FEEDBACK_LOG_FILE, [])
    return data if isinstance(data, list) else []


def append_feedback_log(item: dict) -> None:
    log = load_feedback_log()
    log.append(item)
    log = log[-300:]
    _write_json(FEEDBACK_LOG_FILE, log)


# -----------------------------
# Usage store (beta sessions, exam history)
# -----------------------------
def load_usage() -> dict:
    return _read_json(USAGE_FILE, {})


def save_usage(data: dict) -> None:
    _write_json(USAGE_FILE, data)


def get_user_usage(email: str) -> dict:
    email = normalize_email(email)
    usage = load_usage()
    return usage.get(email, {"exam_sessions_used": 0, "created_at": None, "last_used_at": None, "history": []})


def increment_exam_session(email: str, meta: dict) -> int:
    email = normalize_email(email)
    usage = load_usage()

    if email not in usage:
        usage[email] = {"exam_sessions_used": 0, "created_at": now_ts(), "last_used_at": None, "history": []}

    usage[email]["exam_sessions_used"] = int(usage[email].get("exam_sessions_used", 0)) + 1
    usage[email]["last_used_at"] = now_ts()

    hist = usage[email].get("history", [])
    hist.append(meta)
    usage[email]["history"] = hist[-50:]

    save_usage(usage)
    return int(usage[email]["exam_sessions_used"])


def usage_to_csv_bytes(usage: dict) -> bytes:
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["email", "exam_sessions_used", "created_at", "last_used_at", "history_count"])
    for email, row in usage.items():
        w.writerow([
            email,
            row.get("exam_sessions_used", 0),
            ts_to_str(row.get("created_at") or 0),
            ts_to_str(row.get("last_used_at") or 0),
            len(row.get("history", []) or []),
        ])
    return output.getvalue().encode("utf-8")


# -----------------------------
# Local users store (login)
# -----------------------------
PBKDF2_ITERS = 150_000


def pbkdf2_hash(password: str, salt_hex: str) -> str:
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return dk.hex()


def new_salt_hex() -> str:
    return secrets.token_bytes(16).hex()


def load_users() -> dict:
    return _read_json(USERS_FILE, {})


def save_users(users: dict) -> None:
    _write_json(USERS_FILE, users)


def ensure_admin_user_exists():
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        return
    users = load_users()
    if ADMIN_EMAIL in users:
        return
    salt = new_salt_hex()
    users[ADMIN_EMAIL] = {
        "salt": salt,
        "pwd_hash": pbkdf2_hash(ADMIN_PASSWORD, salt),
        "created_at": now_ts(),
        "last_login_at": None,
        "role": "admin",
    }
    save_users(users)


def register_user(email: str, password: str) -> tuple[bool, str]:
    email = normalize_email(email)
    if not looks_like_email(email):
        return False, "Please enter a valid email."
    if len(password) < 8:
        return False, "Password must be at least 8 characters."

    users = load_users()
    if email in users:
        return False, "This email is already registered. Please log in."

    salt = new_salt_hex()
    users[email] = {
        "salt": salt,
        "pwd_hash": pbkdf2_hash(password, salt),
        "created_at": now_ts(),
        "last_login_at": None,
        "role": "user",
    }
    save_users(users)
    return True, "Account created. You can log in now."


def verify_login(email: str, password: str) -> tuple[bool, str]:
    email = normalize_email(email)
    users = load_users()
    row = users.get(email)
    if not row:
        return False, "No account found for this email. Please register."

    salt = row.get("salt", "")
    expected = row.get("pwd_hash", "")
    got = pbkdf2_hash(password, salt)

    if got != expected:
        return False, "Incorrect password."

    row["last_login_at"] = now_ts()
    users[email] = row
    save_users(users)
    return True, "Logged in."


def reset_password_with_code(email: str, reset_code: str, new_password: str) -> tuple[bool, str]:
    email = normalize_email(email)

    if not looks_like_email(email):
        return False, "Please enter a valid email."
    if not RESET_CODE:
        return False, "Password reset is not configured. Ask admin to set EXAMORA_RESET_CODE in .env."
    if (reset_code or "").strip() != RESET_CODE:
        return False, "Invalid reset code."
    if len(new_password) < 8:
        return False, "New password must be at least 8 characters."

    users = load_users()
    if email not in users:
        return False, "No account found for this email. Please register."

    salt = new_salt_hex()
    users[email]["salt"] = salt
    users[email]["pwd_hash"] = pbkdf2_hash(new_password, salt)
    users[email]["pwd_reset_at"] = now_ts()
    save_users(users)
    return True, "Password reset successful. You can log in now."


def get_role(email: str) -> str:
    email = normalize_email(email)
    users = load_users()
    return (users.get(email, {}).get("role") or "user").lower()


# -----------------------------
# OpenAI guardrails + safe call
# -----------------------------
def require_api_key():
    if not API_KEY:
        st.error("OPENAI_API_KEY not found. Ensure .env exists and contains OPENAI_API_KEY=sk-...")
        st.stop()


def safe_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except AuthenticationError:
        st.error("Authentication failed (invalid API key). Re-check your .env OPENAI_API_KEY value.")
    except RateLimitError as e:
        st.error(f"Rate limit / quota issue: {e}")
    except BadRequestError as e:
        st.error(f"Bad request: {e}")
    except Exception as e:
        st.error(f"Unexpected error: {e}")
    return None


# -----------------------------
# PDF extraction
# -----------------------------
def _seek0(file_obj):
    try:
        file_obj.seek(0)
    except Exception:
        pass


def extract_text_from_pdf_pages(file, start_page: int, end_page: int) -> tuple[str, int, int, int]:
    _seek0(file)
    reader = PdfReader(file)
    n_pages = len(reader.pages)

    start = max(1, min(int(start_page), n_pages))
    end = max(1, min(int(end_page), n_pages))
    if end < start:
        start, end = end, start

    texts = []
    for p in range(start - 1, end):
        texts.append(reader.pages[p].extract_text() or "")
    return "\n".join(texts).strip(), n_pages, start, end


def chunk_text(text: str, max_chars: int = 12000) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    return [text[i: i + max_chars] for i in range(0, len(text), max_chars)]


# -----------------------------
# JSON parsing for MCQs
# -----------------------------
def _extract_json(text: str) -> dict:
    if not text:
        raise ValueError("Empty model output.")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        raise ValueError("Could not locate JSON in model output.")
    return json.loads(m.group(0))


def _difficulty_hint(level: str) -> str:
    if level == "Easy":
        return "Easy: foundational definitions, basic recall, simple conceptual checks."
    if level == "Hard":
        return "Hard: multi-step reasoning, subtle distinctions, application questions."
    return "Medium: conceptual understanding and typical exam-style reasoning."


# -----------------------------
# OpenAI calls
# -----------------------------
def summarize_text(text: str) -> str:
    chunks = chunk_text(text, max_chars=12000)
    if not chunks:
        return "No text available to summarize."

    input_text = "\n\n".join(chunks[:3])

    resp = client.responses.create(
        model="gpt-5",
        reasoning={"effort": "low"},
        instructions=(
            "You are Examora, an expert study tutor.\n\n"
            "Create a comprehensive, clean study summary with these sections:\n"
            "## Key Concepts\n"
            "- 8–14 bullets\n\n"
            "## Key Terms\n"
            "- 5–10 terms with 1–2 line definitions\n\n"
            "## Common Confusions\n"
            "- 3–6 items\n\n"
            "## Exam Takeaways\n"
            "- 5 bullets\n\n"
            "Rules:\n"
            "- Be faithful to the material.\n"
            "- Do not invent details.\n"
            "- If something is unclear, explicitly say it is unclear.\n"
        ),
        input=input_text,
    )
    return resp.output_text.strip()


def generate_mcqs_json(text: str, n_questions: int, difficulty: str) -> dict:
    chunks = chunk_text(text, max_chars=12000)
    if not chunks:
        return {"questions": []}

    input_text = "\n\n".join(chunks[:3])
    diff_hint = _difficulty_hint(difficulty)

    instructions = (
        "You are Examora, an expert exam question writer.\n"
        "Create multiple-choice questions strictly from the provided material.\n\n"
        f"Difficulty guidance: {diff_hint}\n\n"
        "Return ONLY valid JSON with this schema:\n"
        "{\n"
        '  "questions": [\n'
        "    {\n"
        '      "q": "question text",\n'
        '      "options": {"A":"...", "B":"...", "C":"...", "D":"..."},\n'
        '      "answer": "A|B|C|D",\n'
        '      "explanation": "brief explanation grounded in the material"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        f"Generate exactly {n_questions} questions if possible.\n"
        "Rules:\n"
        "- Use ONLY the provided material.\n"
        "- No hallucinations.\n"
        "- Keep explanations concise and tied to the text.\n"
    )

    resp = client.responses.create(
        model="gpt-5",
        reasoning={"effort": "low"},
        instructions=instructions,
        input=input_text,
    )

    data = _extract_json(resp.output_text)
    qs = data.get("questions", [])

    clean = []
    for item in qs:
        if not isinstance(item, dict):
            continue
        qtext = (item.get("q") or "").strip()
        opts = item.get("options") or {}
        ans = item.get("answer")
        exp = (item.get("explanation") or "").strip()

        if (
            qtext
            and isinstance(opts, dict)
            and all(k in opts for k in ["A", "B", "C", "D"])
            and ans in ["A", "B", "C", "D"]
        ):
            clean.append(
                {
                    "q": qtext,
                    "options": {k: str(opts[k]).strip() for k in ["A", "B", "C", "D"]},
                    "answer": ans,
                    "explanation": exp,
                }
            )
    return {"questions": clean}


def shuffle_question_options(q: dict) -> dict:
    labels = ["A", "B", "C", "D"]
    opts = q["options"]
    correct_label = q["answer"]
    correct_text = opts[correct_label]

    option_texts = [opts[l] for l in labels]
    random.shuffle(option_texts)

    new_opts = {labels[i]: option_texts[i] for i in range(4)}
    new_correct = next((k for k, v in new_opts.items() if v == correct_text), None)

    if not new_correct:
        return q
    q2 = dict(q)
    q2["options"] = new_opts
    q2["answer"] = new_correct
    return q2


def build_results_csv(questions: list, answers: dict) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Question#", "Question", "YourAnswer", "CorrectAnswer", "Correct?", "Explanation"])
    for i, q in enumerate(questions, start=1):
        user = answers.get(i)
        correct = q["answer"]
        ok = "YES" if user == correct else "NO"
        writer.writerow([i, q["q"], user or "", correct, ok, q.get("explanation", "")])
    return output.getvalue().encode("utf-8")


# -----------------------------
# Session state
# -----------------------------
def init_state():
    st.session_state.setdefault("auth_email", "")
    st.session_state.setdefault("is_authed", False)

    st.session_state.setdefault("summary_open", False)
    st.session_state.setdefault("summary_text", "")

    st.session_state.setdefault("exam_open", False)
    st.session_state.setdefault("questions", [])
    st.session_state.setdefault("answers", {})
    st.session_state.setdefault("q_index", 0)
    st.session_state.setdefault("flagged", set())
    st.session_state.setdefault("submitted", False)

    # study mode
    st.session_state.setdefault("study_mode", "Single Document / TG Reports etc. (Default)")

    # book navigation
    st.session_state.setdefault("book_start", 1)
    st.session_state.setdefault("book_end", 10)
    st.session_state.setdefault("book_title", "Chapter 1")
    st.session_state.setdefault("book_step", 10)  # how many pages to jump by Prev/Next

    # manual list mode
    st.session_state.setdefault("manual_chapters_text", "")
    st.session_state.setdefault("manual_section_idx", 0)

    # quick split mode
    st.session_state.setdefault("quick_section_idx", 0)


def reset_exam_state():
    st.session_state.exam_open = False
    st.session_state.questions = []
    st.session_state.answers = {}
    st.session_state.q_index = 0
    st.session_state.flagged = set()
    st.session_state.submitted = False
    for k in list(st.session_state.keys()):
        if str(k).startswith("choice_"):
            del st.session_state[k]


init_state()
ensure_admin_user_exists()


# -----------------------------
# Styling
# -----------------------------
st.markdown(
    """
<style>
.block-container { padding-top: 1.1rem; padding-bottom: 3rem; }
h1, h2, h3 { letter-spacing: -0.2px; }
.examora-card h1, .examora-card h2, .examora-card h3 { color: #1e4f91 !important; }

.examora-card {
  border: 1px solid rgba(49, 130, 206, 0.25);
  background: rgba(255,255,255,0.98);
  border-radius: 18px;
  padding: 18px 18px 14px 18px;
  box-shadow: 0 18px 50px rgba(0,0,0,0.12);
}
.examora-title { font-weight: 900; color: #1e4f91; font-size: 22px; margin: 0 0 2px 0; }
.examora-subtitle { color: rgba(0,0,0,0.55); margin: 0 0 14px 0; font-size: 13px; }

.examora-exam {
  border: 1px solid rgba(0,0,0,0.12);
  background: rgba(245,245,245,0.96);
  border-radius: 18px;
  padding: 18px;
  box-shadow: 0 18px 50px rgba(0,0,0,0.12);
}
div.stButton > button { border-radius: 12px !important; font-weight: 800 !important; }
</style>
""",
    unsafe_allow_html=True,
)


# -----------------------------
# Sidebar: Login / Register / Dashboard
# -----------------------------
st.sidebar.markdown("## Examora (Beta)")

# DEBUG STAMP: proves which file is running + last modified
try:
    this_file = os.path.abspath(__file__)
    mtime = datetime.fromtimestamp(os.path.getmtime(this_file)).strftime("%Y-%m-%d %H:%M:%S")
    st.sidebar.caption(f"Running: `{this_file}`")
    st.sidebar.caption(f"Last saved: {mtime}")
except Exception:
    st.sidebar.caption("Running file stamp unavailable (packaged environment).")

auth_tab = st.sidebar.radio("Account", ["Login", "Register", "Dashboard"], index=0)

if auth_tab == "Login":
    email = st.sidebar.text_input("Email", value=st.session_state.auth_email, placeholder="name@example.com")
    password = st.sidebar.text_input("Password", type="password")

    if st.sidebar.button("Log in"):
        email = normalize_email(email)
        ok, msg = verify_login(email, password)
        if ok:
            st.session_state.is_authed = True
            st.session_state.auth_email = email
            st.sidebar.success(msg)
            st.rerun()
        else:
            st.sidebar.error(msg)

    with st.sidebar.expander("Forgot password?"):
        st.caption("Beta reset uses an admin-provided reset code.")
        fp_email = st.text_input("Account email", key="fp_email", placeholder="name@example.com")
        fp_code = st.text_input("Reset code", key="fp_code", type="password")
        fp_new1 = st.text_input("New password (min 8 chars)", key="fp_new1", type="password")
        fp_new2 = st.text_input("Confirm new password", key="fp_new2", type="password")

        if st.button("Reset password"):
            if fp_new1 != fp_new2:
                st.error("Passwords do not match.")
            else:
                ok, msg = reset_password_with_code(fp_email, fp_code, fp_new1)
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

        if not RESET_CODE:
            st.warning("Admin: set EXAMORA_RESET_CODE in .env to enable resets.")

    feedback_link = build_prefilled_feedback_url(FEEDBACK_URL, normalize_email(email))
    st.sidebar.link_button("Submit Beta Feedback", feedback_link)

elif auth_tab == "Register":
    email = st.sidebar.text_input("Email", placeholder="name@example.com")
    password = st.sidebar.text_input("Password (min 8 chars)", type="password")
    password2 = st.sidebar.text_input("Confirm password", type="password")
    if st.sidebar.button("Create account"):
        email = normalize_email(email)
        if password != password2:
            st.sidebar.error("Passwords do not match.")
        else:
            ok, msg = register_user(email, password)
            if ok:
                st.sidebar.success(msg)
            else:
                st.sidebar.error(msg)

    feedback_link = build_prefilled_feedback_url(FEEDBACK_URL, normalize_email(email))
    st.sidebar.link_button("Submit Beta Feedback", feedback_link)

elif auth_tab == "Dashboard":
    if not st.session_state.is_authed:
        st.sidebar.warning("Please log in to view your dashboard.")
    else:
        email = st.session_state.auth_email
        role = get_role(email)
        usage = get_user_usage(email)

        st.sidebar.markdown(f"**Logged in as:** {email}")
        st.sidebar.markdown(f"**Role:** {role}")
        st.sidebar.markdown(f"**Exam sessions used:** {usage.get('exam_sessions_used', 0)}/{BETA_LIMIT}")

        if st.sidebar.button("Log out"):
            st.session_state.is_authed = False
            st.session_state.auth_email = ""
            reset_exam_state()
            st.session_state.summary_open = False
            st.session_state.summary_text = ""
            st.rerun()

        feedback_link = build_prefilled_feedback_url(FEEDBACK_URL, email)
        st.sidebar.link_button("Submit Beta Feedback", feedback_link)

        if role == "admin":
            st.sidebar.divider()
            st.sidebar.markdown("### Admin Tools")
            all_usage = load_usage()
            st.sidebar.download_button(
                "Download Usage CSV",
                data=usage_to_csv_bytes(all_usage),
                file_name="examora_beta_usage.csv",
                mime="text/csv",
            )

            st.sidebar.markdown("### Feedback fallback log (local)")
            fb = load_feedback_log()
            st.sidebar.caption(f"Local feedback entries: {len(fb)}")
            if fb:
                for row in fb[-5:][::-1]:
                    st.sidebar.write(f"- {ts_to_str(row.get('ts',0))} | {row.get('email','')} | {row.get('note','')[:40]}")

# -----------------------------
# Main header
# -----------------------------
st.markdown("## Examora (Beta)")
st.caption("Turn documents into mastery — summary → exam → results")

if not st.session_state.is_authed:
    st.info("Please **Login** (sidebar) to use Examora.")
    st.stop()

user_email = st.session_state.auth_email
# After login gate:
user_email = st.session_state.auth_email
role = get_role(user_email)

# ✅ Add Logout button (always available)
st.sidebar.divider()
if st.sidebar.button("Log out"):
    st.session_state.is_authed = False
    st.session_state.auth_email = ""
    reset_exam_state()
    st.session_state.summary_open = False
    st.session_state.summary_text = ""
    st.rerun()

role = get_role(user_email)

# Dashboard page (main area)
if auth_tab == "Dashboard":
    st.markdown("### Your Usage")
    u = get_user_usage(user_email)
    st.write(f"**Email:** {user_email}")
    st.write(f"**Exam sessions used:** {u.get('exam_sessions_used', 0)}/{BETA_LIMIT}")
    st.write(f"**First seen:** {ts_to_str(u.get('created_at') or 0) if u.get('created_at') else '—'}")
    st.write(f"**Last used:** {ts_to_str(u.get('last_used_at') or 0) if u.get('last_used_at') else '—'}")

    hist = u.get("history", []) or []
    if hist:
        st.markdown("#### Recent Exam Sessions (last 10)")
        for item in list(hist)[-10:][::-1]:
            st.write(
                f"- {ts_to_str(item.get('ts'))} | pages {item.get('pages')} | "
                f"questions {item.get('n_questions')} | diff {item.get('difficulty')}"
            )
    else:
        st.info("No exam history yet.")
    st.stop()


# -----------------------------
# Upload + Study Type
# -----------------------------
st.divider()
uploaded = st.file_uploader("Upload a text-based PDF", type=["pdf"])
if not uploaded:
    st.info("Upload a PDF to begin.")
    st.stop()

_seek0(uploaded)
reader_for_count = PdfReader(uploaded)
total_pages = len(reader_for_count.pages)
pdf_name = getattr(uploaded, "name", "PDF")
st.caption(f"PDF: {pdf_name} • Pages detected: {total_pages}")

st.markdown("### What are you studying?")
study_mode = st.radio(
    label="",
    options=[
        "Single Document / TG Reports etc. (Default)",
        "Book (Chapter-by-chapter / Enter chapter pages)",
        "Book (Autodetect) — Not available in Beta",
    ],
    index=0,
    horizontal=True,
    key="study_mode",
)

if study_mode.startswith("Book (Autodetect)"):
    st.info("🚧 **Book (Autodetect)** is **not available in Beta**. Use **Book (Chapter-by-chapter)**.")
    is_book_mode = True
else:
    is_book_mode = study_mode.startswith("Book")


# -----------------------------
# BOOK MODE (NO autodetect): choose chapter pages directly
# -----------------------------
start_page, end_page = 1, min(5, total_pages)
section_label = ""
section_title = ""
section_num = None
total_sections = 0

if is_book_mode:
    st.session_state.scope_mode = "Book (chapter-by-chapter)"
    st.markdown("### Book Mode")
    st.caption("Pick a **page range for one chapter**. Examora reads **only that range**.")

    chapter_source = st.radio(
        "Chapter selection",
        [
            "Custom page range (this chapter)",
            "Manual chapters (enter page ranges)",
            "Quick split (every N pages)",
        ],
        horizontal=True,
        index=0,
        key="book_chapter_source",
    )

    # --- Custom page range (the main thing you asked for) ---
    if chapter_source.startswith("Custom"):
        c1, c2, c3 = st.columns([1, 1, 2])
        with c1:
            start_page = st.number_input(
                "Start page",
                min_value=1,
                max_value=total_pages,
                value=int(st.session_state.book_start),
                step=1,
                key="book_start_input",
            )
        with c2:
            end_page = st.number_input(
                "End page",
                min_value=1,
                max_value=total_pages,
                value=int(st.session_state.book_end),
                step=1,
                key="book_end_input",
            )
        with c3:
            section_title = st.text_input(
                "Chapter name (optional)",
                value=st.session_state.book_title,
                key="book_title_input",
            )

        # normalize + persist
        a = int(start_page)
        b = int(end_page)
        if b < a:
            a, b = b, a
        a = max(1, min(a, total_pages))
        b = max(1, min(b, total_pages))

        st.session_state.book_start = a
        st.session_state.book_end = b
        st.session_state.book_title = (section_title or "").strip() or "Selected Chapter"
        section_title = st.session_state.book_title
        section_label = f" — {section_title}"

        # page step for next/prev
        step = int(b - a + 1)
        st.session_state.book_step = max(1, step)

        pprev, pnext = st.columns([1, 1])
        with pprev:
            if st.button("◀ Previous range", key="book_prev_range"):
                step = int(st.session_state.book_step)
                new_a = max(1, a - step)
                new_b = max(1, b - step)
                if new_b < new_a:
                    new_b = new_a
                st.session_state.book_start = new_a
                st.session_state.book_end = new_b
                reset_exam_state()
                st.session_state.summary_open = False
                st.session_state.summary_text = ""
                st.rerun()
        with pnext:
            if st.button("Next range ▶", key="book_next_range"):
                step = int(st.session_state.book_step)
                new_a = min(total_pages, a + step)
                new_b = min(total_pages, b + step)
                if new_b < new_a:
                    new_b = new_a
                st.session_state.book_start = new_a
                st.session_state.book_end = new_b
                reset_exam_state()
                st.session_state.summary_open = False
                st.session_state.summary_text = ""
                st.rerun()

        start_page, end_page = a, b
        section_num = 1
        total_sections = 1

    # --- Manual chapters list ---
    elif chapter_source.startswith("Manual"):
        st.warning("Define chapters using page ranges. Works for any book PDF.")

        default_example = (
            "Chapter 1: 1-18\n"
            "Chapter 2: 19-42\n"
            "Chapter 3: 43-70\n"
        )

        raw = st.text_area(
            "Enter chapters (one per line) as: Chapter Name: start-end",
            value=st.session_state.manual_chapters_text or default_example,
            height=140,
        )
        st.session_state.manual_chapters_text = raw

        manual_sections = []
        for line in (raw or "").splitlines():
            line = line.strip()
            if not line or ":" not in line or "-" not in line:
                continue
            name, rng = line.split(":", 1)
            name = name.strip()
            rng = rng.strip()
            m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", rng)
            if not m:
                continue
            a, b = int(m.group(1)), int(m.group(2))
            a = max(1, min(a, total_pages))
            b = max(1, min(b, total_pages))
            if b < a:
                a, b = b, a
            manual_sections.append({"title": name, "start": a, "end": b})

        if not manual_sections:
            st.error("No valid chapters parsed yet. Use format: Chapter Name: start-end")
            st.stop()

        total_sections = len(manual_sections)
        labels = [f"{i+1}. {s['title']} (pp. {s['start']}-{s['end']})" for i, s in enumerate(manual_sections)]

        idx = st.selectbox(
            "Choose chapter",
            list(range(len(labels))),
            index=min(st.session_state.manual_section_idx, len(labels) - 1),
            format_func=lambda i: labels[i],
        )
        st.session_state.manual_section_idx = int(idx)

        start_page = manual_sections[idx]["start"]
        end_page = manual_sections[idx]["end"]
        section_title = manual_sections[idx]["title"]
        section_num = idx + 1
        section_label = f" — {section_title}"

        cprev, cnext = st.columns([1, 1])
        with cprev:
            if st.button("◀ Previous chapter", disabled=(idx <= 0), key="book_prev_manual"):
                st.session_state.manual_section_idx = max(0, idx - 1)
                reset_exam_state()
                st.session_state.summary_open = False
                st.session_state.summary_text = ""
                st.rerun()
        with cnext:
            if st.button("Next chapter ▶", disabled=(idx >= len(manual_sections) - 1), key="book_next_manual"):
                st.session_state.manual_section_idx = min(len(manual_sections) - 1, idx + 1)
                reset_exam_state()
                st.session_state.summary_open = False
                st.session_state.summary_text = ""
                st.rerun()

    # --- Quick split ---
    else:
        st.info("Quick split is a fast fallback if you don’t want to type chapter ranges.")
        pages_per = st.number_input("Pages per chapter", min_value=5, max_value=50, value=20, step=5)

        quick_sections = []
        start = 1
        ch = 1
        while start <= total_pages:
            end = min(total_pages, start + int(pages_per) - 1)
            quick_sections.append({"title": f"Chapter {ch} (pages {start}-{end})", "start": start, "end": end})
            start = end + 1
            ch += 1

        total_sections = len(quick_sections)
        labels = [f"{i+1}. {s['title']}" for i, s in enumerate(quick_sections)]

        idx = st.selectbox(
            "Choose chapter chunk",
            list(range(len(labels))),
            index=min(st.session_state.quick_section_idx, len(labels) - 1),
            format_func=lambda i: labels[i],
        )
        st.session_state.quick_section_idx = int(idx)

        start_page = quick_sections[idx]["start"]
        end_page = quick_sections[idx]["end"]
        section_title = quick_sections[idx]["title"]
        section_num = idx + 1
        section_label = f" — {section_title}"

        cprev, cnext = st.columns([1, 1])
        with cprev:
            if st.button("◀ Previous", disabled=(idx <= 0), key="book_prev_quick"):
                st.session_state.quick_section_idx = max(0, idx - 1)
                reset_exam_state()
                st.session_state.summary_open = False
                st.session_state.summary_text = ""
                st.rerun()
        with cnext:
            if st.button("Next ▶", disabled=(idx >= len(quick_sections) - 1), key="book_next_quick"):
                st.session_state.quick_section_idx = min(len(quick_sections) - 1, idx + 1)
                reset_exam_state()
                st.session_state.summary_open = False
                st.session_state.summary_text = ""
                st.rerun()

    with st.spinner("Reading selected chapter..."):
        text, _, sp, ep = extract_text_from_pdf_pages(uploaded, int(start_page), int(end_page))

    if len(text) < 500:
        st.error("I couldn’t extract enough text from this range. Try a different range or a text-based PDF.")
        st.stop()

    st.success(f"✅ Ready: **{section_title or 'Selected Chapter'}** (pages {sp}–{ep}).")
    st.caption("Generate Summary/Exam for this chapter range. Then move to the next range/chapter.")
    st.info("📌 Examora has **NOT** read the full book. It reads **one chapter/range at a time** in Book Mode.")

# -----------------------------
# SINGLE DOCUMENT MODE
# -----------------------------
else:
    st.session_state.scope_mode = "Single Document/TG Reports etc."
    st.markdown("### Single Document / Report Mode")
    st.caption("Best for TG reports, guidelines, papers, and short documents.")

    scope = st.radio("Reading scope", ["Selected pages", "Whole document"], horizontal=True)

    if scope == "Whole document":
        start_page, end_page = 1, total_pages
        st.info("Whole document selected. Examora will read the entire document.")
    else:
        colA, colB = st.columns(2)
        with colA:
            start_page = st.number_input("Start page", min_value=1, max_value=max(1, total_pages), value=1, step=1)
        with colB:
            end_page = st.number_input("End page", min_value=1, max_value=max(1, total_pages), value=min(5, total_pages), step=1)

    with st.spinner("Reading your selected material..."):
        text, _, sp, ep = extract_text_from_pdf_pages(uploaded, int(start_page), int(end_page))

    if len(text) < 500:
        st.error(
            "I couldn’t extract enough text. This PDF may be scanned/protected, or the page range is too small.\n\n"
            "Try a text-based PDF or increase page range."
        )
        st.stop()

    if scope == "Whole document":
        st.success(f"✅ I’ve read the **entire document**: {pdf_name} (pages 1–{total_pages}).")
    else:
        st.success(f"✅ I’ve read a selected range from {pdf_name} (pages {sp}–{ep}).")

st.caption("Tip: choose a focused chapter/section for best question quality.")

# -----------------------------
# Summary
# -----------------------------
a1, a2, a3 = st.columns([1, 1, 1])
with a1:
    if st.button("Generate Summary"):
        require_api_key()
        with st.spinner("Generating Examora summary..."):
            summary = safe_call(summarize_text, text)
        if summary:
            st.session_state.summary_text = summary
            st.session_state.summary_open = True
            st.rerun()

with a2:
    st.write("")

with a3:
    feedback_link = build_prefilled_feedback_url(FEEDBACK_URL, user_email)
    st.link_button("Submit Beta Feedback", feedback_link)

with st.expander("Having issues with the Google feedback form? Leave feedback here (fallback)."):
    st.caption("This stores feedback locally on the server in .examora/feedback_log.json")
    fb_note = st.text_area("Your feedback", height=100, key="fb_note")
    if st.button("Save feedback (local)"):
        if not (fb_note or "").strip():
            st.warning("Please type feedback first.")
        else:
            append_feedback_log({
                "ts": now_ts(),
                "email": user_email,
                "note": fb_note.strip()
            })
            st.success("Saved locally. Thank you!")

if st.session_state.summary_open:
    st.markdown(
        f"""
<div class="examora-card">
  <div class="examora-title">Examora Study Summary</div>
  <div class="examora-subtitle">Generated from pages {sp}–{ep}{section_label}</div>
</div>
""",
        unsafe_allow_html=True,
    )

    st.markdown(
        f"<div class='examora-card'>{st.session_state.summary_text}</div>",
        unsafe_allow_html=True
    )

    if st.button("Close Summary"):
        st.session_state.summary_open = False
        st.rerun()

st.divider()
# -----------------------------
# Exam controls
# -----------------------------
st.markdown("### Examora Exam Mode")
st.caption("One question at a time • Flag • Jump via grid • Submit for results")

left, right = st.columns([2, 1])
with left:
    n_questions = st.slider("Number of questions", min_value=5, max_value=25, value=10, step=5)
    difficulty = st.selectbox("Difficulty", ["Easy", "Medium", "Hard"], index=1)
    shuffle_opts = st.checkbox("Shuffle answer options", value=True)
with right:
    mode = st.selectbox("Mode", ["Exam Mode (feedback on submit)", "Study Mode (instant feedback)"], index=0)

g1, g2, g3 = st.columns([1, 1, 2])
with g1:
    if st.button("Generate Exam"):
        require_api_key()

        used = get_user_usage(user_email).get("exam_sessions_used", 0)
        if used >= BETA_LIMIT:
            st.error(
                f"Beta limit reached: {used}/{BETA_LIMIT} exam sessions used.\n\n"
                "Thanks for testing Examora. Please send feedback and contact us for full access."
            )
            st.stop()

        reset_exam_state()

        with st.spinner("Generating your Examora exam..."):
            mcq_set = safe_call(
                generate_mcqs_json,
                text,
                n_questions=n_questions,
                difficulty=difficulty,
            )

        if mcq_set and mcq_set.get("questions"):
            questions = mcq_set["questions"]
            if shuffle_opts:
                questions = [shuffle_question_options(q) for q in questions]

            st.session_state.questions = questions
            st.session_state.exam_open = True

            meta = {
                "ts": now_ts(),
                "pages": f"{sp}-{ep}",
                "n_questions": n_questions,
                "difficulty": difficulty,
                "scope": st.session_state.scope_mode,
                "section_title": (section_label.strip(" —") if section_label else ""),
                "pdf_name": getattr(uploaded, "name", ""),
            }

            new_used = increment_exam_session(user_email, meta)
            
with g2:
    if st.button("Close Exam"):
        st.session_state.exam_open = False
        st.rerun()

with g3:
    feedback_link = build_prefilled_feedback_url(FEEDBACK_URL, user_email)
    st.link_button("Submit Beta Feedback", feedback_link)

# -----------------------------
# Exam panel
# -----------------------------
if not st.session_state.exam_open or not st.session_state.questions:
    st.info("Click **Generate Exam** to start.")
    st.stop()

questions = st.session_state.questions
total = len(questions)
study_mode_now = mode.startswith("Study Mode")

st.markdown(
    f"""
<div class="examora-exam">
  <div class="examora-title">Examora Exam</div>
  <div class="examora-subtitle">Questions: {total} • Difficulty: {difficulty} • Pages: {sp}–{ep}{section_label}</div>
</div>
""",
    unsafe_allow_html=True,
)

st.markdown("**Question Grid** (click to jump):")
cols = st.columns(10)
for i in range(total):
    col = cols[i % 10]
    qnum = i + 1
    answered = st.session_state.answers.get(qnum) in ["A", "B", "C", "D"]
    flagged = qnum in st.session_state.flagged

    label = f"{qnum}"
    if flagged:
        label += " ⚑"
    if answered:
        label += " ✓"

    if col.button(label, key=f"grid_{qnum}", disabled=st.session_state.submitted):
        st.session_state.q_index = i
        st.rerun()

st.write("")

idx = st.session_state.q_index
q = questions[idx]
qnum = idx + 1
opts = q["options"]

st.markdown(f"### Q{qnum}. {q['q']}")

prev = st.session_state.answers.get(qnum)
index = ["A", "B", "C", "D"].index(prev) if prev in ["A", "B", "C", "D"] else None

choice = st.radio(
    "Select an answer",
    ["A", "B", "C", "D"],
    format_func=lambda k: f"{k}) {opts[k]}",
    index=index,
    key=f"choice_{qnum}",
    disabled=st.session_state.submitted,
    label_visibility="collapsed",
)

st.session_state.answers[qnum] = choice

c1, c2, c3, c4 = st.columns([1, 1, 1, 2])

with c1:
    if st.button("Flag ⚑", disabled=st.session_state.submitted):
        if qnum in st.session_state.flagged:
            st.session_state.flagged.remove(qnum)
        else:
            st.session_state.flagged.add(qnum)
        st.rerun()

with c2:
    if st.button("Previous", disabled=st.session_state.submitted or idx == 0):
        st.session_state.q_index = max(0, idx - 1)
        st.rerun()

with c3:
    if st.button("Next", disabled=st.session_state.submitted or idx == total - 1):
        st.session_state.q_index = min(total - 1, idx + 1)
        st.rerun()

with c4:
    submit_clicked = st.button("Submit Exam", disabled=st.session_state.submitted)

if study_mode_now and not st.session_state.submitted and choice in ["A", "B", "C", "D"]:
    if choice == q["answer"]:
        st.success("Correct")
    else:
        st.error(f"Incorrect (Correct: {q['answer']})")
    if q.get("explanation"):
        st.caption(f"Explanation: {q['explanation']}")

if submit_clicked:
    st.session_state.submitted = True
    st.rerun()

if st.session_state.submitted:
    st.divider()
    st.markdown(
        """
<div class="examora-card">
  <div class="examora-title">Examora Results</div>
  <div class="examora-subtitle">Review your answers and explanations</div>
</div>
""",
        unsafe_allow_html=True,
    )

    correct = 0
    answered = 0
    for i, qq in enumerate(questions, start=1):
        user_ans = st.session_state.answers.get(i)
        correct_ans = qq["answer"]
        if user_ans in ["A", "B", "C", "D"]:
            answered += 1
        if user_ans == correct_ans:
            correct += 1

    st.metric("Score", f"{correct}/{total}")
    st.write(f"Answered: {answered}/{total}")

    st.markdown("#### Review")
    for i, qq in enumerate(questions, start=1):
        user_ans = st.session_state.answers.get(i)
        correct_ans = qq["answer"]

        if user_ans == correct_ans:
            st.success(f"Q{i}: Correct ({user_ans})")
        else:
            st.error(f"Q{i}: Incorrect (You: {user_ans or 'No answer'} | Correct: {correct_ans})")

        if qq.get("explanation"):
            st.caption(f"Explanation: {qq['explanation']}")
        st.write("")

    csv_bytes = build_results_csv(questions, st.session_state.answers)
    st.download_button(
        "Download Results (CSV)",
        data=csv_bytes,
        file_name="examora_results.csv",
        mime="text/csv",
    )

    st.divider()
    st.markdown("### Help us improve Examora")
    st.write(
        "You are part of our beta group. Please report incorrect answers, unclear questions, "
        "or UI issues. Your feedback directly shapes the product."
    )
    feedback_link = build_prefilled_feedback_url(FEEDBACK_URL, user_email)
    st.link_button("Submit Beta Feedback", feedback_link)

    if st.button("Start New Exam"):
        reset_exam_state()
        st.rerun()



















