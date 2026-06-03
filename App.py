"""
Demand Creation Chatbot — Agentic Version (Fixed)
==================================================
Fixes applied:
  1. Removed time.sleep() from gemini_generate — was killing Gunicorn workers on 429
  2. Agentic follow-up nodes now fail gracefully on 429/any error (skip, don't block)
  3. AI Summariser removed from /chat critical path — no extra Gemini call at completion
  4. gemini_generate raises immediately on 429 so caller can skip cleanly
  5. All agent nodes wrapped in try/except that returns state unchanged on failure
"""

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, render_template_string, send_file
import os, json, re
from io import BytesIO
from datetime import datetime
from google import genai
import sqlalchemy as sa

from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated, List, Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table,
    TableStyle, HRFlowable, KeepTogether
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER

# =========================================================
# CONFIGURATION
# =========================================================

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY must be set")

client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL = "gemini-2.5-flash"

# =========================================================
# GEMINI CALL — NO SLEEP, FAIL FAST
# Key fix: removed time.sleep() which was blocking Gunicorn
# workers for 60s+ causing SIGKILL worker timeouts.
# Callers handle 429 gracefully by skipping the agentic step.
# =========================================================

def gemini_generate(prompt: str) -> str:
    """
    Single Gemini call. Raises immediately on any error including 429.
    NO sleep — sleeping inside a sync Gunicorn worker causes SIGKILL.
    All callers must handle exceptions and degrade gracefully.
    """
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return response.text

# =========================================================
# AZURE SQL  (unchanged)
# =========================================================

AZURE_SQL_SERVER   = (os.getenv("AZURE_SQL_SERVER")   or "").strip()
AZURE_SQL_DATABASE = (os.getenv("AZURE_SQL_DATABASE") or "").strip()
AZURE_SQL_USERNAME = (os.getenv("AZURE_SQL_USERNAME") or "").strip()
AZURE_SQL_PASSWORD = (os.getenv("AZURE_SQL_PASSWORD") or "").strip()

_db_engine = None

def get_db_engine():
    global _db_engine
    if _db_engine is not None:
        return _db_engine
    if not all([AZURE_SQL_SERVER, AZURE_SQL_DATABASE, AZURE_SQL_USERNAME, AZURE_SQL_PASSWORD]):
        return None
    from urllib.parse import quote_plus
    pwd = quote_plus(AZURE_SQL_PASSWORD)
    url = f"mssql+pymssql://{AZURE_SQL_USERNAME}:{pwd}@{AZURE_SQL_SERVER}/{AZURE_SQL_DATABASE}"
    _db_engine = sa.create_engine(url, pool_pre_ping=True, pool_recycle=300,
                                   connect_args={"login_timeout": 30})
    print("[DB] SQLAlchemy engine created (pymssql)")
    return _db_engine


def ensure_table_exists():
    create_sql = sa.text("""
    IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='DemandRequests' AND xtype='U')
    CREATE TABLE DemandRequests (
        id                        INT IDENTITY(1,1) PRIMARY KEY,
        Demand_ID                 NVARCHAR(50),
        Demand_Timestamp          NVARCHAR(100),
        Demand_Status             NVARCHAR(50),
        Submission_Type           NVARCHAR(50),
        Demand_Route              NVARCHAR(200),
        Demand_Title              NVARCHAR(500),
        Requirement_Summary       NVARCHAR(MAX),
        Rationale_And_Purpose     NVARCHAR(MAX),
        Acceptance_Criteria       NVARCHAR(MAX),
        Business_Benefits         NVARCHAR(MAX),
        Business_Process          NVARCHAR(200),
        E2E_Process_In_GPM        NVARCHAR(200),
        Application               NVARCHAR(200),
        Landscape_Impacted        NVARCHAR(200),
        Area                      NVARCHAR(200),
        Requesting_Countries      NVARCHAR(200),
        Requesting_Regions        NVARCHAR(200),
        Bill2Country              NVARCHAR(200),
        Target_Go_Live_Date       NVARCHAR(100),
        Risks_If_Not_Implemented  NVARCHAR(MAX),
        Month_End_Dependency      NVARCHAR(100),
        Legal_Fiscal_Change       NVARCHAR(100),
        Audit_Requirement         NVARCHAR(100),
        GTS_Impact                NVARCHAR(100),
        Impacted_Business_Groups  NVARCHAR(200),
        Requesting_BU             NVARCHAR(200),
        Potential_Savings         NVARCHAR(200),
        Agent_Followup_Log        NVARCHAR(MAX),
        Created_At                DATETIME DEFAULT GETDATE()
    )
    """)
    try:
        engine = get_db_engine()
        if engine is None:
            return
        with engine.begin() as conn:
            conn.execute(create_sql)
        print("[DB] Table DemandRequests ready")
    except Exception as e:
        print(f"[DB] Table creation error: {type(e).__name__}: {e}")


def save_to_db(payload: dict) -> bool:
    engine = get_db_engine()
    if engine is None:
        print("[DB] Azure SQL credentials not configured — skipping save")
        return False
    try:
        ensure_table_exists()
        insert_sql = sa.text("""
        INSERT INTO DemandRequests (
            Demand_ID, Demand_Timestamp, Demand_Status, Submission_Type,
            Demand_Route, Demand_Title, Requirement_Summary, Rationale_And_Purpose,
            Acceptance_Criteria, Business_Benefits, Business_Process, E2E_Process_In_GPM,
            Application, Landscape_Impacted, Area, Requesting_Countries,
            Requesting_Regions, Bill2Country, Target_Go_Live_Date,
            Risks_If_Not_Implemented, Month_End_Dependency, Legal_Fiscal_Change,
            Audit_Requirement, GTS_Impact, Impacted_Business_Groups,
            Requesting_BU, Potential_Savings, Agent_Followup_Log
        ) VALUES (
            :demand_id, :demand_timestamp, :demand_status, :submission_type,
            :demand_route, :demand_title, :requirement_summary, :rationale_and_purpose,
            :acceptance_criteria, :business_benefits, :business_process, :e2e_process,
            :application, :landscape_impacted, :area, :requesting_countries,
            :requesting_regions, :bill2country, :target_go_live_date,
            :risks, :month_end_dependency, :legal_fiscal_change,
            :audit_requirement, :gts_impact, :impacted_business_groups,
            :requesting_bu, :potential_savings, :agent_followup_log
        )
        """)
        values = {
            "demand_id":                payload.get("Demand ID", ""),
            "demand_timestamp":         payload.get("Demand Timestamp", ""),
            "demand_status":            payload.get("Demand Status", "New"),
            "submission_type":          payload.get("SubmissionType", "Chatbot"),
            "demand_route":             payload.get("Demand_route", ""),
            "demand_title":             payload.get("Demand Title", ""),
            "requirement_summary":      payload.get("Requirement summary", ""),
            "rationale_and_purpose":    payload.get("Rationale and Purpose", ""),
            "acceptance_criteria":      payload.get("Acceptance Criteria", ""),
            "business_benefits":        payload.get("Business Benefits", ""),
            "business_process":         payload.get("Business_Process", ""),
            "e2e_process":              payload.get("E2E process in GPM", ""),
            "application":              payload.get("Application", ""),
            "landscape_impacted":       payload.get("Landscape Impacted", ""),
            "area":                     payload.get("Area", ""),
            "requesting_countries":     payload.get("Requesting Countries", ""),
            "requesting_regions":       payload.get("Requesting Regions", ""),
            "bill2country":             payload.get("Bill2Country", ""),
            "target_go_live_date":      payload.get("Target Go Live Date", ""),
            "risks":                    payload.get("Risks if not implemented on Target date", ""),
            "month_end_dependency":     payload.get("Does this have any month-end (/Year-end) dependency?", ""),
            "legal_fiscal_change":      payload.get("Is this a legal/fiscal change?", ""),
            "audit_requirement":        payload.get("Is Audit requirement", ""),
            "gts_impact":               payload.get("GTS Impact", ""),
            "impacted_business_groups": payload.get("Impacted business groups", ""),
            "requesting_bu":            payload.get("Requesting BU", ""),
            "potential_savings":        payload.get("Potential_savings", ""),
            "agent_followup_log":       json.dumps(payload.get("_agent_followup_log", [])),
        }
        with engine.begin() as conn:
            conn.execute(insert_sql, values)
        print(f"[DB] Saved: {payload.get('Demand ID')}")
        return True
    except Exception as e:
        print(f"[DB] Failed: {type(e).__name__}: {e}")
        return False


