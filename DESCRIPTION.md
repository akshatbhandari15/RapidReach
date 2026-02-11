# 🚀 RapidReach Hackathon Journey

## Inspiration

Picture this: You're a barber who's been perfecting fades for 15 years. Or a nail technician whose clients drive across town just for you. Maybe you run a family plumbing business that's never left a job unfinished, or a small restaurant where every dish is made from your grandmother's recipe. You pour your heart into your craft every single day.

But here's the problem — the franchise down the street has a marketing team, a $5,000/month ad budget, SEO consultants, and a slick website that ranks on page one of Google. You? You've got a phone number on a hand-painted sign and maybe a Facebook page your nephew set up three years ago. You're not less talented. You're not less passionate. You just don't have the resources, the tech know-how, or the time to figure out performance marketing, CRM software, and digital outreach while also *running your business*.

This isn't a hypothetical. **There are millions of small businesses exactly like this** — skilled, passionate, deeply rooted in their communities — that have been around for years but have never had the tools to grow to their full potential. They can't afford a sales team. They can't afford a marketing agency. And the gap between them and their better-funded competitors widens every year.

That's the injustice that sparked RapidReach.

We asked ourselves: **What if the same AI that powers billion-dollar enterprises could work for the barber on the corner?** What if a small business owner could press a single button and have an intelligent system find them new customers, research their market, call prospects with a real voice, send professional proposals, and book meetings on their calendar — all while they focus on what they do best?

RapidReach was built to level the playing field. Not by dumbing things down, but by putting world-class AI sales automation — powered by Dedalus Labs, ElevenLabs, and Google Cloud — into the hands of the people who need it most: the small business owners who have the passion but never had the platform.

---

## What It Does

RapidReach is a **fully autonomous AI-powered Sales Development Representative (SDR)** that automates the entire outbound sales lifecycle — from finding a prospect to booking a meeting on your calendar. The system:

**Finds Leads** — Automatically discovers businesses without websites in any target city using Google Maps Places API, filtering out chains and duplicates, paginating through up to 60 results per search.

**Researches Prospects** — Conducts deep business analysis via Brave Search MCP, gathering reviews, competitor landscape, pain points, and opportunities to craft truly informed outreach.

**Generates Proposals** — Creates personalized, fact-checked website development proposals using a generator-critic AI pattern — one model drafts, another validates claims before anything goes out.

**Makes AI Phone Calls** — Places natural-sounding outbound phone calls using ElevenLabs Conversational AI, then classifies the call outcome (interested, wants an email, not interested) via structured LLM output.

**Sends Professional Emails** — Based on the call, it schedules meetings and also sends information by delivering branded HTML emails — all through the Gmail API.

**Creates Pitch Deck** — Based on the conversation, the assistant generates a pitch deck and attaches it to the outreach email along with a calendar invite (.ics).

**Books Meetings Automatically** — Monitors inbox for prospect replies via Google Cloud Pub/Sub, detects meeting requests with LLM analysis, checks your calendar availability, and creates Google Calendar events with Google Meet links.

**Real-Time Dashboard** — A live WebSocket-powered dashboard shows leads discovered, calls made, emails sent, and meetings booked — all updating in real time as agents work.

---

## How We Built It

RapidReach is not a single script — it's a system of **8 specialized AI agents** orchestrated across **6 FastAPI microservices**, all working together through a carefully designed pipeline.

### Architecture

We built a **multi-agent microservices architecture** where each service owns a distinct piece of the sales pipeline:

| Service | What It Does |
|---------|-------------|
| **Lead Finder** (port 8081) | Google Maps discovery → deduplication → BigQuery storage |
| **SDR Agent** (port 8084) | Full outreach pipeline: research → proposal → fact-check → call → classify → email + deck |
| **Deck Generator** (port 8086) | AI-powered PowerPoint generation with `python-pptx` |
| **Lead Manager** (port 8082) | Inbound email analysis → calendar availability → Google Meet booking |
| **Gmail Listener** (port 8083) | Pub/Sub subscriber (with polling fallback) for real-time email notifications |
| **UI Client** (port 8000) | Dashboard with WebSocket streaming, workflow triggers, and live stats |

### The Agent Stack

Every AI-powered service uses **Dedalus Labs ADK** (`DedalusRunner` + `LlmAgent`) as the orchestration layer. We chose Dedalus ADK because it gave us:

- **Agent-as-tool patterns** — sub-agents (Research, Proposal, Fact-Check, Classifier) are invoked as tools by the SDR coordinator
- **MCP server integration** — Brave Search and Google Search run as MCP servers, giving agents web research capability
- **Structured output** — Pydantic models enforce schema on LLM responses (call classification, email analysis)
- **Lifecycle hooks** — `before_agent`, `after_agent`, `before_tool`, `after_tool` for granular control

