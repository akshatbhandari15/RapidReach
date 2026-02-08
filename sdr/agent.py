"""
sdr/agent.py
SDR Agent — FastAPI service + Dedalus DedalusRunner orchestration.

Architecture (Agent-as-Tool pattern):
  Coordinator (cheap model) delegates to specialist tools:
    1. research_business — deep web research via Brave Search MCP
    2. draft_proposal — writes a tailored website proposal (strong model)
    3. fact_check_proposal — validates/refines the draft (generator-critic)
    4. call_business — AI phone call via ElevenLabs
    5. classify_conversation — LLM classification of call outcome
    6. send_outreach_email — HTML email via Gmail service account
    7. save_session — persist results to BigQuery
  Callbacks stream progress to the UI Client.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI
from dedalus_labs import AsyncDedalus, DedalusRunner
from dotenv import load_dotenv

from common.config import (
    DEFAULT_MODEL,
    RESEARCH_MODEL,
    DRAFT_MODEL,
    CLASSIFIER_MODEL,
    UI_CLIENT_URL,
)
from common.models import (
    AgentCallback,
    AgentType,
    CallOutcome,
    ConversationClassification,
    ProposalDraft,
    SDRRequest,
    SDRResult,
)
from sdr.tools.phone_call import make_phone_call
from sdr.tools.email_tool import send_email
from sdr.tools.bigquery_utils import save_sdr_session, update_lead_status

load_dotenv()
logger = logging.getLogger(__name__)

# Fallback email — used only when no email is found from business data or transcript
FALLBACK_EMAIL = os.getenv("FALLBACK_EMAIL", "arnavahuja21@gmail.com")

# In-memory session store
sdr_sessions: dict[str, SDRResult] = {}

# ── Email extraction from spoken transcripts ─────────────────

_NUMBER_WORDS_MAP = {
    'zero': '0', 'oh': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
    'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
    'ten': '10', 'eleven': '11', 'twelve': '12', 'thirteen': '13',
    'fourteen': '14', 'fifteen': '15', 'sixteen': '16', 'seventeen': '17',
    'eighteen': '18', 'nineteen': '19', 'twenty': '20', 'thirty': '30',
    'forty': '40', 'fifty': '50', 'sixty': '60', 'seventy': '70',
    'eighty': '80', 'ninety': '90',
}
_NUMBER_WORDS_SET = set(_NUMBER_WORDS_MAP.keys())
_NUMBER_WORDS_RE = '|'.join(sorted(_NUMBER_WORDS_MAP.keys(), key=len, reverse=True))

_INVALID_SOLO_USERNAMES = _NUMBER_WORDS_SET | {
    'wednesday', 'thursday', 'monday', 'tuesday', 'friday', 'saturday', 'sunday',
    'call', 'look', 'looking', 'reach', 'meet', 'available', 'scheduled',
    'appointment', 'meeting', 'time', 'address', 'number', 'phone', 'business',
    'interested', 'discussed', 'contact', 'march', 'april', 'may', 'june',
    'january', 'february', 'july', 'august', 'september', 'october', 'november',
    'december', 'invitation', 'information', 'provide', 'discuss', 'touch',
    'email', 'your', 'that', 'this', 'with', 'from', 'have', 'been',
    'a', 'i', 'my', 'me', 'is', 'it', 'in', 'on', 'to', 'of', 'or', 'an',
    'the', 'and', 'for', 'but', 'not', 'are', 'was', 'has', 'had', 'his',
    'her', 'our', 'can', 'you', 'all', 'its',
}

_STRIP_LEADING = {'your', 'my', 'is', 'email', 'address', 'it', 'its',
                   "it's", "that's", 'thats', 'the', 'a'}


def extract_emails_from_transcript(text: str) -> list[str]:
    """
    Robustly extract email addresses from a call transcript.
    Handles:
      - Standard format:      user@gmail.com
      - Mixed spoken:         TM07MARCH at gmail.com
      - Spoken with dot:      tm07march at gmail dot com
      - Fully spelled out:    T M zero seven M A R C H at gmail dot com
      - Dictated short form:  t m zero seven march at gmail dot com
    Returns list of emails, best match first.
    """
    import re
    candidates: list[tuple[int, str]] = []

    # 1) Standard email with @ symbol
    for m in re.finditer(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', text):
        candidates.append((100, m.group()))

    # 2) Contiguous username + " at " + domain.tld  (e.g. "TM07MARCH at gmail.com")
    for m in re.finditer(
        r'\b([A-Za-z0-9._%+-]{2,})\s+at\s+([A-Za-z0-9]+\.[A-Za-z]{2,})\b',
        text, re.IGNORECASE,
    ):
        if m.group(1).lower() not in _INVALID_SOLO_USERNAMES:
            candidates.append((90, f"{m.group(1)}@{m.group(2)}"))

    # 3) Contiguous username + " at " + domain + " dot " + tld
    for m in re.finditer(
        r'\b([A-Za-z0-9._%+-]{2,})\s+at\s+([A-Za-z0-9]+)\s+dot\s+([A-Za-z]{2,})\b',
        text, re.IGNORECASE,
    ):
        if m.group(1).lower() not in _INVALID_SOLO_USERNAMES:
            candidates.append((85, f"{m.group(1)}@{m.group(2)}.{m.group(3)}"))

    # 4) Spelled-out username + " at " + domain + " dot " + tld
    #    e.g. "T M zero seven M A R C H at gmail dot com"
    _tok = rf'(?:[A-Za-z]|{_NUMBER_WORDS_RE}|\d+|[A-Za-z]{{2,6}})'
    for m in re.finditer(
        rf'(?<![A-Za-z])({_tok}(?:\s+{_tok})+)\s+at\s+([A-Za-z0-9]+)\s+dot\s+([A-Za-z]{{2,}})\b',
        text, re.IGNORECASE,
    ):
        raw, domain, tld = m.group(1), m.group(2), m.group(3)
        tokens = raw.split()
        while tokens and tokens[0].lower() in _STRIP_LEADING:
            tokens.pop(0)
        raw = ' '.join(tokens)
        if not raw:
            continue
        cleaned = raw
        for w, d in sorted(_NUMBER_WORDS_MAP.items(), key=lambda x: len(x[0]), reverse=True):
            cleaned = re.sub(rf'\b{w}\b', d, cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s+', '', cleaned)
        if len(cleaned) >= 3:
            candidates.append((80, f"{cleaned}@{domain}.{tld}"))

    # 5) Spelled-out username + " at " + domain.tld
    for m in re.finditer(
        rf'(?<![A-Za-z])({_tok}(?:\s+{_tok})+)\s+at\s+([A-Za-z0-9]+\.[A-Za-z]{{2,}})\b',
        text, re.IGNORECASE,
    ):
        raw, domain = m.group(1), m.group(2)
        tokens = raw.split()
        while tokens and tokens[0].lower() in _STRIP_LEADING:
            tokens.pop(0)
        raw = ' '.join(tokens)
        if not raw:
            continue
        cleaned = raw
        for w, d in sorted(_NUMBER_WORDS_MAP.items(), key=lambda x: len(x[0]), reverse=True):
            cleaned = re.sub(rf'\b{w}\b', d, cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s+', '', cleaned)
        if len(cleaned) >= 3:
            candidates.append((75, f"{cleaned}@{domain}"))

    # De-duplicate, normalise to lowercase, best-priority first
    candidates.sort(key=lambda x: -x[0])
    seen: set[str] = set()
    unique: list[str] = []
    for _, email in candidates:
        e_lower = email.lower().strip('.')
        if e_lower not in seen:
            seen.add(e_lower)
            unique.append(e_lower)
    return unique


# ── Meeting time extraction from transcripts ─────────────────

_DAY_NAMES = {
    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
    'friday': 4, 'saturday': 5, 'sunday': 6,
}

_TIME_WORDS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12,
}


def _next_weekday(day_index: int, after: datetime | None = None) -> datetime:
    """Return the next occurrence of a weekday (0=Mon … 6=Sun)."""
    base = after or datetime.now()
    days_ahead = day_index - base.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return base + timedelta(days=days_ahead)


def extract_meeting_time_from_transcript(transcript: str) -> datetime:
    """
    Parse a meeting time mentioned in a call transcript.
    Looks for patterns like "Wednesday at 11", "friday at 2 p.m.", "Tuesday 3pm".
    Falls back to next Wednesday at 11:00 AM if nothing is found.
    """
    text = transcript.lower()

    # Pattern: <day_name> at <time> (am/pm)
    day_pattern = '|'.join(_DAY_NAMES.keys())
    time_pattern = re.compile(
        rf'\b({day_pattern})\s+(?:at\s+)?(\d{{1,2}}|'
        + '|'.join(_TIME_WORDS.keys())
        + r')(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?|am|pm)?',
        re.IGNORECASE,
    )

    match = time_pattern.search(text)
    if match:
        day_str = match.group(1).lower()
        hour_raw = match.group(2)
        minute_str = match.group(3)
        ampm = (match.group(4) or '').replace('.', '').lower()

        # Convert hour
        hour = _TIME_WORDS.get(hour_raw, None)
        if hour is None:
            hour = int(hour_raw)
        minute = int(minute_str) if minute_str else 0

        # Apply AM/PM
        if ampm == 'pm' and hour < 12:
            hour += 12
        elif ampm == 'am' and hour == 12:
            hour = 0
        elif not ampm and 1 <= hour <= 7:
            # Assume PM for business hours (1-7 without am/pm)
            hour += 12

        target_day = _next_weekday(_DAY_NAMES[day_str])
        return target_day.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Default: next Wednesday at 11:00 AM
    default = _next_weekday(2)  # Wednesday = 2
    return default.replace(hour=11, minute=0, second=0, microsecond=0)


def generate_ics(
    start: datetime,
    duration_minutes: int = 30,
    summary: str = "RapidReach — Follow-up Meeting",
    description: str = "",
    attendee_email: str = "",
    organizer_email: str = "",
) -> str:
    """Generate an iCalendar (.ics) string for a meeting invite."""
    end = start + timedelta(minutes=duration_minutes)
    uid = str(uuid.uuid4())
    now_stamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    start_stamp = start.strftime('%Y%m%dT%H%M%S')
    end_stamp = end.strftime('%Y%m%dT%H%M%S')

    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//RapidReach//SDR//EN',
        'METHOD:REQUEST',
        'BEGIN:VEVENT',
        f'UID:{uid}',
        f'DTSTAMP:{now_stamp}',
        f'DTSTART:{start_stamp}',
        f'DTEND:{end_stamp}',
        f'SUMMARY:{summary}',
        f'DESCRIPTION:{description}',
    ]
    if organizer_email:
        lines.append(f'ORGANIZER;CN=RapidReach Team:mailto:{organizer_email}')
    if attendee_email:
        lines.append(f'ATTENDEE;RSVP=TRUE:mailto:{attendee_email}')
    lines += [
        'STATUS:CONFIRMED',
        'END:VEVENT',
        'END:VCALENDAR',
    ]
    return '\r\n'.join(lines)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("SDR Agent service starting")
    yield
    logger.info("SDR Agent service shutting down")


app = FastAPI(title="SalesShortcut SDR Agent", lifespan=lifespan)


# ── Helper: send callback to UI ─────────────────────────────

async def notify_ui(callback_url: str, payload: AgentCallback):
    url = callback_url or f"{UI_CLIENT_URL}/agent_callback"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=payload.model_dump())
    except Exception as e:
        logger.warning(f"UI callback failed: {e}")


# ── Specialist Tools (Agent-as-Tool pattern) ─────────────────

async def research_business(business_name: str, city: str, address: str = "") -> str:
    """
    Deep research on a business using web search.
    Examines competitors, reviews, web presence gaps, and market opportunities.
    Returns a detailed research summary.
    """
    client = AsyncDedalus()
    runner = DedalusRunner(client)

    research_prompt = f"""Research this business thoroughly:
Business: {business_name}
City: {city}
Address: {address}

Find:
1. Their online presence (or lack thereof)
2. Competitor businesses nearby that DO have websites
3. Customer reviews and reputation
4. Market gaps and opportunities
5. What type of website would benefit them most

Provide a detailed research report."""

    # Three-tier fallback approach
    
    # Tier 1: Try Brave Search MCP (single attempt — fail fast to Google)
    try:
        print("🔍 Attempting research with Brave Search MCP...")
        result = await runner.run(
            input=research_prompt,
            model=RESEARCH_MODEL,
            mcp_servers=["windsor/brave-search-mcp"],
            max_steps=1,
        )
        print(f"✅ Research result (Brave Search MCP): {result.final_output}")
        return result.final_output
        
    except Exception as e:
        print(f"❌ Brave Search MCP failed: {e}")
        print("🔍 Falling back to Google Search MCP...")
        
        # Tier 2: Try Google Search MCP (single attempt — fail fast to knowledge)
        try:
            result = await runner.run(
                input=research_prompt,
                model=RESEARCH_MODEL,
                mcp_servers=["google-search"],
                max_steps=1,
            )
            print(f"✅ Research result (Google Search MCP): {result.final_output}")
            return result.final_output
            
        except Exception as e2:
            print(f"❌ Google Search MCP also failed: {e2}")
            print("🧠 Falling back to knowledge-based research...")
            
            # Tier 3: Knowledge-based fallback
            try:
                fallback_result = await runner.run(
                    input=f"""Generate a comprehensive research report for this business without web search:
Business: {business_name}
City: {city}
Address: {address}

Based on the business name and type, provide insights on:
1. Likely business category and typical online presence needs
2. Common competitors in this industry that would have websites
3. Typical customer expectations and review patterns for this type of business
4. Market opportunities for businesses in {city}
5. Website features that would benefit this type of business

Use your knowledge of business patterns, local market dynamics, and industry standards.
Be specific and actionable. Format as a detailed research report.""",
                    model=RESEARCH_MODEL,
                    max_steps=3,
                )
                print(f"✅ Research result (Knowledge-based fallback): {fallback_result.final_output}")
                return fallback_result.final_output
                
            except Exception as e3:
                print(f"❌ All research methods failed. Brave: {e}, Google: {e2}, Knowledge: {e3}")
                # Last resort: return a basic template
                return f"""Research Report for {business_name} (Generated from limited data)

BUSINESS OVERVIEW:
- Name: {business_name}
- Location: {city}
- Address: {address}

ONLINE PRESENCE ASSESSMENT:
- Limited information available due to research limitations
- Likely has minimal online presence based on business type
- Opportunity exists for website development

RECOMMENDATIONS:
1. Create a professional website with business information
2. Establish Google My Business listing
3. Develop social media presence
4. Implement online booking/contact system
5. Focus on local SEO for {city} market