# =========================================================
# MANDATORY FIELDS
# =========================================================

MANDATORY_FIELDS = [
    {"key": "Demand Title",                                         "question": "What is the Demand Title?"},
    {"key": "Requirement summary",                                  "question": "Please provide a Requirement Summary."},
    {"key": "Rationale and Purpose",                                "question": "What is the Rationale and Purpose of this demand?"},
    {"key": "Acceptance Criteria",                                  "question": "What are the Acceptance Criteria?"},
    {"key": "Business Benefits",                                    "question": "What are the Business Benefits?"},
    {"key": "Business_Process",                                     "question": "What is the Business Process involved?"},
    {"key": "E2E process in GPM",                                   "question": "What is the E2E Process in GPM?"},
    {"key": "Area",                                                 "question": "What is the Area / Geographic Location? (e.g. Cairo, Maharashtra, Europe)"},
    {"key": "Application",                                          "question": "Which Application(s) are Impacted? (e.g. ECC, Fusion, SAP)"},
    {"key": "Target Go Live Date",                                  "question": "What is the Target Go Live Date? (e.g. 30/06/2026)"},
    {"key": "Risks if not implemented on Target date",              "question": "What are the Risks if not implemented on the Target Date?"},
    {"key": "Does this have any month-end (/Year-end) dependency?", "question": "Does this have any Month-end / Year-end Dependency? (Yes / No)"},
    {"key": "Is this a legal/fiscal change?",                       "question": "Is this a Legal / Fiscal Change? (Yes / No)"},
    {"key": "Is Audit requirement",                                 "question": "Is there an Audit Requirement? (Yes / No)"},
    {"key": "GTS Impact",                                           "question": "Is there a GTS Impact? (Yes / No)"},
    {"key": "Impacted business groups",                             "question": "Which Business Groups are Impacted? (e.g. Personal Care, Foods)"},
    {"key": "Landscape Impacted",                                   "question": "Which Landscape is Impacted? (e.g. Fusion, ECC, Both)"},
    {"key": "Requesting BU",                                        "question": "What is the Requesting Business Unit (BU)?"},
    {"key": "Potential_savings",                                    "question": "What are the Potential Savings? (enter amount or describe)"},
]

# =========================================================
# LANGGRAPH — AGENT STATE
# =========================================================

class AgentState(TypedDict):
    captured:         dict
    completed:        list
    current_field:    str
    current_question: str
    in_followup:      bool
    followup_parent:  str
    followup_count:   int
    agent_insight:    str
    followup_log:     list
    is_complete:      bool
    final_payload:    dict


# =========================================================
# LANGGRAPH — AGENT NODES
# All nodes are fail-safe: on ANY exception (incl. 429),
# they return state unchanged so the conversation continues.
# =========================================================

def node_analyse_and_route(state: AgentState) -> AgentState:
    """Decide if a dynamic follow-up is needed for the last answered field."""
    completed = state.get("completed", [])
    if not completed:
        return state

    last_key = completed[-1]
    needs_followup = (
        last_key in ("Application", "Business Benefits")
        and state.get("followup_count", 0) == 0
    )
    new_state = dict(state)
    new_state["in_followup"]     = needs_followup
    new_state["followup_parent"] = last_key if needs_followup else ""
    return new_state


def node_dynamic_application_followup(state: AgentState) -> AgentState:
    """
    Generate an application-specific follow-up question.
    FAIL-SAFE: if Gemini is unavailable (429 or any error),
    skips the follow-up and continues normally. No blocking.
    """
    application_value = state["captured"].get("Application", "")
    captured_context = {k: v for k, v in state["captured"].items()
                        if k in ["Demand Title", "Requirement summary", "Business Benefits"]}
    prompt = f"""You are an SAP/enterprise systems analyst capturing a demand request.
Application(s) impacted: "{application_value}"
Context: {json.dumps(captured_context)}

Generate ONE focused clarifying question about the specific modules/components within those applications.
- ECC → ask about modules: FI, CO, MM, SD, PP, HR
- Fusion/Oracle → ask about cloud: HCM, Finance, SCM, CX
- S/4HANA → ask about area: Finance, Logistics, Manufacturing
- Multiple apps → ask which has PRIMARY impact

Return ONLY this JSON (no markdown):
{{"followup_question": "<specific question>", "agent_insight": "<1 sentence insight>", "applications_detected": ["<app1>"]}}"""

    try:
        raw = gemini_generate(prompt).strip()
        raw = re.sub(r"```[a-zA-Z]*", "", raw).replace("```", "").strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        parsed = json.loads(match.group() if match else raw)

        followup_q = parsed.get("followup_question", "").strip()
        insight    = parsed.get("agent_insight", "").strip()
        apps       = parsed.get("applications_detected", [])

        if not followup_q:
            raise ValueError("Empty follow-up question")

        log_entry = {
            "type": "application_followup",
            "trigger_field": "Application",
            "trigger_value": application_value,
            "apps_detected": apps,
            "followup_question": followup_q,
            "agent_insight": insight,
        }
        new_state = dict(state)
        new_state["current_question"] = followup_q
        new_state["agent_insight"]    = insight
        new_state["followup_count"]   = 1
        new_state["followup_log"]     = state.get("followup_log", []) + [log_entry]
        print(f"[AGENT] App follow-up: {followup_q[:80]}")
        return new_state

    except Exception as e:
        # 429 or any error → skip follow-up, continue conversation normally
        print(f"[AGENT] App followup skipped ({type(e).__name__}) — continuing without follow-up")
        new_state = dict(state)
        new_state["in_followup"] = False
        return new_state


def node_dynamic_benefits_followup(state: AgentState) -> AgentState:
    """
    Classify business benefit type and generate a targeted follow-up.
    FAIL-SAFE: skips cleanly on any error including 429.
    """
    benefits_value = state["captured"].get("Business Benefits", "")
    captured_context = {k: v for k, v in state["captured"].items()
                        if k in ["Demand Title", "Requirement summary", "Rationale and Purpose"]}
    prompt = f"""You are a business analyst capturing a formal IT demand.
Business Benefits stated: "{benefits_value}"
Context: {json.dumps(captured_context)}

1. Classify PRIMARY benefit: compliance | revenue_growth | cost_efficiency | risk_mitigation | market_expansion | operational_excellence | customer_experience
2. Generate ONE targeted follow-up question to quantify/clarify the benefit:
   - compliance → what regulation/standard? (GDPR, SOX, local tax)
   - revenue_growth → projected revenue impact or target metric?
   - cost_efficiency → estimated annual savings or headcount reduction?
   - risk_mitigation → what specific risk is being reduced?
   - market_expansion → which new markets/geographies?
   - operational_excellence → which KPI improves and by how much?
   - customer_experience → which touchpoint or NPS metric improves?

Return ONLY this JSON (no markdown):
{{"benefit_category": "<category>", "followup_question": "<question>", "agent_insight": "<1 sentence>"}}"""

    try:
        raw = gemini_generate(prompt).strip()
        raw = re.sub(r"```[a-zA-Z]*", "", raw).replace("```", "").strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        parsed = json.loads(match.group() if match else raw)

        benefit_cat = parsed.get("benefit_category", "operational_excellence").strip()
        followup_q  = parsed.get("followup_question", "").strip()
        insight     = parsed.get("agent_insight", "").strip()

        if not followup_q:
            raise ValueError("Empty follow-up question")

        log_entry = {
            "type": "benefits_followup",
            "trigger_field": "Business Benefits",
            "trigger_value": benefits_value,
            "benefit_category": benefit_cat,
            "followup_question": followup_q,
            "agent_insight": insight,
        }
        new_state = dict(state)
        new_state["current_question"] = followup_q
        new_state["agent_insight"]    = insight
        new_state["followup_count"]   = 1
        new_state["followup_log"]     = state.get("followup_log", []) + [log_entry]
        print(f"[AGENT] Benefits follow-up ({benefit_cat}): {followup_q[:80]}")
        return new_state

    except Exception as e:
        print(f"[AGENT] Benefits followup skipped ({type(e).__name__}) — continuing without follow-up")
        new_state = dict(state)
        new_state["in_followup"] = False
        return new_state


