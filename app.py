"""SCAIe: sustainability project-idea generator for TP's SCA module.

Colleagues: you normally only need to edit
  prompt.md     what Gemini is told to do
  options.yaml  dropdown lists and a few settings
"""

from __future__ import annotations

import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import requests
import streamlit as st
import streamlit.components.v1 as components
import yaml
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from docx import Document
from docx.shared import Pt, RGBColor

HERE = Path(__file__).parent
GREEN = "#1F6F43"
TOTAL_STEPS = 4

st.set_page_config(page_title="SCAIe", page_icon="🌱", layout="centered")

# ------------------------------------------------------------------ styling
st.markdown(
    f"""
    <style>
      .block-container {{ max-width: 820px; padding-top: 2.5rem; }}
      h1, h2, h3 {{ letter-spacing: -0.01em; }}
      .scaie-hero {{ text-align: center; }}
      .scaie-sub {{ color: #5f6368; font-size: 1.1rem; margin: 0.25rem 0 1rem; }}
      .scaie-step {{ color: {GREEN}; font-weight: 600; font-size: 0.85rem;
                     text-transform: uppercase; letter-spacing: 0.06em; }}
      .scaie-oneliner {{ color: #3c4043; font-size: 1.02rem; margin-bottom: 0.5rem; }}
      .scaie-hmw {{ border-left: 4px solid {GREEN}; background: #F4F7F2;
                    padding: 0.75rem 1rem; border-radius: 0 8px 8px 0; font-weight: 600; }}
      div.stButton > button[kind="primary"], div.stDownloadButton > button[kind="primary"] {{
          border-radius: 12px; padding: 0.6rem 2.2rem; font-weight: 700; }}
    </style>
    """,
    unsafe_allow_html=True,
)


# ------------------------------------------------------------------ config files
def load_options() -> dict:
    return yaml.safe_load((HERE / "options.yaml").read_text(encoding="utf-8"))


def load_prompt() -> str:
    text = (HERE / "prompt.md").read_text(encoding="utf-8")
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


def fill(template: str, values: dict) -> str:
    """Replace only known {placeholders}, so other braces in prompt.md are safe."""
    for key, val in values.items():
        template = template.replace("{" + key + "}", str(val))
    return template


def secret(*keys, default=None):
    try:
        node = st.secrets
        for k in keys:
            node = node[k]
        return node
    except (KeyError, FileNotFoundError, AttributeError):
        return default
    except Exception:  # no secrets file at all
        return default


OPTIONS = load_options()
SETTINGS = OPTIONS.get("settings", {})
DIPLOMAS = {d["name"]: d for d in OPTIONS["diplomas"]}
THEMES = {t["name"]: t for t in OPTIONS["themes"]}
DELIVERABLES = {d["name"]: d for d in OPTIONS["deliverables"]}


# ------------------------------------------------------------------ login (Auth0 via OIDC)
def login_enabled() -> bool:
    return secret("auth") is not None


def email_allowed(email: str) -> bool:
    email = (email or "").strip().lower()
    emails = [e.strip().lower() for e in secret("access", "allowed_emails", default=[]) or []]
    domains = [d.strip().lower().lstrip("@") for d in secret("access", "allowed_domains", default=[]) or []]
    if not emails and not domains:
        return bool(email)  # no list set: anyone who can sign in
    return email in emails or email.split("@")[-1] in domains


def auth_gate() -> str:
    """Returns the user's email, or stops the page until they're allowed in."""
    if not login_enabled():
        return "local-dev"

    if not st.user.is_logged_in:
        hero()
        _, mid, _ = st.columns([1, 2, 1])
        mid.button("Log in to start", type="primary", on_click=st.login, width="stretch")
        st.stop()

    email = st.user.get("email", "")
    if not email_allowed(email):
        st.error(f"**{email or 'This account'}** doesn't have access to SCAIe yet. "
                 "Log in with your TP email, or ask your SCA tutor to add you.")
        st.button("Log out", on_click=st.logout)
        st.stop()
    return email.lower()


# ------------------------------------------------------------------ usage cap (per user, per day)
@st.cache_resource
def usage_counter() -> dict:
    return {}


def uses_left(user: str) -> int:
    cap = int(SETTINGS.get("max_generations_per_day", 10))
    used = usage_counter().get((user, date.today().isoformat()), 0)
    return max(cap - used, 0)


def record_use(user: str) -> None:
    key = (user, date.today().isoformat())
    counter = usage_counter()
    counter[key] = counter.get(key, 0) + 1