NEXT STEPS:
- Contact business to verify current online presence
- Assess specific needs during consultation
- Propose tailored website solution

Note: This report was generated with limited research capabilities. 
A more detailed analysis would be available with full web access."""


async def draft_proposal(business_name: str, research_summary: str) -> str:
    """
    Write a tailored website proposal for a business based on research.
    Returns a structured proposal with value proposition, sections, and pricing.
    """
    client = AsyncDedalus()
    runner = DedalusRunner(client)

    result = await runner.run(
        input=f"""Write a compelling, tailored website proposal for {business_name}.

Research findings:
{research_summary}

Create a proposal with:
1. **Value Proposition**: Why they need a website (use specific data from research)
2. **Proposed Website Sections**: Home, About, Services, Contact, etc. tailored to their business
3. **Key Features**: Online booking, menu display, gallery, testimonials, etc.
4. **Expected Benefits**: More customers, credibility, 24/7 visibility
5. **Pricing Notes**: Competitive pricing suggestions
6. **Timeline**: Estimated delivery

Make it persuasive, specific to their business, and professional.
Return the full proposal text.""",
        model=DRAFT_MODEL,
        max_steps=3,
    )
    return result.final_output


async def fact_check_proposal(proposal_text: str, business_name: str, research_summary: str) -> str:
    """
    Validate and refine a website proposal for accuracy and persuasiveness.
    Acts as a critic — checks claims, improves weak points, ensures professionalism.
    Returns the refined proposal.
    """
    client = AsyncDedalus()
    runner = DedalusRunner(client)

    result = await runner.run(
        input=f"""You are a proposal reviewer and fact-checker. Review this website proposal for {business_name}:

PROPOSAL:
{proposal_text}

ORIGINAL RESEARCH:
{research_summary}

Check for:
1. Accuracy — are all claims supported by the research?
2. Persuasiveness — is the value proposition compelling?
3. Professionalism — tone, grammar, formatting
4. Missing points — anything important left out?
5. Pricing — is it realistic for a small business?

Return the improved, fact-checked version of the full proposal.""",
        model=CLASSIFIER_MODEL,
        max_steps=3,
    )
    return result.final_output


async def classify_call_outcome(transcript: str, business_name: str) -> str:
    """
    Classify the outcome of a phone call based on its transcript.
    Returns JSON with outcome, confidence, key points, and recommended next action.
    """
    print(f"\\n=== CLASSIFICATION START ===")
    print(f"Business: {business_name}")
    print(f"Transcript length: {len(transcript)} chars")
    print(f"Transcript preview: {transcript[:300]}...")
    print(f"CLASSIFIER_MODEL: {CLASSIFIER_MODEL}")
    print("============================\\n")
    
    try:
        client = AsyncDedalus()
        runner = DedalusRunner(client)
        
        print("Starting classification with DedalusRunner...")
        
        result = await runner.run(
            input=f"""Classify this phone call transcript with {business_name}.

TRANSCRIPT:
{transcript}

Classify the outcome as one of these EXACT values:
- "interested": They showed clear interest in a website
- "agreed_to_email": They want more info via email  
- "not_interested": They declined
- "no_answer": No one picked up or voicemail
- "issue_appeared": Technical issue or hostile response
- "other": None of the above

Return a JSON object with these exact keys:
{{
  "outcome": "<one of the exact values above>",
  "confidence": <number between 0.0 and 1.0>,
  "key_points": ["<point1>", "<point2>"],
  "next_action": "<recommended next step>",
  "summary": "<one-sentence summary>"
}}