def node_capture_followup_answer(state: AgentState, user_answer: str) -> AgentState:
    """Merge the follow-up answer back into the parent field value."""
    parent_field = state.get("followup_parent", "")
    existing     = state["captured"].get(parent_field, "")
    enriched     = f"{existing} | Detail: {user_answer}"

    new_state = dict(state)
    new_state["captured"] = dict(state["captured"])
    new_state["captured"][parent_field] = enriched
    new_state["in_followup"]     = False
    new_state["followup_count"]  = 0
    new_state["followup_parent"] = ""
    new_state["agent_insight"]   = ""

    log = list(new_state.get("followup_log", []))
    if log:
        log[-1] = dict(log[-1])
        log[-1]["user_followup_answer"] = user_answer
    new_state["followup_log"] = log
    print(f"[AGENT] Follow-up answer captured for '{parent_field}'")
    return new_state


# =========================================================
# LANGGRAPH — GRAPH
# =========================================================

def build_agent_graph() -> StateGraph:
    graph = StateGraph(AgentState)
    graph.add_node("analyse_and_route",    node_analyse_and_route)
    graph.add_node("application_followup", node_dynamic_application_followup)
    graph.add_node("benefits_followup",    node_dynamic_benefits_followup)

    def routing_condition(state: AgentState) -> str:
        if not state.get("in_followup"):
            return "done"
        parent = state.get("followup_parent", "")
        if parent == "Application":
            return "app_followup"
        if parent == "Business Benefits":
            return "benefits_followup"
        return "done"

    graph.add_conditional_edges(
        "analyse_and_route",
        routing_condition,
        {
            "app_followup":      "application_followup",
            "benefits_followup": "benefits_followup",
            "done":              END,
        }
    )
    graph.add_edge("application_followup", END)
    graph.add_edge("benefits_followup",    END)
    graph.set_entry_point("analyse_and_route")
    return graph.compile()


AGENT_GRAPH = build_agent_graph()


# =========================================================
# GEO INFERENCE
# Uses COUNTRY_TO_REGION lookup — Gemini resolves city → country.
# Fails safely to fallback if Gemini unavailable.
# =========================================================

COUNTRY_TO_REGION = {
    "United States": "North America", "Canada": "North America", "Mexico": "North America",
    "United Kingdom": "Europe", "Germany": "Europe", "France": "Europe", "Italy": "Europe",
    "Spain": "Europe", "Netherlands": "Europe", "Poland": "Europe", "Switzerland": "Europe",
    "Sweden": "Europe", "Norway": "Europe", "Denmark": "Europe", "Finland": "Europe",
    "Ireland": "Europe", "Portugal": "Europe", "Austria": "Europe", "Belgium": "Europe",
    "Greece": "Europe", "Turkey": "Europe", "Czech Republic": "Europe", "Hungary": "Europe",
    "Romania": "Europe", "Slovakia": "Europe", "Croatia": "Europe", "Bulgaria": "Europe",
    "Serbia": "Europe", "Ukraine": "Europe", "Russia": "Europe", "Luxembourg": "Europe",
    "Estonia": "Europe", "Latvia": "Europe", "Lithuania": "Europe", "Slovenia": "Europe",
    "Albania": "Europe", "North Macedonia": "Europe", "Bosnia and Herzegovina": "Europe",
    "Cyprus": "Europe", "Malta": "Europe", "Iceland": "Europe",
    "India": "Asia-South Pacific", "Australia": "Asia-South Pacific",
    "New Zealand": "Asia-South Pacific", "Singapore": "Asia-South Pacific",
    "Malaysia": "Asia-South Pacific", "Indonesia": "Asia-South Pacific",
    "Thailand": "Asia-South Pacific", "Vietnam": "Asia-South Pacific",
    "Philippines": "Asia-South Pacific", "Pakistan": "Asia-South Pacific",
    "Bangladesh": "Asia-South Pacific", "Sri Lanka": "Asia-South Pacific",
    "Nepal": "Asia-South Pacific", "Myanmar": "Asia-South Pacific",
    "Cambodia": "Asia-South Pacific", "Afghanistan": "Asia-South Pacific",
    "Egypt": "Middle East & Africa", "United Arab Emirates": "Middle East & Africa",
    "Saudi Arabia": "Middle East & Africa", "Nigeria": "Middle East & Africa",
    "Kenya": "Middle East & Africa", "South Africa": "Middle East & Africa",
    "Qatar": "Middle East & Africa", "Kuwait": "Middle East & Africa",
    "Bahrain": "Middle East & Africa", "Oman": "Middle East & Africa",
    "Jordan": "Middle East & Africa", "Lebanon": "Middle East & Africa",
    "Israel": "Middle East & Africa", "Ghana": "Middle East & Africa",
    "Ethiopia": "Middle East & Africa", "Tanzania": "Middle East & Africa",
    "Morocco": "Middle East & Africa", "Algeria": "Middle East & Africa",
    "Tunisia": "Middle East & Africa", "Uganda": "Middle East & Africa",
    "Iraq": "Middle East & Africa", "Iran": "Middle East & Africa",
    "Yemen": "Middle East & Africa", "Libya": "Middle East & Africa",
    "Sudan": "Middle East & Africa", "Angola": "Middle East & Africa",
    "Brazil": "Latin America", "Argentina": "Latin America", "Chile": "Latin America",
    "Colombia": "Latin America", "Peru": "Latin America", "Ecuador": "Latin America",
    "Venezuela": "Latin America", "Uruguay": "Latin America", "Paraguay": "Latin America",
    "Bolivia": "Latin America", "Panama": "Latin America", "Costa Rica": "Latin America",
    "Dominican Republic": "Latin America", "Guatemala": "Latin America",
    "Honduras": "Latin America", "Cuba": "Latin America",
    "China": "China", "Hong Kong": "China", "Macau": "China",
    "Japan": "North Asia", "South Korea": "North Asia", "Taiwan": "North Asia",
    "Mongolia": "North Asia", "North Korea": "North Asia",
    "Global": "Global",
}