# ------------------------------------------------------------------ Gemini call
# SCAIe makes two Gemini calls:
#   1. research: Gemini searches Google and writes the ideas (following prompt.md)
#   2. organise: Gemini sorts that write-up into the fixed layout below (JSON)
# Splitting them keeps the source links reliable; Google Search and JSON output
# in one call is still a preview feature and sometimes drops the sources.

def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _list(desc: str) -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


IDEA_SCHEMA = {
    "type": "object",
    "required": ["title", "one_liner", "problem_statement", "who_is_affected", "evidence",
                 "green_plan_link", "causes", "primary_stakeholders", "secondary_stakeholders",
                 "connections", "solution", "existing_initiatives", "deliverable_format",
                 "deliverable_description", "benefits", "limitations", "diploma_fit"],
    "properties": {
        "title": _str("Short, catchy project title (max ~8 words)."),
        "one_liner": _str("One sentence summary of the idea."),
        "problem_statement": _str('Starts with "How might we…"; names a specific group and situation.'),
        "who_is_affected": _list("Groups affected by the problem."),
        "evidence": {
            "type": "array",
            "description": "The evidence points given for this idea.",
            "items": {
                "type": "object",
                "required": ["point", "source_number", "source_title"],
                "properties": {
                    "point": _str("The fact or statistic, in plain words, without citation markers."),
                    "source_number": {"type": "integer",
                                      "description": "The [n] citation number backing this point, or 0 if none."},
                    "source_title": _str("Who published it (organisation or site), if the write-up says."),
                },
            },
        },
        "green_plan_link": _str("Singapore Green Plan 2030 pillar(s) and how, or 'Not directly linked'."),
        "causes": _list("2-3 main causes, each one sentence."),
        "primary_stakeholders": _list("Who benefits from the solution."),
        "secondary_stakeholders": _list("Who must be involved for it to work."),
        "connections": _str("How the problem, causes and stakeholders connect."),
        "solution": _str("The ONE solution and how it works."),
        "existing_initiatives": _str("Related existing Singapore initiatives and what's new about this idea."),
        "deliverable_format": _str("Physical prototype or Digital prototype."),
        "deliverable_description": _str("What the group would actually make, concretely."),
        "benefits": _list("2-3 benefits."),
        "limitations": _list("2-3 limitations, incl. what the solution does NOT solve."),
        "diploma_fit": _str("How the student's diploma skills come in."),
    },
}

ORGANISE_PROMPT = """Below is a write-up of {num_ideas} sustainability project ideas for a student.
Citation markers like [3] point to the numbered source list after it.

Turn the write-up into JSON that matches the schema, one entry per idea.
- Copy the content faithfully. Do not add facts, sources or ideas that aren't in the write-up.
- Remove citation markers from the text fields.
- For each evidence point, set source_number to the citation number that backs it, or 0 if it has none.

=== WRITE-UP ===
{write_up}

=== SOURCES ===
{sources}"""


def build_system_prompt(inputs: dict) -> str:
    diploma = DIPLOMAS[inputs["diploma"]]
    theme = THEMES[inputs["theme"]]
    deliverable = DELIVERABLES[inputs["deliverable"]]
    return fill(load_prompt(), {
        "diploma": diploma["name"],
        "diploma_description": " ".join(diploma["description"].split()),
        "theme": theme["name"],
        "sdgs": theme["sdgs"],
        "areas": theme["areas"],
        "problem": inputs["problem"].strip() or "(blank: the student hasn't picked a problem yet)",
        "deliverable": deliverable["name"],
        "deliverable_guidance": deliverable["guidance"],
        "num_ideas": int(SETTINGS.get("ideas_per_run", 3)),
    })


def _with_citations(text: str, supports) -> str:
    """Adds [n] markers after each sentence Google Search backed up."""
    data = text.encode("utf-8")  # segment offsets are byte positions
    for s in sorted(supports or [], key=lambda s: s.segment.end_index or 0, reverse=True):
        idx = s.grounding_chunk_indices or []
        if not idx or s.segment.end_index is None:
            continue
        marks = "".join(f"[{i + 1}]" for i in sorted(set(idx)))
        data = data[: s.segment.end_index] + f" {marks}".encode() + data[s.segment.end_index:]
    return data.decode("utf-8", errors="ignore")


def _real_url(url: str) -> str:
    """Google gives redirect links; follow one hop to get the actual page address."""
    try:
        r = requests.head(url, allow_redirects=False, timeout=5)
        return r.headers.get("location") or url
    except requests.RequestException:
        return url


