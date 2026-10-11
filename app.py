"""CapBrief — Flask backend.

Serves the site, calls the Claude API for notes and Q&A, enforces the free
daily limit on the server, and unlocks Pro through Stripe.
"""
import json
import os
import re
import threading
import time
from datetime import date, timedelta

import anthropic
import stripe
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, send_from_directory, session

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("DEV") != "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=365),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,  # 10 MB request cap (screenshots)
)

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

NOTES_MODEL = os.getenv("NOTES_MODEL", "claude-sonnet-5-5")
ASK_MODEL = os.getenv("ASK_MODEL", "claude-haiku-5-5")
FREE_PER_DAY = int(os.getenv("FREE_PER_DAY", "3"))
FREE_ASKS_PER_DAY = int(os.getenv("FREE_ASKS_PER_DAY", "20"))
PRO_LINK = os.getenv("STRIPE_PRO_LINK", "")
TEAM_LINK = os.getenv("STRIPE_TEAM_LINK", "")

FREE_CHARS, PRO_CHARS = 24_000, 120_000
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
LEVELS = {"beginner", "student", "pro"}

# ---------------------------------------------------------------- usage limits
# In-memory counters keyed by day + IP. Fine for one server instance; move to
# Redis if you run several workers or instances.
_usage, _lock = {}, threading.Lock()


def _ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "?")


def _key(kind):
    return f"{date.today()}|{kind}|{_ip()}"


def used(kind):
    return _usage.get(_key(kind), 0)


def incr(kind):
    with _lock:
        today = str(date.today())
        for k in [k for k in _usage if not k.startswith(today)]:
            del _usage[k]  # drop old days
        _usage[_key(kind)] = _usage.get(_key(kind), 0) + 1


# ---------------------------------------------------------------- Pro status
_pro_cache = {}


def is_pro():
    cust = session.get("customer")
    if not cust or not stripe.api_key:
        return False
    hit = _pro_cache.get(cust)
    if hit and hit[1] > time.time():
        return hit[0]
    try:
        subs = stripe.Subscription.list(customer=cust, status="all", limit=10)
        ok = any(s.status in ("active", "trialing") for s in subs.data)
    except Exception:
        ok = hit[0] if hit else False
    _pro_cache[cust] = (ok, time.time() + 1800)
    return ok


def err(msg, status=400, code=None):
    return jsonify({"error": msg, "code": code}), status


def parse_json(text):
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1])


# ---------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def config():
    return jsonify(pro_link=PRO_LINK, team_link=TEAM_LINK, free_per_day=FREE_PER_DAY)


@app.get("/api/status")
def status():
    pro = is_pro()
    return jsonify(pro=pro, left=max(0, FREE_PER_DAY - used("notes")))


# ---------------------------------------------------------------- notes
SYSTEM = (
    "You write study notes about the business of sports. Return ONLY a JSON "
    "object, no prose and no code fences. Use only facts from the source for "
    "numbers and claims; glossary meanings may use general knowledge. If the "
    "source isn't usable, still return the JSON with a summary that says so."
)