def infer_geo_from_area(area: str) -> dict:
    """
    Resolve city/region → country → enterprise region.
    Single Gemini call only. Falls back instantly if Gemini fails (no retry/sleep).
    """
    area_clean = area.strip()

    # Direct country match — no Gemini needed
    if area_clean.title() in COUNTRY_TO_REGION:
        country = area_clean.title()
        return {"Requesting Countries": country,
                "Requesting Regions":   COUNTRY_TO_REGION[country],
                "Bill2Country":         country}

    prompt = (
        "You are a world geography expert. Given a location (city, town, district, region, or country), "
        "identify the sovereign country it belongs to.\n\n"
        f"Location: \"{area_clean}\"\n\n"
        "Think step by step: what type of place is this? Which sovereign country contains it?\n\n"
        "Return ONLY this JSON (no markdown, no preamble):\n"
        "{\"reasoning\": \"<one sentence>\", \"country\": \"<full English country name>\"}"
    )

    try:
        raw = gemini_generate(prompt).strip()
        raw = re.sub(r"```[a-zA-Z]*", "", raw).replace("```", "").strip()
        match = re.search(r'\{.*?\}', raw, re.DOTALL)
        parsed  = json.loads(match.group() if match else raw)
        country = parsed.get("country", "").strip()
        print(f"[GEO] Resolved '{area_clean}' → '{country}'")

        if country in COUNTRY_TO_REGION:
            return {"Requesting Countries": country,
                    "Requesting Regions":   COUNTRY_TO_REGION[country],
                    "Bill2Country":         country}
        if country and country != "Global":
            return {"Requesting Countries": country,
                    "Requesting Regions":   "Global",
                    "Bill2Country":         country}
    except Exception as e:
        print(f"[GEO] Gemini call failed ({type(e).__name__}) — using fallback")

    # Fallback: return area as-is
    return {"Requesting Countries": area_clean.title(),
            "Requesting Regions":   "Global",
            "Bill2Country":         area_clean.title()}


# =========================================================
# PDF GENERATION  (unchanged)
# =========================================================

NAVY       = colors.HexColor("#1B2A4A")
BLUE       = colors.HexColor("#2563EB")
LIGHT_BG   = colors.HexColor("#EEF2FF")
MID_BG     = colors.HexColor("#DBEAFE")
WHITE      = colors.white
GREY_LINE  = colors.HexColor("#CBD5E1")
DARK_TEXT  = colors.HexColor("#1E293B")
MUTED      = colors.HexColor("#64748B")
PURPLE_BG  = colors.HexColor("#EDE9FE")
PURPLE_TXT = colors.HexColor("#5B21B6")
TEAL_BG    = colors.HexColor("#CCFBF1")
TEAL_TXT   = colors.HexColor("#0F766E")
AGENT_BG   = colors.HexColor("#FEF3C7")
AGENT_TXT  = colors.HexColor("#92400E")

SECTIONS = {
    "Demand Overview": [
        "Demand Title", "Requirement summary", "Rationale and Purpose",
        "Acceptance Criteria", "Business Benefits",
    ],
    "Process & Technical": [
        "Business_Process", "E2E process in GPM", "Application", "Landscape Impacted",
    ],
    "Geographic Information": [
        "Area", "Requesting Countries", "Requesting Regions", "Bill2Country",
    ],
    "Timeline & Risk": [
        "Target Go Live Date", "Risks if not implemented on Target date",
        "Does this have any month-end (/Year-end) dependency?",
    ],
    "Compliance & Impact": [
        "Is this a legal/fiscal change?", "Is Audit requirement",
        "GTS Impact", "Impacted business groups",
    ],
    "Business Information": [
        "Requesting BU", "Demand_route", "Potential_savings",
        "Demand Status", "SubmissionType",
    ],
}

AUTO_POPULATED_KEYS = {"Requesting Countries", "Requesting Regions", "Bill2Country"}
SYSTEM_KEYS         = {"Demand ID", "Demand Timestamp"}