NO_SEARCH_NOTE = """

## IMPORTANT: Google Search is switched off

You cannot look anything up for this run, so ignore the instructions above about searching.
- Don't present specific numbers or statistics as confirmed facts.
- For each evidence point, tell the student what to look up and where: name the organisation
  and the report or page (e.g. "NEA's waste statistics page: how much food waste Singapore
  throws away each year"). Set source_number to 0.
- Only name existing Singapore initiatives you are confident are real."""


def _call(client, **kwargs):
    """Calls Gemini, waiting and retrying briefly if it's busy (429/503)."""
    for wait in (5, 15, None):
        try:
            return client.models.generate_content(**kwargs)
        except genai_errors.APIError as e:
            if e.code not in (429, 503) or wait is None:
                raise
            time.sleep(wait)


def _schema() -> dict:
    return {"type": "object", "required": ["ideas"],
            "properties": {"ideas": {"type": "array", "items": IDEA_SCHEMA}}}


def generate_ideas(inputs: dict, status) -> dict:
    """Returns {'ideas': [...], 'sources': [...], 'search_html': str}"""
    client = genai.Client(api_key=secret("GEMINI_API_KEY"))
    model = SETTINGS.get("model", "gemini-3.8-flash")
    num = int(SETTINGS.get("ideas_per_run", 3))

    if not SETTINGS.get("google_search", True):
        # free tier: one call, no web search, evidence given as "what to look up"
        status.write("💡 Thinking up ideas…")
        resp = _call(client, model=model, contents="Please generate my project ideas.",
                     config=types.GenerateContentConfig(
                         system_instruction=build_system_prompt(inputs) + NO_SEARCH_NOTE,
                         response_mime_type="application/json",
                         response_json_schema=_schema()))
        ideas = json.loads(resp.text)["ideas"]
        for idea in ideas:
            for ev in idea.get("evidence", []):
                ev["source_url"] = ""
        return {"ideas": ideas, "sources": [], "search_html": ""}

    # 1. research with Google Search
    status.write("🔎 Searching Google for Singapore evidence…")
    research = _call(
        client,
        model=model,
        contents="Please research and write up my project ideas.",
        config=types.GenerateContentConfig(
            system_instruction=build_system_prompt(inputs),
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )
    write_up = research.text or ""
    if not write_up.strip():
        raise RuntimeError("SCAIe didn't get an answer this time. Please try again.")

    meta = research.candidates[0].grounding_metadata if research.candidates else None
    chunks = [c.web for c in (meta.grounding_chunks or []) if c.web] if meta else []
    for q in (meta.web_search_queries or []) if meta else []:
        status.write(f"Searched: *{q}*")
    with ThreadPoolExecutor(max_workers=8) as pool:
        urls = list(pool.map(_real_url, [c.uri for c in chunks]))
    sources = [{"title": c.title or "source", "url": u} for c, u in zip(chunks, urls)]

    # 2. organise into the fixed layout
    status.write("✍️ Organising your ideas…")
    organised = _call(
        client,
        model=model,
        contents=ORGANISE_PROMPT.format(
            num_ideas=num,
            write_up=_with_citations(write_up, meta.grounding_supports if meta else []),
            sources="\n".join(f"[{i}] {s['title']}: {s['url']}" for i, s in enumerate(sources, 1))
                    or "(none)",
        ),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=_schema(),
        ),
    )
    ideas = json.loads(organised.text)["ideas"]

    # attach real links; anything without a matching search result is marked "verify this"
    for idea in ideas:
        for ev in idea.get("evidence", []):
            n = ev.get("source_number") or 0
            if 1 <= n <= len(sources):
                ev["source_url"] = sources[n - 1]["url"]
                ev["source_title"] = ev.get("source_title") or sources[n - 1]["title"]
            else:
                ev["source_url"] = ""

    search_html = meta.search_entry_point.rendered_content if meta and meta.search_entry_point else ""
    return {"ideas": ideas, "sources": sources, "search_html": search_html or ""}


