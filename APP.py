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
# - Feedback button (Google Form)
# ============================================================

# --- MUST be first Streamlit call ---
st.set_page_config(page_title="Examora (Beta)", layout="wide")

# --- Your Google Form link ---
FEEDBACK_URL = "https://docs.google.com/forms/d/e/1FAIpQLSc1n_uvwsnr1NpXiY_1SCg5_t_6MnsWVgG54z2NZHgVJOrkVw/viewform?usp=header"


# -----------------------------
# Env + OpenAI client
# -----------------------------
load_dotenv(dotenv_path=r".\.env", override=True)

API_KEY = os.getenv("OPENAI_API_KEY") or ""
BETA_LIMIT = int(os.getenv("EXAMORA_BETA_LIMIT") or "20")

ADMIN_EMAIL = (os.getenv("EXAMORA_ADMIN_EMAIL") or "").strip().lower()
ADMIN_PASSWORD = os.getenv("EXAMORA_ADMIN_PASSWORD") or ""

client = OpenAI()  # reads OPENAI_API_KEY from environment


# -----------------------------
# Local data store
# -----------------------------
DATA_DIR = Path(".examora")
DATA_DIR.mkdir(exist_ok=True)

USAGE_FILE = DATA_DIR / "beta_usage.json"
USERS_FILE = DATA_DIR / "users.json"


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

    # keep lightweight history (cap last 50)
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


def pbkdf2_hash(password: str, salt_b64: str) -> str:
    salt = bytes.fromhex(salt_b64)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return dk.hex()


def new_salt_hex() -> str:
    return secrets.token_bytes(16).hex()


def load_users() -> dict:
    return _read_json(USERS_FILE, {})


def save_users(users: dict) -> None:
    _write_json(USERS_FILE, users)


def ensure_admin_user_exists():
    # Optional: auto-create admin user if env vars are provided
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
def extract_text_from_pdf_pages(file, start_page: int, end_page: int) -> tuple[str, int, int, int]:
    reader = PdfReader(file)
    n_pages = len(reader.pages)

    start = max(1, min(start_page, n_pages))
    end = max(1, min(end_page, n_pages))
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
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]


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
    first_chunk = chunks[0]

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
        input=first_chunk,
    )
    return resp.output_text.strip()


def generate_mcqs_json(text: str, n_questions: int, difficulty: str) -> dict:
    chunks = chunk_text(text, max_chars=12000)
    if not chunks:
        return {"questions": []}
    first_chunk = chunks[0]
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
        input=first_chunk,
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

    st.sidebar.caption("Admin access appears automatically when your email matches the configured admin user.")
    st.sidebar.link_button("Submit Beta Feedback", FEEDBACK_URL)

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

    st.sidebar.link_button("Submit Beta Feedback", FEEDBACK_URL)

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

        st.sidebar.link_button("Submit Beta Feedback", FEEDBACK_URL)

        # Admin dashboard controls
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


# -----------------------------
# Main header
# -----------------------------
st.markdown("## Examora (Beta)")
st.caption("Turn documents into mastery — summary → exam → results")

if not st.session_state.is_authed:
    st.info("Please **Login** (sidebar) to use Examora.")
    st.stop()

user_email = st.session_state.auth_email
role = get_role(user_email)

# Admin usage dashboard (main area)
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

    if role == "admin":
        st.divider()
        st.markdown("### Admin Usage Dashboard")
        all_usage = load_usage()
        total_users = len(all_usage)
        total_sessions = sum(int(v.get("exam_sessions_used", 0)) for v in all_usage.values())

        c1, c2, c3 = st.columns(3)
        c1.metric("Total users", total_users)
        c2.metric("Total exam sessions", total_sessions)
        c3.metric("Beta session limit", BETA_LIMIT)

        # Top users
        top = sorted(
            [(email, int(v.get("exam_sessions_used", 0))) for email, v in all_usage.items()],
            key=lambda x: x[1],
            reverse=True,
        )[:15]

        st.markdown("#### Top Users (by exam sessions)")
        for e, n in top:
            st.write(f"- {e}: {n}")

        st.download_button(
            "Download Usage CSV",
            data=usage_to_csv_bytes(all_usage),
            file_name="examora_beta_usage.csv",
            mime="text/csv",
        )

    st.stop()