def generate_demand_pdf(payload: dict) -> BytesIO:
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm, topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()

    style_title    = ParagraphStyle("DT", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=20, textColor=WHITE, alignment=TA_CENTER, spaceAfter=2)
    style_subtitle = ParagraphStyle("DS", parent=styles["Normal"],
        fontName="Helvetica", fontSize=8.5, textColor=colors.HexColor("#93C5FD"), alignment=TA_CENTER)
    style_demandid = ParagraphStyle("DID", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=10, textColor=colors.HexColor("#FDE68A"), alignment=TA_CENTER)
    style_section  = ParagraphStyle("SH", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=10, textColor=WHITE)
    style_label    = ParagraphStyle("FL", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=8.5, textColor=MUTED, spaceAfter=1)
    style_value    = ParagraphStyle("FV", parent=styles["Normal"],
        fontName="Helvetica", fontSize=9.5, textColor=DARK_TEXT, leading=13)
    style_auto     = ParagraphStyle("AV", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=9.5, textColor=PURPLE_TXT, leading=13)
    style_sys      = ParagraphStyle("SV", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=9.5, textColor=TEAL_TXT, leading=13)
    style_footer   = ParagraphStyle("FT", parent=styles["Normal"],
        fontName="Helvetica", fontSize=7.5, textColor=MUTED, alignment=TA_CENTER)

    story = []
    demand_id    = payload.get("Demand ID", "N/A")
    timestamp    = payload.get("Demand Timestamp", "N/A")
    demand_title = payload.get("Demand Title", "Demand Request")

    header_data = [
        [Paragraph("DEMAND CREATION REQUEST", style_title)],
        [Paragraph(f"Demand ID: {demand_id}", style_demandid)],
        [Paragraph(
            f"Recorded: {timestamp}  |  Status: {payload.get('Demand Status','New')}  |  "
            f"Type: {payload.get('SubmissionType','Chatbot')}  |  ⚡ LangGraph Agentic",
            style_subtitle)],
    ]
    header_table = Table(header_data, colWidths=[174*mm])
    header_table.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), NAVY),
        ("TOPPADDING",    (0,0), (-1,0), 14), ("BOTTOMPADDING", (0,0), (-1,0), 2),
        ("TOPPADDING",    (0,1), (-1,1), 2),  ("BOTTOMPADDING", (0,1), (-1,1), 2),
        ("TOPPADDING",    (0,2), (-1,2), 0),  ("BOTTOMPADDING", (0,2), (-1,2), 12),
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 4*mm))

    title_card = Table(
        [[Paragraph(f"<b>{demand_title}</b>", ParagraphStyle("TC",
            fontName="Helvetica-Bold", fontSize=13, textColor=NAVY, alignment=TA_CENTER))]],
        colWidths=[174*mm])
    title_card.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), MID_BG),
        ("TOPPADDING",    (0,0), (-1,-1), 10), ("BOTTOMPADDING", (0,0), (-1,-1), 10),
        ("BOX",           (0,0), (-1,-1), 1, BLUE),
    ]))
    story.append(title_card)
    story.append(Spacer(1, 5*mm))

    # Agent follow-up log
    followup_log = payload.get("_agent_followup_log", [])
    if followup_log:
        agent_lines = []
        for entry in followup_log:
            t   = entry.get("type", "")
            cat = entry.get("benefit_category", "")
            apps = entry.get("apps_detected", [])
            label = f"[{', '.join(apps)}]" if apps else f"[{cat}]" if cat else ""
            agent_lines.append(f"• {t} {label}: {entry.get('followup_question','')}")
            if entry.get("user_followup_answer"):
                agent_lines.append(f"  → {entry['user_followup_answer']}")
        if agent_lines:
            agent_sec = Table(
                [[Paragraph("  ⚡ LANGGRAPH AGENT — DYNAMIC FOLLOW-UP LOG",
                    ParagraphStyle("AH", fontName="Helvetica-Bold", fontSize=9, textColor=AGENT_TXT))]],
                colWidths=[174*mm])
            agent_sec.setStyle(TableStyle([
                ("BACKGROUND",    (0,0), (-1,-1), AGENT_BG),
                ("TOPPADDING",    (0,0), (-1,-1), 6),
                ("BOTTOMPADDING", (0,0), (-1,-1), 6),
            ]))
            agent_val = Table(
                [[Paragraph("\n".join(agent_lines),
                    ParagraphStyle("AC", fontName="Helvetica", fontSize=8.5,
                        textColor=AGENT_TXT, leading=13))]],
                colWidths=[174*mm])
            agent_val.setStyle(TableStyle([
                ("BACKGROUND",    (0,0), (-1,-1), AGENT_BG),
                ("LEFTPADDING",   (0,0), (-1,-1), 12),
                ("RIGHTPADDING",  (0,0), (-1,-1), 12),
                ("TOPPADDING",    (0,0), (-1,-1), 6),
                ("BOTTOMPADDING", (0,0), (-1,-1), 8),
                ("BOX",           (0,0), (-1,-1), 0.5, colors.HexColor("#F59E0B")),
            ]))
            story.append(agent_sec)
            story.append(agent_val)
            story.append(Spacer(1, 4*mm))

    single_key_fields = {
        "Demand Title", "Requirement summary", "Rationale and Purpose",
        "Acceptance Criteria", "Business Benefits",
        "Risks if not implemented on Target date"
    }

    for section_name, keys in SECTIONS.items():
        sec_header = Table(
            [[Paragraph(f"  {section_name.upper()}", style_section)]],
            colWidths=[174*mm])
        sec_header.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,-1), BLUE),
            ("TOPPADDING",    (0,0), (-1,-1), 6),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ]))

        field_rows = []
        i = 0
        while i < len(keys):
            key   = keys[i]
            value = payload.get(key)
            if not value:
                i += 1
                continue
            is_auto = key in AUTO_POPULATED_KEYS
            is_sys  = key in SYSTEM_KEYS
            label_p = Paragraph(key.replace("_", " ").upper(), style_label)
            if is_auto:
                val_p   = Paragraph(f"&#9733; {value}  <font size='7' color='#7C3AED'>[Auto]</font>", style_auto)
                cell_bg = PURPLE_BG
            elif is_sys:
                val_p   = Paragraph(f"&#128273; {value}", style_sys)
                cell_bg = TEAL_BG
            else:
                val_p   = Paragraph(str(value), style_value)
                cell_bg = LIGHT_BG

            if key in single_key_fields or len(str(value)) > 80:
                field_rows.append(("full", label_p, val_p, cell_bg))
                i += 1
            else:
                j = i + 1
                next_key = None
                while j < len(keys):
                    if payload.get(keys[j]):
                        next_key = keys[j]
                        break
                    j += 1
                if next_key and next_key not in single_key_fields and len(str(payload.get(next_key, ""))) <= 80:
                    nv      = payload.get(next_key)
                    nis_auto = next_key in AUTO_POPULATED_KEYS
                    nis_sys  = next_key in SYSTEM_KEYS
                    nl_p = Paragraph(next_key.replace("_", " ").upper(), style_label)
                    if nis_auto:
                        nv_p = Paragraph(f"&#9733; {nv}  <font size='7' color='#7C3AED'>[Auto]</font>", style_auto)
                        nbg  = PURPLE_BG
                    elif nis_sys:
                        nv_p = Paragraph(f"&#128273; {nv}", style_sys)
                        nbg  = TEAL_BG
                    else:
                        nv_p = Paragraph(str(nv), style_value)
                        nbg  = LIGHT_BG
                    field_rows.append(("pair", label_p, val_p, cell_bg, nl_p, nv_p, nbg))
                    i = j + 1
                else:
                    field_rows.append(("full", label_p, val_p, cell_bg))
                    i += 1

        if not field_rows:
            continue

        tbl_data   = []
        tbl_styles = [
            ("GRID",         (0,0), (-1,-1), 0.5, GREY_LINE),
            ("TOPPADDING",   (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0), (-1,-1), 6),
            ("LEFTPADDING",  (0,0), (-1,-1), 8),
            ("RIGHTPADDING", (0,0), (-1,-1), 8),
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
        ]
        ri = 0
        for row_spec in field_rows:
            if row_spec[0] == "full":
                _, lp, vp, bg = row_spec
                tbl_data.append([lp, vp])
                tbl_styles += [("BACKGROUND", (0,ri),(1,ri), bg)]
            else:
                _, lp, vp, bg, nlp, nvp, nbg = row_spec
                tbl_data.append([lp, nlp])
                tbl_styles += [
                    ("BACKGROUND",    (0,ri),(0,ri), bg),
                    ("BACKGROUND",    (1,ri),(1,ri), nbg),
                    ("BOTTOMPADDING", (0,ri),(1,ri), 1),
                ]
                ri += 1
                tbl_data.append([vp, nvp])
                tbl_styles += [
                    ("BACKGROUND", (0,ri),(0,ri), bg),
                    ("BACKGROUND", (1,ri),(1,ri), nbg),
                    ("TOPPADDING", (0,ri),(1,ri), 1),
                ]
            ri += 1

        field_table = Table(tbl_data, colWidths=[87*mm, 87*mm])
        field_table.setStyle(TableStyle(tbl_styles))
        story.append(KeepTogether([
            sec_header, Spacer(1, 2*mm), field_table, Spacer(1, 4*mm),
        ]))

    story.append(HRFlowable(width="100%", thickness=0.5, color=GREY_LINE))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        f"Demand ID: {demand_id}  |  Recorded: {timestamp}  |  "
        f"Powered by LangGraph Agentic AI + Gemini 2.5 Flash",
        style_footer))
    doc.build(story)
    buffer.seek(0)
    return buffer


# =========================================================
# SESSION STATE + DEMAND ID
# =========================================================

session_state = {
    "captured": {}, "completed": [], "last_payload": {},
    "in_followup": False, "followup_parent": "",
    "followup_count": 0, "followup_log": [], "agent_insight": ""
}
_demand_counter = 0


def generate_demand_id() -> str:
    global _demand_counter
    _demand_counter += 1
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    return f"{datetime.now(IST).strftime('%y%m')}_DEMO{_demand_counter:04d}"


def generate_timestamp() -> str:
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(IST).strftime("%d-%b-%Y %I:%M %p") + " IST"


def reset_session():
    session_state["captured"]        = {}
    session_state["completed"]       = []
    session_state["in_followup"]     = False
    session_state["followup_parent"] = ""
    session_state["followup_count"]  = 0
    session_state["followup_log"]    = []
    session_state["agent_insight"]   = ""


def get_next_field():
    for field in MANDATORY_FIELDS:
        if field["key"] not in session_state["completed"]:
            return field
    return None


# =========================================================
# FLASK APP
# =========================================================

app = Flask(__name__)


@app.route("/start", methods=["GET"])
def start():
    reset_session()
    first = MANDATORY_FIELDS[0]
    return jsonify({
        "message": (
            "Welcome to the Demand Creation Chatbot! ⚡ Powered by LangGraph Agentic AI.\n\n"
            "I'll guide you through capturing your demand. For Applications and Business Benefits, "
            "I'll ask intelligent follow-up questions to enrich your demand automatically."
        ),
        "next_question": first["question"],
        "field":         first["key"],
        "progress":      f"1 of {len(MANDATORY_FIELDS)}",
        "agent_insight": ""
    })


