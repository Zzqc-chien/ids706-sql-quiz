"""SQL Quiz - Streamlit app.

Signed-out visitors can read the questions.
Signed-in users (Supabase Auth) can run SQL against the live database and save answers.

Run locally:  streamlit run app.py
"""

from __future__ import annotations

import pandas as pd
import psycopg2
import streamlit as st
from supabase import create_client

import quizlib

st.set_page_config(page_title="SQL Quiz", page_icon="🗄️", layout="wide")

QUESTIONS = quizlib.load_questions()
TOTAL_POINTS = sum(q["points"] for q in QUESTIONS)


# ---------------------------------------------------------------------------
# Authentication (Supabase Auth, email + password)
# ---------------------------------------------------------------------------
def supabase_client():
    # One client per browser session. Do NOT cache it with st.cache_resource:
    # a shared client would share one login between all visitors.
    if "sb" not in st.session_state:
        st.session_state.sb = create_client(
            quizlib.setting("SUPABASE_URL"), quizlib.setting("SUPABASE_KEY"))
    return st.session_state.sb


def current_user() -> dict | None:
    return st.session_state.get("user")


def _forget_answers() -> None:
    """Clear per-user state so one person's answers never show for another."""
    for key in list(st.session_state.keys()):
        if key.startswith("sql_") or key in ("results", "saved"):
            del st.session_state[key]


def sign_in(email: str, password: str) -> None:
    res = supabase_client().auth.sign_in_with_password({"email": email, "password": password})
    _forget_answers()
    st.session_state.user = {"id": res.user.id, "email": res.user.email}


def sign_up(email: str, password: str) -> str:
    res = supabase_client().auth.sign_up({"email": email, "password": password})
    if res.session is None:
        return "Account created. Check your email to confirm it, then sign in."
    _forget_answers()
    st.session_state.user = {"id": res.user.id, "email": res.user.email}
    return "Account created and signed in."


def sign_out() -> None:
    try:
        supabase_client().auth.sign_out()
    finally:
        _forget_answers()
        st.session_state.pop("user", None)