Return ONLY the JSON, no other text.""",
            model=CLASSIFIER_MODEL,
            max_steps=3,
        )
        
        print(f"Classification result type: {type(result.final_output)}")
        print(f"Classification result: {result.final_output}")
        
        final_result = result.final_output if isinstance(result.final_output, str) else json.dumps(result.final_output)
        print(f"Final classification: {final_result}")
        
        return final_result
        
    except Exception as e:
        print(f"ERROR in classify_call_outcome: {e}")
        print(f"Error type: {type(e)}")
        import traceback
        traceback.print_exc()
        
        # Return a fallback classification
        fallback_result = {
            "outcome": "other",
            "confidence": 0.1,
            "key_points": ["Classification failed"],
            "next_action": "Manual review needed",
            "summary": f"Classification error: {str(e)}"
        }
        return json.dumps(fallback_result)


# ── API Endpoints ────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "sdr", "timestamp": datetime.utcnow().isoformat()}


@app.post("/run_sdr")
async def run_sdr_endpoint(req: SDRRequest):
    """
    Execute the full SDR pipeline for a business lead.
    Direct sequential execution — NO LLM orchestration.
    Each step is called directly from Python, guaranteeing execution.

    Pipeline: Research → Proposal → Fact-check → Call → Classify → Deck → Email → Save
    """
    session_id = str(uuid.uuid4())
    callback_url = req.callback_url

    # Accumulate step summaries for the final output
    step_results: dict[str, str] = {}
    call_transcript = ""
    call_outcome = "other"
    research_summary = ""
    proposal_content = ""
    email_result = ""
    email_subject = ""

    await notify_ui(callback_url, AgentCallback(
        agent_type=AgentType.SDR,
        event="sdr_started",
        business_name=req.business_name,
        business_id=req.place_id,
        message=f"Starting SDR outreach for {req.business_name}",
    ))

    try:
        # ── STEP 1/8: RESEARCH ──────────────────────────────────
        print("\n" + "=" * 60)
        print("📋 STEP 1/8 — RESEARCH")
        print("=" * 60)
        await notify_ui(callback_url, AgentCallback(
            agent_type=AgentType.SDR,
            event="step_progress",
            business_name=req.business_name,
            message="Step 1/8 — Researching business...",
        ))
        try:
            research_summary = await research_business(
                req.business_name, req.city, req.address or ""
            )
            print(f"✅ STEP 1/8 COMPLETED — Research ({len(research_summary)} chars)")
            step_results["research"] = "completed"
        except Exception as e:
            print(f"❌ STEP 1/8 FAILED — {e}")
            research_summary = f"Research unavailable for {req.business_name} in {req.city}."
            step_results["research"] = f"failed: {e}"

        # ── STEP 2/8: DRAFT PROPOSAL ────────────────────────────
        print("\n" + "=" * 60)
        print("📋 STEP 2/8 — DRAFT PROPOSAL")
        print("=" * 60)
        await notify_ui(callback_url, AgentCallback(
            agent_type=AgentType.SDR,
            event="step_progress",
            business_name=req.business_name,
            message="Step 2/8 — Drafting website proposal...",
        ))
        try:
            proposal_content = await draft_proposal(
                req.business_name, research_summary
            )
            print(f"✅ STEP 2/8 COMPLETED — Proposal ({len(proposal_content)} chars)")
            step_results["proposal"] = "completed"
        except Exception as e:
            print(f"❌ STEP 2/8 FAILED — {e}")
            proposal_content = f"Website proposal for {req.business_name} — details to follow."
            step_results["proposal"] = f"failed: {e}"

        # ── STEP 3/8: FACT-CHECK PROPOSAL ────────────────────────
        print("\n" + "=" * 60)
        print("📋 STEP 3/8 — FACT-CHECK PROPOSAL")
        print("=" * 60)
        await notify_ui(callback_url, AgentCallback(
            agent_type=AgentType.SDR,
            event="step_progress",
            business_name=req.business_name,
            message="Step 3/8 — Fact-checking proposal...",
        ))
        try:
            proposal_content = await fact_check_proposal(
                proposal_content, req.business_name, research_summary
            )
            print(f"✅ STEP 3/8 COMPLETED — Fact-checked ({len(proposal_content)} chars)")
            step_results["fact_check"] = "completed"
        except Exception as e:
            print(f"❌ STEP 3/8 FAILED — {e}")
            step_results["fact_check"] = f"failed: {e}"

        # ── STEP 4/8: PHONE CALL ─────────────────────────────────
        print("\n" + "=" * 60)
        print("📋 STEP 4/8 — PHONE CALL")
        print("=" * 60)
        if req.skip_call:
            print("⏭️  STEP 4/8 SKIPPED — skip_call=True")
            step_results["phone_call"] = "skipped"
        else:
            await notify_ui(callback_url, AgentCallback(
                agent_type=AgentType.SDR,
                event="step_progress",
                business_name=req.business_name,
                message=f"Step 4/8 — Calling {req.phone}...",
            ))
            try:
                call_result_json = await make_phone_call(
                    phone_number=req.phone,
                    business_name=req.business_name,
                    context=research_summary,
                    proposal_summary=proposal_content,
                )
                print(f"📞 Raw call result: {call_result_json[:300]}...")
                call_result = json.loads(call_result_json)
                call_transcript = call_result.get("transcript", "")
                print(f"✅ STEP 4/8 COMPLETED — Call done, transcript {len(call_transcript)} chars")
                # Extract email from transcript immediately
                if call_transcript:
                    found_emails = extract_emails_from_transcript(call_transcript)
                    if found_emails:
                        print(f"📧 Extracted email from transcript: {found_emails[0]}")
                step_results["phone_call"] = "completed"
            except Exception as e:
                print(f"❌ STEP 4/8 FAILED — {e}")
                import traceback
                traceback.print_exc()
                step_results["phone_call"] = f"failed: {e}"

        # ── STEP 5/8: CLASSIFY CALL OUTCOME ──────────────────────
        print("\n" + "=" * 60)
        print("📋 STEP 5/8 — CLASSIFY CALL OUTCOME")
        print("=" * 60)
        if req.skip_call or not call_transcript:
            reason = "skip_call=True" if req.skip_call else "no transcript"
            print(f"⏭️  STEP 5/8 SKIPPED — {reason}")
            call_outcome = "other"
            step_results["classify"] = f"skipped ({reason})"
        else:
            await notify_ui(callback_url, AgentCallback(
                agent_type=AgentType.SDR,
                event="step_progress",
                business_name=req.business_name,
                message="Step 5/8 — Classifying call outcome...",
            ))
            try:
                classification_json = await classify_call_outcome(
                    call_transcript, req.business_name
                )
                print(f"📊 Classification raw: {classification_json[:300]}...")
                # Parse the classification — handle LLM returning markdown-wrapped JSON
                clean_json = classification_json.strip()
                if clean_json.startswith("```"):
                    clean_json = re.sub(r"^```(?:json)?\s*", "", clean_json)
                    clean_json = re.sub(r"\s*```$", "", clean_json)
                classification = json.loads(clean_json)
                call_outcome = classification.get("outcome", "other")
                print(f"✅ STEP 5/8 COMPLETED — Outcome: {call_outcome}")
                step_results["classify"] = f"completed ({call_outcome})"
            except Exception as e:
                print(f"❌ STEP 5/8 FAILED — {e}")
                call_outcome = "other"
                step_results["classify"] = f"failed: {e}"

        # ── STEP 6/8: GENERATE BUSINESS DECK ─────────────────────
        # (moved before email so deck can be attached)
        print("\n" + "=" * 60)
        print("📋 STEP 6/8 — GENERATE BUSINESS DECK")
        print("=" * 60)
        await notify_ui(callback_url, AgentCallback(
            agent_type=AgentType.SDR,
            event="step_progress",
            business_name=req.business_name,
            message="Step 6/8 — Generating business deck...",
        ))
        deck_info = None
        try:
            deck_request = {
                "session_id": session_id,
                "business_name": req.business_name,
                "research_summary": research_summary,
                "call_transcript": call_transcript,
                "call_outcome": call_outcome,
                "contact_email": req.email or FALLBACK_EMAIL,
                "meeting_date": datetime.now().isoformat(),
                "template_style": req.deck_template,
            }
            async with httpx.AsyncClient(timeout=60.0) as http_client:
                resp = await http_client.post(
                    "http://localhost:8086/generate-deck",
                    json=deck_request,
                )
                resp.raise_for_status()
                deck_result = resp.json()

            if deck_result.get("success"):
                deck_info = {
                    "filename": deck_result.get("filename", f"{req.business_name}_Business_Solution.pptx"),
                    "content": deck_result.get("deck_content", {}),
                    "file_data": deck_result.get("deck_file_b64", ""),
                }
                print(f"✅ STEP 6/8 COMPLETED — Deck: {deck_info['filename']}")
                step_results["deck"] = "completed"
            else:
                print(f"❌ STEP 6/8 — Deck generator returned failure: {deck_result.get('error')}")
                step_results["deck"] = f"failed: {deck_result.get('error')}"
        except Exception as e:
            print(f"❌ STEP 6/8 FAILED — {e}")
            step_results["deck"] = f"failed: {e}"

        # ── STEP 7/8: SEND EMAIL ─────────────────────────────────
        print("\n" + "=" * 60)
        print("📋 STEP 7/8 — SEND EMAIL")
        print("=" * 60)
        await notify_ui(callback_url, AgentCallback(
            agent_type=AgentType.SDR,
            event="step_progress",
            business_name=req.business_name,
            message="Step 7/8 — Sending outreach email...",
        ))
        try:
            # Determine email address
            email_to_use = req.email
            if not email_to_use and call_transcript:
                found_emails = extract_emails_from_transcript(call_transcript)
                if found_emails:
                    email_to_use = found_emails[0]
                    print(f"📧 Using email extracted from transcript: {email_to_use}")
            if not email_to_use and FALLBACK_EMAIL:
                email_to_use = FALLBACK_EMAIL
                print(f"⚠️  Using FALLBACK_EMAIL: {email_to_use}")

            if not email_to_use:
                raise ValueError("No email address available")

            email_subject = f"Elevate {req.business_name}'s Online Presence — RapidReach"

            # Build attachment from deck (before email body so we know if we have it)
            attachment_data = None
            if deck_info and deck_info.get("file_data"):
                attachment_data = {
                    "filename": deck_info["filename"],
                    "content_b64": deck_info["file_data"],
                    "mimetype": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                }
                print(f"📎 Attaching deck: {attachment_data['filename']}")

            # Generate calendar invite (before email body so we can reference the date)
            meeting_dt = extract_meeting_time_from_transcript(call_transcript)
            from common.config import SALES_EMAIL as _sales_email
            calendar_ics = generate_ics(
                start=meeting_dt,
                duration_minutes=30,
                summary=f"RapidReach — Follow-up with {req.business_name}",
                description=f"Follow-up discussion about our website proposal for {req.business_name}. Presented by the RapidReach Team.",
                attendee_email=email_to_use,
                organizer_email=_sales_email or "",
            )
            print(f"📅 Calendar invite for {meeting_dt.strftime('%A %B %d at %I:%M %p')}")

            # ── Build professional HTML email ─────────────────
            # Use clean, professional benefit statements (not raw LLM prose)
            _bullet_html = (
                '<li style="margin-bottom:8px;color:#374151;">'
                f'A custom-designed, mobile-responsive website built specifically for {req.business_name}</li>'
                '<li style="margin-bottom:8px;color:#374151;">'
                'Search engine optimization (SEO) to help local customers find you online</li>'
                '<li style="margin-bottom:8px;color:#374151;">'
                'Integrated booking, contact forms, and social media to drive engagement</li>'
                '<li style="margin-bottom:8px;color:#374151;">'
                'Ongoing support, analytics, and performance reporting to track growth</li>'
            )

            _opening = (
                f"Thank you for taking the time to speak with us today about {req.business_name}. "
                "As discussed, we've put together a tailored proposal for your new online presence."
                if call_transcript
                else f"We've been looking into {req.business_name} and we see a great opportunity "
                     "to help you attract more customers with a professional online presence."
            )

            _meeting_str = meeting_dt.strftime("%A, %B %d at %I:%M %p")

            html_body = f"""\