@app.route("/chat", methods=["POST"])
def chat():
    body = request.get_json(silent=True)
    if not body or "message" not in body:
        return jsonify({"error": "message field is required"}), 400
    message = body["message"].strip()
    if not message:
        return jsonify({"error": "Empty message received"}), 400

    # ── CASE A: answering an agent follow-up ──────────────────────────────
    if session_state["in_followup"]:
        agent_state: AgentState = {
            "captured":        session_state["captured"],
            "completed":       session_state["completed"],
            "current_field":   session_state["followup_parent"],
            "current_question": "",
            "in_followup":     True,
            "followup_parent": session_state["followup_parent"],
            "followup_count":  session_state["followup_count"],
            "agent_insight":   "",
            "followup_log":    session_state["followup_log"],
            "is_complete":     False,
            "final_payload":   {},
        }
        updated = node_capture_followup_answer(agent_state, message)

        session_state["captured"]        = updated["captured"]
        session_state["in_followup"]     = False
        session_state["followup_parent"] = ""
        session_state["followup_count"]  = 0
        session_state["followup_log"]    = updated["followup_log"]
        session_state["agent_insight"]   = ""

        next_field = get_next_field()
        answered   = len(session_state["completed"])
        total      = len(MANDATORY_FIELDS)

        if next_field:
            return jsonify({
                "captured_field": "follow-up",
                "captured_value": message,
                "next_question":  next_field["question"],
                "field":          next_field["key"],
                "progress":       f"{answered + 1} of {total}",
                "agent_insight":  "",
                "is_followup":    False,
            })
        # Fall through to completion
        message = "__COMPLETE__"

    # ── CASE B: answering a mandatory field ───────────────────────────────
    if message != "__COMPLETE__":
        current_field = get_next_field()
        if not current_field:
            return jsonify({"error": "Session already complete. Call /start to reset."}), 400

        session_state["captured"][current_field["key"]] = message
        session_state["completed"].append(current_field["key"])

        # Geo inference for Area field
        if current_field["key"] == "Area":
            geo = infer_geo_from_area(message)
            session_state["captured"]["Requesting Regions"]   = geo.get("Requesting Regions", "Global")
            session_state["captured"]["Requesting Countries"] = geo.get("Requesting Countries", message)
            session_state["captured"]["Bill2Country"]         = geo.get("Bill2Country", message)

        # Run LangGraph agent
        agent_state: AgentState = {
            "captured":        session_state["captured"],
            "completed":       session_state["completed"],
            "current_field":   current_field["key"],
            "current_question": "",
            "in_followup":     False,
            "followup_parent": "",
            "followup_count":  session_state["followup_count"],
            "agent_insight":   "",
            "followup_log":    session_state["followup_log"],
            "is_complete":     False,
            "final_payload":   {},
        }
        result = AGENT_GRAPH.invoke(agent_state)

        session_state["followup_log"]  = result.get("followup_log", session_state["followup_log"])
        session_state["agent_insight"] = result.get("agent_insight", "")

        # If agent triggered a follow-up and it succeeded (has a question)
        if result.get("in_followup") and result.get("current_question"):
            session_state["in_followup"]     = True
            session_state["followup_parent"] = result["followup_parent"]
            session_state["followup_count"]  = result.get("followup_count", 1)

            answered = len(session_state["completed"])
            total    = len(MANDATORY_FIELDS)
            return jsonify({
                "captured_field": current_field["key"],
                "captured_value": message,
                "next_question":  result["current_question"],
                "field":          f"[Agent Follow-up] {current_field['key']}",
                "progress":       f"{answered} of {total}",
                "agent_insight":  result.get("agent_insight", ""),
                "is_followup":    True,
            })

    # ── CASE C: check if more mandatory fields remain ─────────────────────
    next_field = get_next_field()
    answered   = len(session_state["completed"])
    total      = len(MANDATORY_FIELDS)

    if next_field and message != "__COMPLETE__":
        return jsonify({
            "captured_field": session_state["completed"][-1] if session_state["completed"] else "",
            "captured_value": message,
            "next_question":  next_field["question"],
            "field":          next_field["key"],
            "progress":       f"{answered + 1} of {total}",
            "agent_insight":  session_state["agent_insight"],
            "is_followup":    False,
        })

    # ── CASE D: All fields done — build final payload ─────────────────────
    final = dict(session_state["captured"])
    final["Demand Status"]       = "New"
    final["SubmissionType"]      = "Chatbot (LangGraph Agentic)"
    final["Demand ID"]           = generate_demand_id()
    final["Demand Timestamp"]    = generate_timestamp()
    final["_agent_followup_log"] = session_state["followup_log"]

    if "Requesting BU" in final and "Area" in final:
        final["Demand_route"] = f"{final['Requesting BU']} - {final['Area']}"

    session_state["last_payload"] = final
    db_saved = save_to_db(final)

    auto = {
        "Requesting Regions":   final.get("Requesting Regions", ""),
        "Requesting Countries": final.get("Requesting Countries", ""),
        "Bill2Country":         final.get("Bill2Country", ""),
    }
    payload_for_display = {k: v for k, v in final.items() if not k.startswith("_")}

    reset_session()
    return jsonify({
        "status":               "All mandatory fields captured successfully!",
        "db_saved":             db_saved,
        "auto_populated":       auto,
        "demand_id":            final["Demand ID"],
        "demand_timestamp":     final["Demand Timestamp"],
        "agent_followup_log":   final["_agent_followup_log"],
        "final_demand_payload": payload_for_display,
    })


@app.route("/download-pdf", methods=["POST"])
def download_pdf():
    payload = request.get_json(silent=True)
    if not payload:
        payload = session_state.get("last_payload", {})
    if not payload:
        return jsonify({"error": "No payload available"}), 400
    if "_agent_followup_log" not in payload:
        payload["_agent_followup_log"] = session_state.get("last_payload", {}).get("_agent_followup_log", [])
    try:
        pdf_buffer = generate_demand_pdf(payload)
        demand_id  = payload.get("Demand ID", "Demand")
        filename   = f"{demand_id}_{payload.get('Demand Title','Request').replace(' ','_')[:30]}.pdf"
        return send_file(pdf_buffer, mimetype="application/pdf",
                         as_attachment=True, download_name=filename)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/reset", methods=["POST"])
def reset():
    reset_session()
    first = MANDATORY_FIELDS[0]
    return jsonify({"message": "Session reset.", "next_question": first["question"], "field": first["key"]})