### Model Specialization

We didn't use a single model for everything. We matched models to tasks:

| Task | Model | Why |
|------|-------|-----|
| Coordination, research, classification | **OpenAI GPT-4.1** | Fast, reliable, strong at structured reasoning |
| Proposal drafting, creative writing | **Anthropic Claude Sonnet 4** | Superior prose quality, persuasive business writing |
| Phone conversations | **ElevenLabs Conversational AI** | Natural-sounding real-time voice interactions |

### Key Design Patterns

- **Generator-Critic** — The proposal agent drafts, then a separate fact-check agent validates claims before the email goes out
- **Sequential Pipeline** — The SDR runs an 8-step pipeline in direct Python (not LLM-orchestrated), calling agent-as-tool functions at each step for reliability
- **Callback Broadcasting** — Every agent streams real-time status updates to the dashboard via WebSocket callbacks
- **Merge-Not-Replace** — Sessions and leads are merged (not overwritten) between in-memory state and BigQuery persistence
- **Fallback Chains** — MCP research tries Brave Search → Google Search → graceful degradation, Maps search has a mock fallback for local dev

### Technology Stack

- **Dedalus Labs ADK** — Agent orchestration, DedalusRunner, LlmAgent, agent-as-tool using Brave Search MCP
- **OpenAI GPT-4.1 + Anthropic Claude Sonnet 4** — LLM backbone
- **ElevenLabs** — Conversational AI phone calls with batch API + transcript polling
- **Google Cloud BigQuery** — Persistent storage for leads, sessions, and meetings
- **Google Maps Places API** — Business discovery with pagination
- **Gmail API** — Sending branded HTML emails with MIME attachments
- **Google Calendar API + Google Meet** — Automated meeting scheduling
- **Google Cloud Pub/Sub** — Real-time inbox monitoring
- **FastAPI + WebSockets** — Service framework with real-time streaming
- **python-pptx** — PowerPoint deck generation
- **Pydantic v2** — Data validation and structured LLM output

---

## Challenges We Ran Into

### Orchestrating 8 Agents Across 6 Services

The hardest part wasn't building a single agent — it was making 8 agents across 6 independent services work together as one coherent system. State needed to flow from the Lead Finder to the SDR to the Deck Generator to the Email tool, and eventually loop back through the Gmail Listener to the Lead Manager. Getting the data contracts right (Pydantic models shared via a `common/` package) took multiple iterations.

### ElevenLabs Phone Call Integration

The ElevenLabs Conversational AI batch calling API was powerful but challenging. Calls are asynchronous — you fire off a request and then have to poll for the transcript. We had to build retry logic with exponential backoff, handle cases where calls went to voicemail, and parse dictated email addresses from transcripts ("a-r-n-a-v at gmail dot com" → `arnav@gmail.com`). Getting the AI voice to sound natural while staying on-script required extensive prompt engineering in the ElevenLabs agent configuration.

### Generator-Critic Pattern at Scale

We loved the idea of having one LLM write proposals and another fact-check them, but making the critic agent actually *useful* (not just rubber-stamping everything) required careful prompt design. Too strict and every proposal got rewritten; too lenient and hallucinated claims slipped through. We iterated on the fact-check prompt many times to find the right balance.

### Real-Time State Synchronization

The dashboard needed to show live updates from agents running across 6 services. We built a WebSocket callback system where every agent posts status updates to the UI Client, which broadcasts them to connected browsers. Getting the merge logic right — so that refreshing the page shows both historical BigQuery data *and* live in-memory sessions without duplicates — was a subtle bug that took significant debugging.

### Gmail API & Pub/Sub Reliability

Google's Pub/Sub push notifications for Gmail required setting up domain verification, webhook endpoints, and handling message deduplication. In local development, push doesn't work, so we built a polling fallback that checks the inbox every 30 seconds. Managing OAuth2 service account credentials across all Google services (Maps, Gmail, Calendar, BigQuery, Pub/Sub) added another layer of complexity.

---

## Accomplishments That We're Proud Of

### Technical Achievements

- **End-to-end autonomous pipeline** — A single "Run SDR" click triggers research, proposal generation, fact-checking, an AI phone call, call classification, deck generation, and email delivery — all without human intervention
- **Real AI phone calls** — Our system places actual phone calls to real businesses with natural-sounding AI voice conversations via ElevenLabs
- **Generator-critic quality control** — Proposals are fact-checked by a separate AI agent before being sent, catching hallucinated claims
- **6 microservices running in concert** — Each service is independently deployable with its own FastAPI app, connected through HTTP APIs and WebSocket callbacks
- **Smart meeting booking** — The system detects meeting requests in email replies and automatically creates Google Calendar events with Google Meet links