<div style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:600px;margin:0 auto;color:#1f2937;">
  <!-- Header -->
  <div style="background:linear-gradient(135deg,#2563eb,#7c3aed);padding:32px 24px;border-radius:12px 12px 0 0;text-align:center;">
    <h1 style="color:#ffffff;font-size:22px;margin:0 0 4px;">⚡ RapidReach</h1>
    <p style="color:#e0e7ff;font-size:13px;margin:0;">AI-Powered Web Solutions</p>
  </div>

  <!-- Body -->
  <div style="background:#ffffff;padding:28px 24px;border:1px solid #e5e7eb;border-top:none;">
    <p style="font-size:15px;line-height:1.6;color:#374151;">Hi {req.business_name} Team,</p>

    <p style="font-size:15px;line-height:1.6;color:#374151;">{_opening}</p>

    <h2 style="font-size:16px;color:#2563eb;margin:24px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px;">What We're Proposing</h2>
    <ul style="padding-left:20px;font-size:14px;line-height:1.8;">
      {_bullet_html}
    </ul>

    <!-- CTA -->
    <div style="text-align:center;margin:28px 0;">
      <a href="mailto:{_sales_email or ''}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 28px;border-radius:6px;font-weight:600;font-size:14px;">Schedule a Consultation</a>
    </div>

    <p style="font-size:14px;color:#6b7280;line-height:1.6;">
      📎 We've attached a detailed presentation deck for your review.<br>
      📅 A calendar invite has been included for <strong>{_meeting_str}</strong> so we can walk you through everything.
    </p>

    <p style="font-size:15px;line-height:1.6;color:#374151;margin-top:24px;">
      Looking forward to helping {req.business_name} grow!
    </p>
  </div>

  <!-- Footer -->
  <div style="background:#f9fafb;padding:20px 24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 12px 12px;text-align:center;">
    <p style="font-size:14px;color:#374151;margin:0 0 4px;"><strong>The RapidReach Team</strong></p>
    <p style="font-size:12px;color:#9ca3af;margin:0;">AI-Powered Sales Development &bull; rapidreach.ai</p>
  </div>
