"""Optional Gemini AI backend for the Mentor System.

Zero extra dependencies (uses only the standard library via urllib),
so the college PC needs nothing beyond `pip install flask`.

Every function fails soft: on ANY error (no key, no internet, bad key,
API hiccup) it returns None, and the app falls back to its built-in
rule-based logic. The demo can never break because of AI.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_MODEL = "gemini-2.0-flash"
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Remembers the picked model per API key for the lifetime of the process,
# so we only ask Google for the model list once per key.
PICKED_MODELS = {}


def _http_reason(e):
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (400, 401, 403):
            return "bad_key"
        if e.code == 404:
            return "model_not_found"
        return f"http_{e.code}"
    return "no_internet"


def list_models(api_key, timeout=20):
    """Ask Google which models this key can use. Returns (names, reason)."""
    if not api_key:
        return None, "no_key"
    req = urllib.request.Request(LIST_URL, headers={"x-goog-api-key": api_key}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return None, _http_reason(e)
    try:
        names = []
        for m in data.get("models", []):
            name = m.get("name", "").replace("models/", "")
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" in methods and name.startswith("gemini-") \
                    and "tts" not in name and "image" not in name:
                names.append(name)
        return names, None
    except (AttributeError, TypeError):
        return None, "bad_response"


def pick_model(api_key, timeout=20):
    """Pick the best available text model for this key. Returns (name, reason)."""
    if not api_key:
        return None, "no_key"
    if api_key in PICKED_MODELS:
        return PICKED_MODELS[api_key], None
    names, reason = list_models(api_key, timeout=timeout)
    if not names:
        return None, reason or "model_not_found"
    flash = sorted([n for n in names if "flash" in n], reverse=True)
    chosen = (flash or sorted(names, reverse=True))[0]
    PICKED_MODELS[api_key] = chosen
    return chosen, None


def gemini_generate(api_key, prompt, model=DEFAULT_MODEL, timeout=25, max_tokens=600):
    """Send one prompt to Gemini.

    Returns (text_or_None, reason). reason is None on success, otherwise one of:
    no_key, bad_key, no_internet, model_not_found, empty_reply, bad_response, http_NNN.
    """
    if not api_key:
        return None, "no_key"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": max_tokens},
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL.format(model=model),
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            return None, "bad_key"
        if e.code == 404:
            return None, "model_not_found"
        return None, f"http_{e.code}"
    except Exception:
        return None, "no_internet"
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        return (text, None) if text else (None, "empty_reply")
    except (KeyError, IndexError, TypeError):
        return None, "bad_response"


def gemini_generate_any(api_key, prompt, timeout=25, max_tokens=600):
    """Pick a working model for this key automatically, then generate.

    Returns (text_or_None, reason). The model list is fetched once per key
    and cached, so model names can never go stale.
    """
    model, reason = pick_model(api_key, timeout=min(timeout, 15))
    if not model:
        return None, reason
    text, reason = gemini_generate(api_key, prompt, model=model,
                                   timeout=timeout, max_tokens=max_tokens)
    if text:
        return text, None
    if reason == "model_not_found":
        # Model vanished between listing and use; forget the cached pick once.
        PICKED_MODELS.pop(api_key, None)
    return None, reason


POLLINATIONS_URL = "https://text.pollinations.ai/{prompt}"


def pollinations_generate(prompt, timeout=40):
    """Free AI service, no key or signup needed. Returns (text_or_None, reason)."""
    url = POLLINATIONS_URL.format(prompt=urllib.parse.quote(prompt[:1500]))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace").strip()
            return (text, None) if text else (None, "empty_reply")
    except Exception as e:
        return None, _http_reason(e)


def ai_generate(api_key, prompt, timeout=30, max_tokens=600):
    """Best-effort AI: Gemini when a key is set, otherwise the free no-key service.

    Returns (text_or_None, provider, reason). provider is 'gemini', 'free' or 'none'.
    Even a failing/rate-limited Gemini key falls through to the free service,
    so the demo keeps working.
    """
    if api_key:
        text, _reason = gemini_generate_any(api_key, prompt, timeout=timeout, max_tokens=max_tokens)
        if text:
            return text, "gemini", None
    text, reason = pollinations_generate(prompt, timeout=min(timeout + 10, 45))
    if text:
        return text, "free", None
    return None, "none", reason


def _fmt(v, na="not recorded"):
    return na if v is None else str(v)


def chat_prompt(student, message):
    """Prompt for the student chatbot. Only the student's own record is given."""
    r = student["requirements"]
    reasons = "; ".join(student["risk"]["reasons"]) or "no concerns"
    return (
        "You are a friendly student-support assistant inside a college mentoring app. "
        "Answer ONLY using the student's record below. Do not invent facts not in the record. "
        "Keep it short (2-4 sentences), warm, and in simple English.\n\n"
        "Student record:\n"
        f"- Name: {student['name']}, Roll: {student['roll']}\n"
        f"- Class: {student['programme']} Semester {student['semester']} Section {student['section']}\n"
        f"- Mentor: {student['mentor'] or 'not assigned yet'}\n"
        f"- Exam form: {r['exam_form']} | Informed about notice: {'yes' if r['informed'] else 'no'} | "
        f"Sessional: {r['sessional']} | Fees: {r['fee_status']}\n"
        f"- Attendance: {_fmt(student['attendance'])}% | CGPA: {_fmt(student['cgpa'])}\n"
        f"- Open issues: {student['open_queries']} | Support level: {student['risk']['level']} ({reasons})\n\n"
        f"Student asks: {message}\n\n"
        "If the question is not about their mentoring record, politely say you can only help "
        "with their mentoring record and suggest they raise it with their mentor."
    )


def summary_prompt(student):
    """Prompt for the mentor briefing: what is wrong + what to do."""
    r = student["requirements"]
    reasons = "; ".join(student["risk"]["reasons"]) or "no concerns flagged"
    return (
        "You are helping a college mentor. Write a short briefing about this student for their mentor. "
        "Use plain text only (no markdown, no bullet symbols, no headings). "
        "Write 3 short paragraphs: 1) what needs attention, 2) what is most urgent and why, "
        "3) one concrete action the mentor should take this week. "
        "Use only the facts below; do not invent anything.\n\n"
        "Student facts:\n"
        f"- Name: {student['name']}, Roll: {student['roll']}, "
        f"Class: {student['programme']} Sem {student['semester']} Sec {student['section']}\n"
        f"- Attendance: {_fmt(student['attendance'])}% | CGPA: {_fmt(student['cgpa'])}\n"
        f"- Exam form: {r['exam_form']} | Informed about notice: {'yes' if r['informed'] else 'no'} | "
        f"Sessional: {r['sessional']} | Fees: {r['fee_status']}\n"
        f"- Open issues: {student['open_queries']} | Support level: {student['risk']['level']} | "
        f"Flags: {reasons}\n"
        f"- Mentor remark on file: {r['remarks'] or 'none'}"
    )