### Business Impact

- **Solves a real problem** — We built this for a real use case we witnessed: a salesperson spending 8+ hours to find 2 leads, replaced by a system that processes dozens in minutes
- **Full-funnel automation** — From lead discovery to meeting booking, every manual step in the SDR process is automated
- **Professional output** — Branded HTML emails with PowerPoint decks and calendar invites look like they came from a real sales team, not a bot

### User Experience

- **Dark-theme dashboard** — Real-time WebSocket-powered UI with live stats, lead tables, and session cards
- **One-click workflows** — Find leads, run outreach, and process inbox are each a single button click
- **Transparency** — Every agent step is streamed to the dashboard so you can watch the AI work in real time

---

## What We Learned

Building RapidReach was a crash course in modern AI engineering. We came in knowing basic LLM APIs and walked out understanding how to architect complex multi-agent systems.

### Dedalus Labs ADK

We'd never built a multi-agent system before, and Dedalus ADK was our framework of choice. We learned how `DedalusRunner` handles agent lifecycle, how to use `LlmAgent` with structured Pydantic output schemas, and — most importantly — the **agent-as-tool pattern**, where one agent can call another agent as if it were a tool function. This pattern was the backbone of our SDR pipeline.

### Multi-Model Architecture

We learned that not all LLM tasks are created equal. GPT-4.1 excels at structured reasoning and classification, while Claude Sonnet 4 writes more compelling, human-sounding proposals. Using the right model for each task — rather than forcing one model to do everything — dramatically improved output quality.

### MCP (Model Context Protocol)

Integrating Brave Search and Google Search as MCP servers taught us how to give agents access to real-time web data. The MCP standard made it easy to swap research providers and build fallback chains without changing agent code.

### Google Cloud Ecosystem

We went deep into Google Cloud: BigQuery for persistence, Maps Places API for lead discovery, Gmail API for email (both sending and reading), Calendar API for meeting scheduling, Google Meet for video links, and Pub/Sub for real-time notifications. Learning how all these services connect through a single service account and work together was immensely valuable.

### Voice AI

Integrating ElevenLabs taught us that voice AI is a different beast from text AI. You have to think about conversation flow, interruption handling, and the fact that the AI needs to extract structured data (like email addresses) from spoken, often imprecise, language. Building the transcript parser to handle dictated emails was a unique challenge we hadn't anticipated.

### Microservices in Practice

Running 6 services locally with different ports, shared models, and cross-service HTTP calls taught us the practical realities of distributed systems — service discovery, health checks, shared configuration, and the importance of a common data model (`common/models.py`).

---

## What's Next for RapidReach

### Immediate Enhancements

- **Industry Expansion** — Extend beyond website sales to target restaurants needing online ordering, retail stores needing e-commerce, and service businesses needing booking systems
- **Multi-Language Outreach** — Add localization so AI calls and emails can target non-English-speaking markets
- **Advanced Analytics** — ML-powered conversion prediction based on lead characteristics and call outcomes
- **CRM Integration** — Connect with Salesforce, HubSpot, and Pipedrive for teams already using CRM tools

### Long-Term Vision

- **Vertical Specialization** — Adapt the agent pipeline for different industries: legal, healthcare, consulting, real estate
- **AI-Powered Negotiation** — Train negotiation agents that can handle pricing discussions and objection handling on calls
- **Predictive Lead Scoring** — Use historical conversion data to rank leads before outreach begins
- **Team Features** — Multi-user dashboards, role-based access, territory management, and performance analytics

### Platform Evolution

- **Agent Marketplace** — Let users create, share, and sell custom outreach agents tuned for specific industries
- **No-Code Agent Builder** — A visual editor for non-technical users to configure their own SDR pipeline
- **Integration Ecosystem** — Connect with payment processors, proposal tools, and marketing platforms for end-to-end business automation

---

## Built With

- Dedalus Labs ADK
- OpenAI GPT-4.1
- Anthropic Claude Sonnet 4
- ElevenLabs Conversational AI
- Google Cloud BigQuery
- Google Maps Places API
- Gmail API
- Google Calendar API
- Google Meet
- Google Cloud Pub/Sub
- Brave Search MCP
- FastAPI
- WebSockets
- Python
- python-pptx
- Pydantic

---

> 🚀 *Built with passion at Columbia ADI DevFest 2026 by Columbians.*