# ------------------------------------------------------------------ Word export
def build_docx(inputs: dict, result: dict) -> bytes:
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)

    doc.add_heading("SCAIe project ideas", 0)
    meta = doc.add_paragraph()
    for label, val in [("Diploma", inputs["diploma"]), ("Theme", inputs["theme"]),
                       ("Problem noticed", inputs["problem"] or "(left blank)"),
                       ("Prototype", inputs["deliverable"]),
                       ("Generated", date.today().strftime("%d %b %Y"))]:
        meta.add_run(f"{label}: ").bold = True
        meta.add_run(f"{val}\n")
    note = doc.add_paragraph(
        "These are AI-generated starting points. Check every source yourself before using it, "
        "and list SCAIe in your References slide (suggested wording at the end).")
    note.runs[0].italic = True

    for i, idea in enumerate(result["ideas"], 1):
        doc.add_page_break()
        doc.add_heading(f"Idea {i}: {idea['title']}", 1)
        doc.add_paragraph(idea["one_liner"])

        doc.add_heading("Step 1: Problem & evidence", 2)
        p = doc.add_paragraph()
        p.add_run("Problem statement: ").bold = True
        p.add_run(idea["problem_statement"])
        _bullets(doc, "Who is affected", idea["who_is_affected"])
        doc.add_paragraph().add_run("Evidence").bold = True
        for ev in idea["evidence"]:
            line = doc.add_paragraph(style="List Bullet")
            line.add_run(ev["point"])
            src = ev.get("source_title") or ""
            if ev.get("source_url"):
                line.add_run(f" ({src}: {ev['source_url']})").font.color.rgb = RGBColor(0x55, 0x55, 0x55)
            elif src:
                line.add_run(f" ({src}, no link found, please verify)").italic = True
        p = doc.add_paragraph()
        p.add_run("Singapore Green Plan 2030: ").bold = True
        p.add_run(idea["green_plan_link"])

        doc.add_heading("Step 2: Causes & stakeholders", 2)
        _bullets(doc, "Main causes", idea["causes"])
        _bullets(doc, "Primary stakeholders (who benefits)", idea["primary_stakeholders"])
        _bullets(doc, "Secondary stakeholders (who needs to be involved)", idea["secondary_stakeholders"])
        p = doc.add_paragraph()
        p.add_run("How it connects: ").bold = True
        p.add_run(idea["connections"])

        doc.add_heading("Step 3: Solution & prototype", 2)
        doc.add_paragraph(idea["solution"])
        p = doc.add_paragraph()
        p.add_run("Existing initiatives / what's new: ").bold = True
        p.add_run(idea["existing_initiatives"])
        p = doc.add_paragraph()
        p.add_run(f"{idea['deliverable_format']}: ").bold = True
        p.add_run(idea["deliverable_description"])

        doc.add_heading("Step 4: Impacts & limitations", 2)
        _bullets(doc, "Benefits", idea["benefits"])
        _bullets(doc, "Limitations", idea["limitations"])

        p = doc.add_paragraph()
        p.add_run("Why it fits your diploma: ").bold = True
        p.add_run(idea["diploma_fit"])

    doc.add_page_break()
    doc.add_heading("Sources SCAIe looked at", 1)
    for s in result["sources"]:
        doc.add_paragraph(f"{s['title']}: {s['url']}", style="List Bullet")
    doc.add_heading("Referencing SCAIe (suggested)", 2)
    doc.add_paragraph(
        f"Temasek Polytechnic. ({date.today().year}). SCAIe project idea generator "
        f"[AI tool powered by Google Gemini]. Prompt inputs: diploma = {inputs['diploma']}; "
        f"theme = {inputs['theme']}; problem = \"{inputs['problem'] or 'blank'}\"; "
        f"prototype = {inputs['deliverable']}. Accessed {date.today().strftime('%d %B %Y')}.")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _bullets(doc, heading: str, items: list) -> None:
    doc.add_paragraph().add_run(heading).bold = True
    for it in items:
        doc.add_paragraph(it, style="List Bullet")


# ------------------------------------------------------------------ UI pieces
def hero() -> None:
    st.markdown("<div class='scaie-hero'><h1>Hi! I'm SCAIe.</h1>"
                "<p class='scaie-sub'>I'll help you explore sustainability project ideas "
                "tailored to your diploma and interests. Let's get started.</p></div>",
                unsafe_allow_html=True)
    _, mid, _ = st.columns([1, 2, 1])
    mid.image(str(HERE / "assets" / "scaie.png"), width="stretch")


def step_header(n: int, title: str, sub: str) -> None:
    st.markdown(f"<div class='scaie-step'>Step {n} of {TOTAL_STEPS}</div>", unsafe_allow_html=True)
    st.progress(n / TOTAL_STEPS)
    st.header(title)
    st.caption(sub)


def go(step: int) -> None:
    st.session_state.step = step