@app.post("/api/notes")
def notes():
    body = request.get_json(silent=True) or {}
    pro = is_pro()
    if not pro and used("notes") >= FREE_PER_DAY:
        return err("You've used today's free notes. Go Pro for unlimited notes.", 402, "limit")

    text = str(body.get("text", "")).strip()
    image = body.get("image")
    level = body.get("level") if body.get("level") in LEVELS else "student"
    ncards = body.get("ncards") if body.get("ncards") in (6, 10, 15) else 10
    nquiz = body.get("nquiz") if body.get("nquiz") in (4, 6, 10) else 6

    if image and not pro:
        return err("Screenshot reading is a Pro feature. Paste the text or upload a PDF instead.", 402, "pro_only")
    if image and image.get("media_type") not in IMAGE_TYPES:
        return err("Upload a PNG, JPG, WEBP or GIF screenshot.")
    if not image and len(text) < 80:
        return err("Add at least a few sentences so there's something to study.")

    text = text[: PRO_CHARS if pro else FREE_CHARS]
    shape = f"""Reader level: {level}.
Return JSON with this shape:
{{"title": string (max 8 words),
"summary": string (2-3 sentences),
"keyPoints": [5-7 short strings],
"numbers": [up to 6 {{"value": string, "label": string}}],
"takeaway": string (one sentence: why this matters for a team or business),
"flashcards": [{ncards} {{"q": string, "a": string}}],
"quiz": [{nquiz} {{"q": string, "options": [4 strings], "answer": 0-3, "why": string}}],
"glossary": [4-8 {{"term": string, "meaning": string}}],
"interview": [4 {{"q": string, "tip": string}}],
"script": string (60-second video script for a sports analytics creator: hook, 3 beats, closing line, with line breaks)}}"""

    content = []
    if image:
        content.append({"type": "image", "source": {"type": "base64", "media_type": image["media_type"], "data": image["data"]}})
        content.append({"type": "text", "text": "The SOURCE is the attached screenshot.\n" + shape})
    else:
        content.append({"type": "text", "text": f"{shape}\n\nSOURCE:\n{text}"})

    try:
        msg = client.messages.create(
            model=NOTES_MODEL, max_tokens=8000, system=SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
        data = parse_json("".join(b.text for b in msg.content if b.type == "text"))
    except (ValueError, json.JSONDecodeError):
        return err("Couldn't build notes from that. Try again or shorten the source.", 502)
    except anthropic.APIError:
        app.logger.exception("Claude API error")
        return err("The notes service is busy. Try again in a minute.", 503)

    if not pro:
        incr("notes")
    source = text or f"[Screenshot] {data.get('summary', '')} {' '.join(data.get('keyPoints', []))}"
    return jsonify(notes=data, source=source, pro=pro, left=max(0, FREE_PER_DAY - used("notes")))


# ---------------------------------------------------------------- ask
@app.post("/api/ask")
def ask():
    body = request.get_json(silent=True) or {}
    pro = is_pro()
    if not pro and used("ask") >= FREE_ASKS_PER_DAY:
        return err("You've hit today's free questions. Go Pro for unlimited.", 402, "limit")
    question = str(body.get("question", "")).strip()[:500]
    source = str(body.get("source", ""))[: PRO_CHARS if pro else FREE_CHARS]
    if not question:
        return err("Type a question first.")

    msgs = []
    for m in (body.get("history") or [])[-8:]:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            msgs.append({"role": m["role"], "content": str(m["content"])[:2000]})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if msgs and msgs[-1]["role"] == "user":
        msgs.pop()
    msgs.append({"role": "user", "content": question})

    try:
        msg = client.messages.create(
            model=ASK_MODEL, max_tokens=600,
            system=("You are a sports-business tutor. Answer from the SOURCE and say plainly "
                    "when something isn't in it. Keep answers under 120 words.\n\nSOURCE:\n" + source),
            messages=msgs,
        )
    except anthropic.APIError:
        app.logger.exception("Claude API error")
        return err("The answer service is busy. Try again in a minute.", 503)
    if not pro:
        incr("ask")
    return jsonify(answer="".join(b.text for b in msg.content if b.type == "text"))


# ---------------------------------------------------------------- Stripe
@app.get("/success")
def success():
    sid = request.args.get("session_id", "")
    try:
        cs = stripe.checkout.Session.retrieve(sid)
        if cs.customer and cs.status == "complete":
            session.permanent = True
            session["customer"] = cs.customer
            _pro_cache.pop(cs.customer, None)
            return redirect("/?pro=1")
    except Exception:
        app.logger.exception("Checkout lookup failed")
    return redirect("/#pricing")


@app.get("/portal")
def portal():
    cust = session.get("customer")
    if not cust:
        return redirect("/#pricing")
    ps = stripe.billing_portal.Session.create(customer=cust, return_url=request.host_url)
    return redirect(ps.url)


if __name__ == "__main__":
    app.run(debug=os.getenv("DEV") == "1", port=int(os.getenv("PORT", "5000")))