# -----------------------------
# Upload + page range
# -----------------------------
st.divider()
uploaded = st.file_uploader("Upload a text-based PDF", type=["pdf"])
if not uploaded:
    st.info("Upload a PDF to begin.")
    st.stop()

reader_for_count = PdfReader(uploaded)
total_pages = len(reader_for_count.pages)
st.caption(f"PDF pages detected: {total_pages}")

colA, colB = st.columns(2)
with colA:
    start_page = st.number_input("Start page", min_value=1, max_value=max(1, total_pages), value=1, step=1)
with colB:
    end_page = st.number_input("End page", min_value=1, max_value=max(1, total_pages), value=min(5, total_pages), step=1)

with st.spinner("Reading your material..."):
    text, _, sp, ep = extract_text_from_pdf_pages(uploaded, int(start_page), int(end_page))

if len(text) < 500:
    st.error(
        "I couldn’t extract enough text. This PDF may be scanned/protected, or the page range is too small.\n\n"
        "Try a text-based PDF or increase page range."
    )
    st.stop()

st.success(f"✅ I’ve read your material (pages {sp}–{ep}). What would you like to do next?")
st.caption("Tip: choose a focused chapter range for best question quality.")

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
    st.link_button("Submit Beta Feedback", FEEDBACK_URL)

if st.session_state.summary_open:
    st.markdown(
        f"""
<div class="examora-card">
  <div class="examora-title">Examora Study Summary</div>
  <div class="examora-subtitle">Generated from pages {sp}–{ep}</div>
</div>
""",
        unsafe_allow_html=True,
    )
    st.markdown(f"<div class='examora-card'>{st.session_state.summary_text}</div>", unsafe_allow_html=True)
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
            mcq_set = safe_call(generate_mcqs_json, text, n_questions=n_questions, difficulty=difficulty)

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
            }
            new_used = increment_exam_session(user_email, meta)
            st.success(f"Exam generated. Session used: {new_used}/{BETA_LIMIT}")
            st.rerun()
        else:
            st.warning("No questions returned. Try increasing page range or reducing difficulty.")

with g2:
    if st.button("Close Exam"):
        st.session_state.exam_open = False
        st.rerun()

with g3:
    st.link_button("Submit Beta Feedback", FEEDBACK_URL)

# -----------------------------
# Exam panel
# -----------------------------
if not st.session_state.exam_open or not st.session_state.questions:
    st.info("Click **Generate Exam** to start.")
    st.stop()

questions = st.session_state.questions
total = len(questions)
study_mode = mode.startswith("Study Mode")

st.markdown(
    f"""
<div class="examora-exam">
  <div class="examora-title">Examora Exam</div>
  <div class="examora-subtitle">Questions: {total} • Difficulty: {difficulty} • Pages: {sp}–{ep}</div>
</div>
""",
    unsafe_allow_html=True,
)

# Grid navigation
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

# Current question
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

# Controls
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

# Study mode instant feedback
if study_mode and not st.session_state.submitted and choice in ["A", "B", "C", "D"]:
    if choice == q["answer"]:
        st.success("Correct")
    else:
        st.error(f"Incorrect (Correct: {q['answer']})")
    if q.get("explanation"):
        st.caption(f"Explanation: {q['explanation']}")

if submit_clicked:
    st.session_state.submitted = True
    st.rerun()

# Results
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
    st.link_button("Submit Beta Feedback", FEEDBACK_URL)

    if st.button("Start New Exam"):
        reset_exam_state()
        st.rerun()