def nav(back_to: int, next_label: str, on_next, disabled: bool = False) -> None:
    left, right = st.columns([1, 2])
    left.button("← Back", on_click=go, args=(back_to,), width="stretch")
    right.button(next_label, type="primary", on_click=on_next, disabled=disabled,
                 width="stretch")


def picker(key: str, label: str, choices: list[str]) -> None:
    current = st.session_state.inputs.get(key)
    choice = st.selectbox(label, choices, index=choices.index(current) if current in choices else None,
                          placeholder="Choose one…")
    st.session_state.inputs[key] = choice


def render_idea(idea: dict) -> None:
    st.markdown(f"### {idea['title']}")
    st.markdown(f"<div class='scaie-oneliner'>{idea['one_liner']}</div>", unsafe_allow_html=True)
    st.markdown(f"<div class='scaie-hmw'>{idea['problem_statement']}</div>", unsafe_allow_html=True)
    st.write("")

    with st.expander("**1 · Problem & evidence**", expanded=True):
        st.markdown("**Who is affected:** " + "; ".join(idea["who_is_affected"]))
        st.markdown("**Evidence**")
        for ev in idea["evidence"]:
            if ev.get("source_url"):
                st.markdown(f"- {ev['point']} [{ev['source_title'] or 'source'}]({ev['source_url']})")
            else:
                st.markdown(f"- {ev['point']} *(no link found: look this up and verify it)*")
        st.markdown(f"**Singapore Green Plan 2030:** {idea['green_plan_link']}")

    with st.expander("**2 · Causes & stakeholders**"):
        st.markdown("**Main causes**\n" + "\n".join(f"- {c}" for c in idea["causes"]))
        a, b = st.columns(2)
        a.markdown("**Primary (who benefits)**\n" + "\n".join(f"- {s}" for s in idea["primary_stakeholders"]))
        b.markdown("**Secondary (who's needed)**\n" + "\n".join(f"- {s}" for s in idea["secondary_stakeholders"]))
        st.markdown(f"**How it connects:** {idea['connections']}")

    with st.expander("**3 · Solution & prototype**"):
        st.markdown(idea["solution"])
        st.markdown(f"**Existing initiatives / what's new:** {idea['existing_initiatives']}")
        st.markdown(f"**{idea['deliverable_format']}.** {idea['deliverable_description']}")

    with st.expander("**4 · Impacts & limitations**"):
        a, b = st.columns(2)
        a.markdown("**Benefits**\n" + "\n".join(f"- {x}" for x in idea["benefits"]))
        b.markdown("**Limitations**\n" + "\n".join(f"- {x}" for x in idea["limitations"]))

    st.info(f"**Why it fits your diploma:** {idea['diploma_fit']}")


# ------------------------------------------------------------------ pages
def page_welcome() -> None:
    hero()
    _, mid, _ = st.columns([1, 2, 1])
    mid.button("Start Your Project Ideas", type="primary", on_click=go, args=(1,),
               width="stretch")


def page_diploma() -> None:
    step_header(1, "What is your diploma?",
                "This helps me tailor sustainability project ideas to your field of study.")
    picker("diploma", "Select your diploma", list(DIPLOMAS))
    nav(0, "Continue →", lambda: go(2), disabled=not st.session_state.inputs.get("diploma"))


def page_theme() -> None:
    step_header(2, "Which sustainability challenge interests you?",
                "These are the challenge themes from your project brief.")
    picker("theme", "Select a challenge theme", list(THEMES))
    theme = THEMES.get(st.session_state.inputs.get("theme") or "")
    if theme:
        st.caption(f"**SDGs:** {theme['sdgs']}  ·  **Explore:** {theme['areas']}")
    nav(1, "Continue →", lambda: go(3), disabled=not st.session_state.inputs.get("theme"))


def page_problem() -> None:
    step_header(3, "What sustainability problem would you like to solve?",
                "Share a problem you've noticed in school, your community or daily life. "
                "Not sure yet? Leave it blank and I'll suggest some.")
    if "problem_box" not in st.session_state:
        st.session_state.problem_box = st.session_state.inputs.get("problem", "")
    st.text_area("Sustainability concern (optional)", key="problem_box", max_chars=200, height=130,
                 placeholder="e.g. Seniors in my block rarely leave home and seem lonely")

    def save_and_go(step: int) -> None:
        # saved in the button callback, so text typed just before clicking isn't lost
        st.session_state.inputs["problem"] = st.session_state.get("problem_box", "")
        go(step)

    left, right = st.columns([1, 2])
    left.button("← Back", on_click=save_and_go, args=(2,), width="stretch")
    right.button("Continue →", type="primary", on_click=save_and_go, args=(4,), width="stretch")