</div>"""

            email_result = await send_email(
                to_email=email_to_use,
                subject=email_subject,
                html_body=html_body,
                business_name=req.business_name,
                attachment_data=attachment_data,
                calendar_ics=calendar_ics,
            )
            print(f"✅ STEP 7/8 COMPLETED — Email sent to {email_to_use}")
            step_results["email"] = f"completed (to {email_to_use})"
        except Exception as e:
            print(f"❌ STEP 7/8 FAILED — {e}")
            import traceback
            traceback.print_exc()
            email_result = json.dumps({"success": False, "error": str(e)})
            step_results["email"] = f"failed: {e}"

        # ── STEP 8/8: SAVE SESSION ───────────────────────────────
        print("\n" + "=" * 60)
        print("📋 STEP 8/8 — SAVE SESSION")
        print("=" * 60)
        await notify_ui(callback_url, AgentCallback(
            agent_type=AgentType.SDR,
            event="step_progress",
            business_name=req.business_name,
            message="Step 8/8 — Saving session to database...",
        ))
        try:
            email_sent = False
            if email_result:
                try:
                    email_data = json.loads(email_result)
                    email_sent = email_data.get("success", False)
                except Exception:
                    email_sent = "success" in email_result.lower()

            session_data = {
                "session_id": session_id,
                "lead_place_id": req.place_id,
                "business_name": req.business_name,
                "research_summary": research_summary[:5000],
                "proposal_summary": proposal_content[:5000],
                "call_transcript": call_transcript[:5000],
                "call_outcome": call_outcome,
                "email_sent": email_sent,
                "email_subject": email_subject,
                "created_at": datetime.utcnow().isoformat(),
            }
            sdr_sessions[session_id] = SDRResult(**session_data)
            if deck_info:
                sd = sdr_sessions[session_id].dict()
                sd["deck_info"] = deck_info
                sdr_sessions[session_id] = SDRResult(**sd)

            save_result = save_sdr_session(session_data)
            print(f"✅ STEP 8/8 COMPLETED — Session saved ({save_result})")
            step_results["save"] = "completed"
        except Exception as e:
            print(f"❌ STEP 8/8 FAILED — {e}")
            import traceback
            traceback.print_exc()
            step_results["save"] = f"failed: {e}"

        # ── FINAL SUMMARY ────────────────────────────────────────
        print("\n" + "=" * 60)
        print("🏁 SDR PIPELINE COMPLETE")
        print("=" * 60)
        for step_name, status in step_results.items():
            icon = "✅" if "completed" in status else ("⏭️" if "skipped" in status else "❌")
            print(f"  {icon} {step_name}: {status}")
        print("=" * 60 + "\n")

        final_output = f"""SDR Pipeline completed for {req.business_name}