@app.route("/ui")
def ui():
    total_fields = len(MANDATORY_FIELDS)
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Demand Creation — Agentic AI</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Mono:wght@400;500&family=Inter:wght@300;400;500&display=swap" rel="stylesheet"/>
<style>
  :root {
    --bg:#0a0e1a;--surface:#111827;--surface2:#1a2235;--border:#1e2d45;
    --primary:#3b82f6;--accent:#8b5cf6;--agent:#f59e0b;--green:#10b981;
    --user-bg:#1d4ed8;--bot-bg:#1a2235;--text:#e2e8f0;--muted:#64748b;
    --radius:14px;--glow:0 0 30px rgba(59,130,246,0.15);
  }
  *{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:'Inter',sans-serif;background:var(--bg);
    background-image:radial-gradient(ellipse at 20% 10%,rgba(59,130,246,0.08) 0%,transparent 50%),
    radial-gradient(ellipse at 80% 90%,rgba(139,92,246,0.08) 0%,transparent 50%);
    min-height:100vh;display:flex;align-items:center;justify-content:center;
    padding:20px;color:var(--text);}
  .container{width:100%;max-width:780px;background:var(--surface);
    border:1px solid var(--border);border-radius:20px;
    box-shadow:var(--glow),0 25px 60px rgba(0,0,0,0.5);
    overflow:hidden;display:flex;flex-direction:column;height:92vh;max-height:760px;}
  .header{background:linear-gradient(135deg,#0f1b35 0%,#1a1040 100%);
    border-bottom:1px solid var(--border);padding:18px 28px;
    display:flex;align-items:center;justify-content:space-between;gap:12px;}
  .header-brand{display:flex;align-items:center;gap:12px;}
  .brand-icon{width:40px;height:40px;background:linear-gradient(135deg,var(--primary),var(--accent));
    border-radius:10px;display:flex;align-items:center;justify-content:center;
    font-size:18px;flex-shrink:0;box-shadow:0 0 20px rgba(59,130,246,0.3);}
  .brand-text h1{font-family:'Syne',sans-serif;font-size:16px;font-weight:700;color:#f1f5f9;}
  .brand-text p{font-size:11px;color:var(--muted);margin-top:2px;}
  .agent-badge{background:rgba(245,158,11,0.15);border:1px solid rgba(245,158,11,0.3);
    color:var(--agent);font-family:'DM Mono',monospace;font-size:10px;
    padding:4px 10px;border-radius:20px;display:flex;align-items:center;gap:5px;}
  .agent-dot{width:6px;height:6px;border-radius:50%;background:var(--agent);
    animation:pulse 1.5s infinite;}
  @keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:0.5;transform:scale(0.8)}}
  .reset-btn{background:rgba(255,255,255,0.05);border:1px solid var(--border);
    color:var(--muted);padding:6px 14px;border-radius:8px;cursor:pointer;
    font-size:11px;font-family:inherit;transition:all 0.2s;}
  .reset-btn:hover{background:rgba(255,255,255,0.1);color:var(--text);}
  .progress-wrap{background:var(--surface2);border-bottom:1px solid var(--border);
    padding:10px 28px;display:flex;align-items:center;gap:14px;}
  .progress-bg{flex:1;height:4px;background:var(--border);border-radius:99px;overflow:hidden;}
  .progress-fill{height:100%;background:linear-gradient(90deg,var(--primary),var(--accent));
    border-radius:99px;transition:width 0.5s cubic-bezier(0.4,0,0.2,1);width:0%;
    box-shadow:0 0 8px rgba(59,130,246,0.5);}
  .progress-lbl{font-family:'DM Mono',monospace;font-size:10px;color:var(--primary);
    white-space:nowrap;min-width:70px;text-align:right;}
  .chat-area{flex:1;overflow-y:auto;padding:24px 28px;display:flex;
    flex-direction:column;gap:16px;scroll-behavior:smooth;}
  .chat-area::-webkit-scrollbar{width:4px;}
  .chat-area::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px;}
  .bubble-row{display:flex;align-items:flex-end;gap:10px;animation:fadeUp 0.3s ease;}
  .bubble-row.user{flex-direction:row-reverse;}
  @keyframes fadeUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
  .avatar{width:30px;height:30px;border-radius:8px;display:flex;
    align-items:center;justify-content:center;font-size:13px;flex-shrink:0;}
  .avatar.bot{background:linear-gradient(135deg,var(--primary),var(--accent));}
  .avatar.user{background:var(--surface2);border:1px solid var(--border);}
  .bubble{max-width:80%;padding:12px 16px;border-radius:14px;font-size:14px;
    line-height:1.6;word-break:break-word;}
  .bubble.bot{background:var(--bot-bg);border:1px solid var(--border);
    color:var(--text);border-bottom-left-radius:4px;}
  .bubble.user{background:var(--user-bg);color:#fff;border-bottom-right-radius:4px;}
  .bubble.agent-followup{background:rgba(245,158,11,0.08);
    border:1px solid rgba(245,158,11,0.25);border-bottom-left-radius:4px;}
  .field-tag{font-size:9.5px;font-weight:600;font-family:'DM Mono',monospace;
    color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:5px;}
  .field-tag.agent{color:var(--agent);}
  .insight-box{background:rgba(139,92,246,0.1);border:1px solid rgba(139,92,246,0.2);
    border-radius:8px;padding:8px 12px;margin-bottom:8px;
    font-size:12px;color:#a78bfa;font-style:italic;}
  .insight-box::before{content:'💡 ';}
  .demand-id-badge{display:inline-block;background:#0f172a;color:#fde68a;
    font-family:'DM Mono',monospace;font-size:12px;font-weight:700;
    padding:5px 14px;border-radius:8px;margin-bottom:8px;letter-spacing:1px;
    border:1px solid rgba(253,230,138,0.2);}
  .timestamp-badge{display:inline-block;background:rgba(16,185,129,0.1);color:#34d399;
    font-family:'DM Mono',monospace;font-size:11px;font-weight:600;
    padding:3px 10px;border-radius:6px;margin-bottom:10px;margin-left:6px;
    border:1px solid rgba(16,185,129,0.2);}
  .agent-log-box{background:rgba(245,158,11,0.06);border:1px solid rgba(245,158,11,0.2);
    border-radius:10px;padding:12px 14px;margin-bottom:10px;font-size:12px;}
  .agent-log-title{font-family:'Syne',sans-serif;font-size:11px;font-weight:700;
    color:var(--agent);margin-bottom:8px;text-transform:uppercase;letter-spacing:0.5px;}
  .agent-log-item{padding:4px 0;border-top:1px solid rgba(245,158,11,0.1);
    color:#fcd34d;font-family:'DM Mono',monospace;font-size:11px;}
  .final-box{background:rgba(16,185,129,0.05);border:1px solid rgba(16,185,129,0.15);
    border-radius:10px;padding:14px;font-size:11.5px;font-family:'DM Mono',monospace;
    color:#6ee7b7;white-space:pre-wrap;word-break:break-all;
    max-height:240px;overflow-y:auto;margin-bottom:12px;}
  .auto-pop-tag{display:inline-block;background:rgba(139,92,246,0.15);color:#a78bfa;
    font-size:10px;font-weight:600;font-family:'DM Mono',monospace;
    padding:2px 8px;border-radius:99px;margin-bottom:6px;
    border:1px solid rgba(139,92,246,0.2);}
  .pdf-btn{display:inline-flex;align-items:center;gap:7px;
    background:linear-gradient(135deg,#059669,#047857);
    color:white;border:none;border-radius:10px;padding:10px 20px;
    font-size:13px;font-weight:600;font-family:inherit;cursor:pointer;
    transition:opacity 0.2s,transform 0.1s;margin-top:4px;
    box-shadow:0 0 20px rgba(5,150,105,0.3);}
  .pdf-btn:hover{opacity:0.9;} .pdf-btn:active{transform:scale(0.97);}
  .pdf-btn:disabled{opacity:0.5;cursor:not-allowed;}
  .typing{display:flex;gap:5px;align-items:center;padding:12px 16px;
    background:var(--bot-bg);border:1px solid var(--border);
    border-radius:14px;border-bottom-left-radius:4px;width:fit-content;}
  .typing span{width:6px;height:6px;background:var(--primary);border-radius:50%;
    animation:bounce 1.2s infinite;}
  .typing span:nth-child(2){animation-delay:0.2s;}
  .typing span:nth-child(3){animation-delay:0.4s;}
  @keyframes bounce{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-6px)}}
  .input-area{padding:14px 28px 18px;border-top:1px solid var(--border);
    background:var(--surface);display:flex;gap:10px;}
  .input-area input{flex:1;padding:12px 16px;background:var(--surface2);
    border:1.5px solid var(--border);border-radius:10px;font-size:14px;
    font-family:inherit;outline:none;color:var(--text);transition:border 0.2s;}
  .input-area input:focus{border-color:var(--primary);box-shadow:0 0 0 3px rgba(59,130,246,0.1);}
  .input-area input::placeholder{color:var(--muted);}
  .send-btn{background:linear-gradient(135deg,var(--primary),var(--accent));
    color:white;border:none;border-radius:10px;padding:12px 22px;
    font-size:14px;font-weight:600;font-family:inherit;cursor:pointer;
    transition:opacity 0.2s,transform 0.1s;box-shadow:0 0 20px rgba(59,130,246,0.25);}
  .send-btn:hover{opacity:0.9;} .send-btn:active{transform:scale(0.97);}
  .send-btn:disabled{opacity:0.4;cursor:not-allowed;}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="header-brand">
      <div class="brand-icon">⚡</div>
      <div class="brand-text">
        <h1>Demand Creation Agent</h1>
        <p>Powered by LangGraph + Gemini 2.5 Flash</p>
      </div>
    </div>
    <div style="display:flex;gap:8px;align-items:center;">
      <div class="agent-badge"><div class="agent-dot"></div>LangGraph Active</div>
      <button class="reset-btn" onclick="resetChat()">↺ Reset</button>
    </div>
  </div>
  <div class="progress-wrap">
    <div class="progress-bg"><div class="progress-fill" id="progressBar"></div></div>
    <div class="progress-lbl" id="progressLabel">Starting...</div>
  </div>
  <div class="chat-area" id="chatArea"></div>
  <div class="input-area">
    <input type="text" id="inp" placeholder="Type your answer and press Enter..."
           onkeydown="if(event.key==='Enter')sendMsg()"/>
    <button class="send-btn" id="sendBtn" onclick="sendMsg()">Send →</button>
  </div>
</div>
<script>
const TOTAL = TOTAL_PLACEHOLDER;
let started = false;
let lastPayload = null;

async function startChat() {
  const res = await fetch('/start');
  const d = await res.json();
  addBotMsg(d.message, null, null);
  setTimeout(() => addBotMsg(d.next_question, d.field, d.progress), 700);
  started = true;
}

async function sendMsg() {
  const inp = document.getElementById('inp');
  const msg = inp.value.trim();
  if (!msg || !started) return;
  addUserMsg(msg); inp.value = '';
  document.getElementById('sendBtn').disabled = true;
  const tid = showTyping();
  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg})
    });
    const d = await res.json();
    removeTyping(tid);

    if (d.error) {
      addBotMsg('⚠️ ' + d.error, null, null);
    } else if (d.final_demand_payload) {
      lastPayload = d.final_demand_payload;
      const auto = d.auto_populated;
      const agentLog = d.agent_followup_log || [];

      addBotMsg(
        '<span class="auto-pop-tag">✨ Auto-populated by Gemini</span><br/>'
        + '<b>Requesting Regions:</b> ' + auto['Requesting Regions'] + '<br/>'
        + '<b>Requesting Countries:</b> ' + auto['Requesting Countries'] + '<br/>'
        + '<b>Bill2Country:</b> ' + auto['Bill2Country'],
        null, null
      );

      setTimeout(() => {
        let agentLogHtml = '';
        if (agentLog.length > 0) {
          agentLogHtml = '<div class="agent-log-box"><div class="agent-log-title">⚡ LangGraph Agent — Dynamic Follow-up Log</div>';
          agentLog.forEach(entry => {
            const cat  = entry.benefit_category ? ' [' + entry.benefit_category + ']' : '';
            const apps = entry.apps_detected ? ' [' + entry.apps_detected.join(', ') + ']' : '';
            agentLogHtml += '<div class="agent-log-item">▶ ' + entry.type + apps + cat + '</div>';
            if (entry.user_followup_answer) {
              agentLogHtml += '<div class="agent-log-item" style="color:#a3e635;padding-left:12px;">↳ ' + entry.user_followup_answer + '</div>';
            }
          });
          agentLogHtml += '</div>';
        }
        addBotMsg(
          '<b>✅ ' + d.status + '</b><br/><br/>'
          + '<span class="demand-id-badge">🔑 Demand ID: ' + d.demand_id + '</span>'
          + '<span class="timestamp-badge">🕐 ' + d.demand_timestamp + '</span>'
          + '<br/><br/>' + agentLogHtml
          + 'Complete Demand Payload:<br/><br/>'
          + '<div class="final-box">' + JSON.stringify(d.final_demand_payload, null, 2) + '</div>'
          + '<button class="pdf-btn" onclick="downloadPDF()">📄 Download as PDF</button>',
          null, null
        );
        updateProgress('Done', 100);
        document.getElementById('inp').disabled = true;
        document.getElementById('sendBtn').disabled = true;
      }, 800);

    } else if (d.is_followup) {
      let insightHtml = d.agent_insight ? '<div class="insight-box">' + d.agent_insight + '</div>' : '';
      addAgentFollowupMsg(insightHtml + d.next_question, d.field, d.progress);
    } else {
      let insightHtml = d.agent_insight ? '<div class="insight-box">' + d.agent_insight + '</div>' : '';
      addBotMsg(insightHtml + d.next_question, d.field, d.progress);
    }
  } catch (e) {
    removeTyping(tid);
    addBotMsg('⚠️ Network error. Please try again.', null, null);
  }
  document.getElementById('sendBtn').disabled = false;
  document.getElementById('inp').focus();
}