def page_deliverable(user: str) -> None:
    step_header(4, "Which prototype would you like to make?",
                "This helps me suggest the right type of project for you.")
    picker("deliverable", "Select a prototype type", list(DELIVERABLES))
    d = DELIVERABLES.get(st.session_state.inputs.get("deliverable") or "")
    if d:
        st.caption(d["guidance"])

    def submit():
        st.session_state.result = None
        go(5)

    nav(3, "Submit", submit, disabled=not st.session_state.inputs.get("deliverable") or uses_left(user) == 0)
    if uses_left(user) == 0:
        st.warning("You've reached today's limit for generating ideas. Try again tomorrow.")


def page_results(user: str) -> None:
    inputs = st.session_state.inputs

    if st.session_state.get("result") is None:
        if uses_left(user) == 0:
            st.warning("You've reached today's limit for generating ideas. Try again tomorrow.")
            st.button("← Back", on_click=go, args=(4,))
            return
        with st.status("SCAIe is researching Singapore sources… this takes about a minute.",
                       expanded=True) as status:
            try:
                st.session_state.result = generate_ideas(inputs, status)
                record_use(user)
                status.update(label="Done! Here are your ideas.", state="complete", expanded=False)
            except genai_errors.APIError as e:
                status.update(label="Something went wrong", state="error")
                if e.code == 429:
                    st.error("SCAIe is busy right now (lots of people are using it, or today's "
                             "free limit is used up). Please try again in a few minutes.")
                else:
                    st.error(f"SCAIe couldn't reach Gemini ({e.code}). Please try again in a minute.")
            except Exception as e:  # noqa: BLE001
                status.update(label="Something went wrong", state="error")
                st.error(str(e) or "Something went wrong. Please try again.")
        if st.session_state.result is None:
            st.button("Try again", type="primary", on_click=lambda: None)
            st.button("← Back", on_click=go, args=(4,))
            return

    result = st.session_state.result
    st.header("Your project ideas")
    st.caption(f"{inputs['diploma']} · {inputs['theme']} · {inputs['deliverable']}")
    st.warning("These are starting points, not final answers. Check each source before you use it, "
               "and remember to reference SCAIe in your slides.", icon="⚠️")

    tabs = st.tabs([f"Idea {i}" for i in range(1, len(result["ideas"]) + 1)])
    for tab, idea in zip(tabs, result["ideas"]):
        with tab:
            render_idea(idea)

    if result["sources"]:
        with st.expander(f"All sources SCAIe looked at ({len(result['sources'])})"):
            for s in result["sources"]:
                st.markdown(f"- [{s['title']}]({s['url']})")

    if result.get("search_html"):
        # Google requires showing these search suggestions alongside search-grounded answers
        st.caption("Explore further on Google")
        components.html(result["search_html"], height=110, scrolling=True)

    st.divider()
    st.download_button("⬇ Download ideas (Word)", data=build_docx(inputs, result),
                       file_name=f"SCAIe ideas - {inputs['theme'][:40]}.docx",
                       mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                       type="primary", width="stretch")
    a, b = st.columns(2)

    def regenerate():
        st.session_state.result = None

    a.button(f"↻ Generate new ideas ({uses_left(user)} left today)", on_click=regenerate,
             disabled=uses_left(user) == 0, width="stretch")
    b.button("Start over", on_click=reset, width="stretch")


def reset() -> None:
    st.session_state.step = 0
    st.session_state.inputs = {}
    st.session_state.result = None
    st.session_state.pop("problem_box", None)


# ------------------------------------------------------------------ main
def main() -> None:
    if not secret("GEMINI_API_KEY"):
        st.error("Missing GEMINI_API_KEY. Add it to .streamlit/secrets.toml (see README).")
        st.stop()

    user = auth_gate()
    st.session_state.setdefault("step", 0)
    st.session_state.setdefault("inputs", {})
    st.session_state.setdefault("result", None)

    if login_enabled():
        with st.sidebar:
            st.caption(f"Logged in as {user}")
            st.button("Log out", on_click=st.logout)

    step = st.session_state.step
    if step == 0:
        page_welcome()
    elif step == 1:
        page_diploma()
    elif step == 2:
        page_theme()
    elif step == 3:
        page_problem()
    elif step == 4:
        page_deliverable(user)
    else:
        page_results(user)


main()