Step Results:
""" + "\n".join(
            f"  {'✅' if 'completed' in s else ('⏭️' if 'skipped' in s else '❌')} {n}: {s}"
            for n, s in step_results.items()
        )

        # Notify UI: completed
        await notify_ui(callback_url, AgentCallback(
            agent_type=AgentType.SDR,
            event="sdr_completed",
            business_name=req.business_name,
            business_id=req.place_id,
            message=f"SDR outreach completed for {req.business_name}",
            data={
                "session_id": session_id,
                "summary": final_output[:1000] if final_output else "",
                "step_results": step_results,
            },
        ))

        # Update lead status in BQ
        if req.place_id:
            update_lead_status(req.place_id, "contacted")

        return {
            "status": "success",
            "session_id": session_id,
            "business_name": req.business_name,
            "step_results": step_results,
            "summary": final_output,
        }

    except Exception as e:
        logger.error(f"SDR pipeline failed for {req.business_name}: {e}")
        await notify_ui(callback_url, AgentCallback(
            agent_type=AgentType.SDR,
            event="error",
            business_name=req.business_name,
            message=f"SDR failed: {str(e)}",
        ))
        return {"status": "error", "message": str(e)}


@app.get("/api/sessions")
async def get_sessions():
    """Return ALL SDR sessions — merged from BigQuery history + in-memory."""
    sessions = {}

    # 1) Load historical sessions from BigQuery first
    try:
        from sdr.tools.bigquery_utils import _get_client
        from common.config import GOOGLE_CLOUD_PROJECT, BIGQUERY_DATASET, BIGQUERY_SDR_SESSIONS_TABLE
        client = _get_client()
        if client:
            table_ref = f"{GOOGLE_CLOUD_PROJECT}.{BIGQUERY_DATASET}.{BIGQUERY_SDR_SESSIONS_TABLE}"
            query = f"SELECT * FROM `{table_ref}` ORDER BY created_at DESC LIMIT 50"
            rows = client.query(query).result()
            for row in rows:
                row_dict = dict(row)
                sid = row_dict.get("session_id", "")
                # Convert any non-serializable types
                for k, v in row_dict.items():
                    if hasattr(v, 'isoformat'):
                        row_dict[k] = v.isoformat()
                sessions[sid] = row_dict
    except Exception as e:
        logger.warning(f"BigQuery session fetch failed: {e}")

    # 2) Overlay in-memory sessions (freshest state wins)
    for k, v in sdr_sessions.items():
        sessions[k] = v.model_dump()

    return {"sessions": sessions}