def auth_sidebar() -> None:
    with st.sidebar:
        st.header("Account")
        user = current_user()
        if user:
            st.success(f"Signed in as **{user['email']}**")
            if st.button("Sign out", width="stretch"):
                sign_out()
                st.rerun()
            return

        st.info("Sign in to run queries and save answers.")
        tab_in, tab_up = st.tabs(["Sign in", "Create account"])
        with tab_in, st.form("sign_in"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in", width="stretch"):
                try:
                    sign_in(email.strip(), password)
                    st.rerun()
                except Exception as exc:
                    st.error(f"Sign-in failed: {exc}")
        with tab_up, st.form("sign_up"):
            email = st.text_input("Email", key="up_email")
            password = st.text_input("Password (at least 6 characters)",
                                     type="password", key="up_pw")
            if st.form_submit_button("Create account", width="stretch"):
                try:
                    message = sign_up(email.strip(), password)
                    st.success(message)
                    if current_user():
                        st.rerun()
                except Exception as exc:
                    st.error(f"Sign-up failed: {exc}")


# ---------------------------------------------------------------------------
# Actions (each one checks sign-in again on the server before doing anything)
# ---------------------------------------------------------------------------
def safe_log(**kwargs) -> None:
    try:
        quizlib.log_event(**kwargs)
    except Exception as exc:  # the quiz should keep working even if logging fails
        st.warning(f"Could not write the audit log: {exc}")


def run_query(question: dict, sql: str) -> None:
    user = current_user()
    if not user:
        st.error("You must be signed in to run a query.")
        return
    try:
        columns, rows = quizlib.run_readonly(sql)
    except (ValueError, psycopg2.Error) as exc:
        message = str(exc).strip().splitlines()[0]
        if isinstance(exc, psycopg2.Error):
            message = f"Database error: {message}"
        st.session_state.results[question["id"]] = {"error": message}
        safe_log(user_id=user["id"], user_email=user["email"], action="run",
                 question_id=question["id"], sql_text=sql, success=False, error=message)
        return
    st.session_state.results[question["id"]] = {"columns": columns, "rows": rows}
    safe_log(user_id=user["id"], user_email=user["email"], action="run",
             question_id=question["id"], sql_text=sql, success=True, row_count=len(rows))


def save_answer(question: dict, sql: str) -> None:
    user = current_user()
    if not user:
        st.error("You must be signed in to save an answer.")
        return
    if not sql.strip():
        st.warning("Type a query before saving.")
        return
    try:
        quizlib.save_submission(user["id"], user["email"], question["id"], sql)
    except Exception as exc:
        st.error(f"Could not save: {exc}")
        return
    safe_log(user_id=user["id"], user_email=user["email"], action="save",
             question_id=question["id"], sql_text=sql, success=True)
    st.session_state.pop("saved", None)       # reload the saved list on the next run
    st.session_state.flash = f"Saved your answer to {question['id'].upper()}."
    st.rerun()


def saved_answers() -> dict:
    user = current_user()
    if not user:
        return {}
    if "saved" not in st.session_state:
        try:
            st.session_state.saved = quizlib.latest_submissions(user["id"])
        except Exception as exc:
            st.warning(f"Could not load saved answers: {exc}")
            st.session_state.saved = {}
    return st.session_state.saved


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
def schema_reference() -> None:
    with st.expander("📋 Database schema (tables you can query)"):
        st.markdown(
            """
| Table | Columns |
|---|---|
| `students` | `student_id`, `name`, `program`, `start_year` |
| `courses` | `course_id`, `course_code`, `title`, `department`, `credits` |
| `enrollments` | `enrollment_id`, `student_id` → students, `course_id` → courses, `term`, `grade_points` (NULL = in progress) |

Terms are `'Fall 2025'`, `'Spring 2026'` and `'Fall 2026'`. Only `SELECT` / `WITH` queries are allowed.
"""
        )


def show_result(question: dict) -> None:
    result = st.session_state.results.get(question["id"])
    if not result:
        return
    if "error" in result:
        st.error(result["error"])
        return
    rows = result["rows"]
    st.markdown(f"**Result** — {len(rows)} row(s)"
                + (f" (showing the first {quizlib.MAX_ROWS})" if len(rows) == quizlib.MAX_ROWS else ""))
    st.dataframe(pd.DataFrame(rows, columns=result["columns"]),
                 width="stretch", hide_index=True)
    expected = question["expected"]["rows"]
    if quizlib.results_match(rows, expected, question.get("ordered", False)):
        st.success("✅ Matches the expected result.")
    else:
        st.info("This does not match the expected result yet. Check the filters, joins and ordering.")


def question_panel(question: dict, signed_in: bool, saved: dict) -> None:
    qid = question["id"]
    st.subheader(f"{qid.upper()}. {question['title']}  ·  {question['points']} pts")
    st.markdown(question["prompt"])
    st.caption(f"Skills: {question['skills']}")

    key = f"sql_{qid}"
    if key not in st.session_state:
        st.session_state[key] = saved.get(qid, {}).get("sql", "")
    sql = st.text_area("Your SQL", key=key, height=180,
                       placeholder="SELECT ...", disabled=not signed_in)

    col_run, col_save, col_note = st.columns([1, 1, 4])
    if col_run.button("▶ Run", key=f"run_{qid}", type="primary", disabled=not signed_in):
        run_query(question, sql)
    if col_save.button("💾 Save", key=f"save_{qid}", disabled=not signed_in):
        save_answer(question, sql)
    if not signed_in:
        col_note.warning("Sign in (left sidebar) to run and save queries.")
    elif qid in saved:
        col_note.caption(f"Last saved: {saved[qid]['saved_at']:%Y-%m-%d %H:%M}")

    show_result(question)

    with st.expander("💡 Hint: expected SQL and expected result"):
        st.code(question["hint_sql"], language="sql")
        st.dataframe(pd.DataFrame(question["expected"]["rows"],
                                  columns=question["expected"]["columns"]),
                     width="stretch", hide_index=True)


def main() -> None:
    st.session_state.setdefault("results", {})
    if "flash" in st.session_state:
        st.toast(st.session_state.pop("flash"), icon="💾")
    auth_sidebar()
    signed_in = current_user() is not None
    saved = saved_answers()

    st.title("🗄️ SQL Quiz: Graduate Course Enrollment")
    st.write(f"{len(QUESTIONS)} questions · {TOTAL_POINTS} points. "
             "Write a query, run it against the live PostgreSQL database, and compare your "
             "result with the expected one. Grading compares result sets, not SQL text.")
    schema_reference()

    labels = [f"{q['id'].upper()} · {q['title']} ({q['points']} pts)"
              + ("  ✔ saved" if q["id"] in saved else "") for q in QUESTIONS]
    choice = st.radio("Question", range(len(QUESTIONS)),
                      format_func=lambda i: labels[i], horizontal=False)
    st.divider()
    question_panel(QUESTIONS[choice], signed_in, saved)


main()