async function downloadPDF() {
  if (!lastPayload) { alert('No payload available.'); return; }
  const btn = document.querySelector('.pdf-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Generating PDF...'; }
  try {
    const res = await fetch('/download-pdf', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(lastPayload)
    });
    if (!res.ok) throw new Error('PDF generation failed');
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url;
    a.download = (lastPayload['Demand ID'] || 'Demand') + '_'
      + (lastPayload['Demand Title'] || 'Request').replace(/\s+/g, '_').slice(0, 30) + '.pdf';
    a.click(); URL.revokeObjectURL(url);
  } catch (e) {
    alert('Error: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '📄 Download as PDF'; }
  }
}

function addBotMsg(html, field, progress) {
  const area = document.getElementById('chatArea');
  const row  = document.createElement('div'); row.className = 'bubble-row bot';
  let inner  = field ? '<div class="field-tag">' + field + '</div>' : '';
  inner += html;
  row.innerHTML = '<div class="avatar bot">🤖</div><div class="bubble bot">' + inner + '</div>';
  area.appendChild(row); area.scrollTop = area.scrollHeight;
  if (progress) updateProgress(progress);
}
function addAgentFollowupMsg(html, field, progress) {
  const area = document.getElementById('chatArea');
  const row  = document.createElement('div'); row.className = 'bubble-row bot';
  let inner  = field ? '<div class="field-tag agent">⚡ ' + field + '</div>' : '';
  inner += html;
  row.innerHTML = '<div class="avatar bot">⚡</div><div class="bubble agent-followup">' + inner + '</div>';
  area.appendChild(row); area.scrollTop = area.scrollHeight;
  if (progress) updateProgress(progress);
}
function addUserMsg(text) {
  const area = document.getElementById('chatArea');
  const row  = document.createElement('div'); row.className = 'bubble-row user';
  row.innerHTML = '<div class="avatar user">👤</div><div class="bubble user">' + text + '</div>';
  area.appendChild(row); area.scrollTop = area.scrollHeight;
}
function showTyping() {
  const area = document.getElementById('chatArea');
  const row  = document.createElement('div');
  const id   = 't' + Date.now(); row.id = id; row.className = 'bubble-row bot';
  row.innerHTML = '<div class="avatar bot">🤖</div>'
    + '<div class="typing"><span></span><span></span><span></span></div>';
  area.appendChild(row); area.scrollTop = area.scrollHeight; return id;
}
function removeTyping(id) { const el = document.getElementById(id); if (el) el.remove(); }
function updateProgress(label, pct) {
  const num = pct !== undefined ? pct : (() => {
    const p = label.split(' of ');
    return p.length === 2 ? Math.round(parseInt(p[0]) / TOTAL * 100) : 0;
  })();
  document.getElementById('progressBar').style.width = num + '%';
  document.getElementById('progressLabel').textContent = pct === 100 ? '✓ Complete' : 'Q ' + label;
}
async function resetChat() {
  await fetch('/reset', {method: 'POST'});
  document.getElementById('chatArea').innerHTML = '';
  document.getElementById('inp').disabled = false;
  document.getElementById('sendBtn').disabled = false;
  lastPayload = null; updateProgress('', 0); started = false; startChat();
}
startChat();
</script>
</body>
</html>"""
    html = html.replace("TOTAL_PLACEHOLDER", str(total_fields))
    return render_template_string(html)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print("=" * 60)
    print("  Demand Creation Chatbot — LangGraph Agentic (Fixed)")
    print("  Key fix: no sleep in gemini_generate, all nodes fail-safe")
    print(f"  URL: http://0.0.0.0:{port}/ui")
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
