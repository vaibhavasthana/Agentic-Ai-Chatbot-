"""
Demand Creation Chatbot - Final
================================
Changes in this version:
  1. Gemini 2.5 Flash is PRIMARY for geo resolution; local lookup is SECONDARY fallback
  2. Demand ID auto-generated per demand in format: YYMM_DEMO#### (e.g. 2605_DEMO0001)
  3. Timestamp recorded in payload showing exact time demand was submitted
- PDF export: professional reportlab-generated demand payload
- UI: SharePoint & Google Sites compatible
"""

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, render_template_string, send_file
import os, json, re
from io import BytesIO
from datetime import datetime
from google import genai
import sqlalchemy as sa

# reportlab imports
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
    raise RuntimeError("GEMINI_API_KEY must be set in your .env file")

client = genai.Client(api_key=GEMINI_API_KEY)

# =========================================================
# AZURE SQL DATABASE CONFIGURATION
# Stores every completed demand permanently.
# All values loaded from Render environment variables.
# =========================================================

AZURE_SQL_SERVER   = os.getenv("AZURE_SQL_SERVER")    # e.g. demandchatbot-se.database.windows.net
AZURE_SQL_DATABASE = os.getenv("AZURE_SQL_DATABASE")  # e.g. DemandChatbotDB
AZURE_SQL_USERNAME = os.getenv("AZURE_SQL_USERNAME")  # SQL admin username
AZURE_SQL_PASSWORD = os.getenv("AZURE_SQL_PASSWORD")  # SQL admin password

# SQLAlchemy engine — built once at startup, reused for every insert.
# Uses pytds dialect which needs NO system ODBC drivers (Render compatible).
_db_engine = None

def get_db_engine():
    """
    Returns a cached SQLAlchemy engine using pytds dialect.
    pytds is pure-Python — no ODBC/C drivers needed on Render.
    Connection string format: mssql+pytds://user:pass@server/database
    """
    global _db_engine
    if _db_engine is not None:
        return _db_engine
    if not all([AZURE_SQL_SERVER, AZURE_SQL_DATABASE, AZURE_SQL_USERNAME, AZURE_SQL_PASSWORD]):
        return None
    # URL-encode password to handle special characters safely
    from urllib.parse import quote_plus
    pwd = quote_plus(AZURE_SQL_PASSWORD)
    url = (
        f"mssql+pytds://{AZURE_SQL_USERNAME}:{pwd}"
        f"@{AZURE_SQL_SERVER}/{AZURE_SQL_DATABASE}"
        f"?encrypt=true"
    )
    _db_engine = sa.create_engine(url, pool_pre_ping=True, pool_recycle=300)
    print("[DB] SQLAlchemy engine created (pytds)")
    return _db_engine


def ensure_table_exists():
    """
    Creates the DemandRequests table if it doesn't already exist.
    Called once on first save — safe to call multiple times (IF NOT EXISTS).
    """
    create_sql = sa.text("""
    IF NOT EXISTS (
        SELECT * FROM sysobjects WHERE name='DemandRequests' AND xtype='U'
    )
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
    """
    Saves the completed demand payload to Azure SQL Database via SQLAlchemy + pytds.
    Auto-creates the table on first run if it doesn't exist.
    Returns True on success, False on failure — app continues regardless.
    """
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
            Requesting_BU, Potential_Savings
        ) VALUES (
            :demand_id, :demand_timestamp, :demand_status, :submission_type,
            :demand_route, :demand_title, :requirement_summary, :rationale_and_purpose,
            :acceptance_criteria, :business_benefits, :business_process, :e2e_process,
            :application, :landscape_impacted, :area, :requesting_countries,
            :requesting_regions, :bill2country, :target_go_live_date,
            :risks, :month_end_dependency, :legal_fiscal_change,
            :audit_requirement, :gts_impact, :impacted_business_groups,
            :requesting_bu, :potential_savings
        )
        """)

        values = {
            "demand_id":               payload.get("Demand ID", ""),
            "demand_timestamp":        payload.get("Demand Timestamp", ""),
            "demand_status":           payload.get("Demand Status", "New"),
            "submission_type":         payload.get("SubmissionType", "Chatbot"),
            "demand_route":            payload.get("Demand_route", ""),
            "demand_title":            payload.get("Demand Title", ""),
            "requirement_summary":     payload.get("Requirement summary", ""),
            "rationale_and_purpose":   payload.get("Rationale and Purpose", ""),
            "acceptance_criteria":     payload.get("Acceptance Criteria", ""),
            "business_benefits":       payload.get("Business Benefits", ""),
            "business_process":        payload.get("Business_Process", ""),
            "e2e_process":             payload.get("E2E process in GPM", ""),
            "application":             payload.get("Application", ""),
            "landscape_impacted":      payload.get("Landscape Impacted", ""),
            "area":                    payload.get("Area", ""),
            "requesting_countries":    payload.get("Requesting Countries", ""),
            "requesting_regions":      payload.get("Requesting Regions", ""),
            "bill2country":            payload.get("Bill2Country", ""),
            "target_go_live_date":     payload.get("Target Go Live Date", ""),
            "risks":                   payload.get("Risks if not implemented on Target date", ""),
            "month_end_dependency":    payload.get("Does this have any month-end (/Year-end) dependency?", ""),
            "legal_fiscal_change":     payload.get("Is this a legal/fiscal change?", ""),
            "audit_requirement":       payload.get("Is Audit requirement", ""),
            "gts_impact":              payload.get("GTS Impact", ""),
            "impacted_business_groups":payload.get("Impacted business groups", ""),
            "requesting_bu":           payload.get("Requesting BU", ""),
            "potential_savings":       payload.get("Potential_savings", ""),
        }

        with engine.begin() as conn:
            conn.execute(insert_sql, values)

        print(f"[DB] Saved to Azure SQL: {payload.get('Demand ID')}")
        return True

    except Exception as e:
        print(f"[DB] Failed to save: {type(e).__name__}: {e}")
        return False


# =========================================================
# MANDATORY FIELDS
# =========================================================

MANDATORY_FIELDS = [
    {"key": "Demand Title",                                          "question": "What is the Demand Title?"},
    {"key": "Requirement summary",                                   "question": "Please provide a Requirement Summary."},
    {"key": "Rationale and Purpose",                                 "question": "What is the Rationale and Purpose of this demand?"},
    {"key": "Acceptance Criteria",                                   "question": "What are the Acceptance Criteria?"},
    {"key": "Business Benefits",                                     "question": "What are the Business Benefits?"},
    {"key": "Business_Process",                                      "question": "What is the Business Process involved?"},
    {"key": "E2E process in GPM",                                    "question": "What is the E2E Process in GPM?"},
    {"key": "Area",                                                  "question": "What is the Area / Geographic Location? (e.g. Cairo, Maharashtra, Europe)"},
    {"key": "Application",                                           "question": "Which Application(s) are Impacted? (e.g. ECC, Fusion, SAP)"},
    {"key": "Target Go Live Date",                                   "question": "What is the Target Go Live Date? (e.g. 30/06/2026)"},
    {"key": "Risks if not implemented on Target date",               "question": "What are the Risks if not implemented on the Target Date?"},
    {"key": "Does this have any month-end (/Year-end) dependency?",  "question": "Does this have any Month-end / Year-end Dependency? (Yes / No)"},
    {"key": "Is this a legal/fiscal change?",                        "question": "Is this a Legal / Fiscal Change? (Yes / No)"},
    {"key": "Is Audit requirement",                                  "question": "Is there an Audit Requirement? (Yes / No)"},
    {"key": "GTS Impact",                                            "question": "Is there a GTS Impact? (Yes / No)"},
    {"key": "Impacted business groups",                              "question": "Which Business Groups are Impacted? (e.g. Personal Care, Foods)"},
    {"key": "Landscape Impacted",                                    "question": "Which Landscape is Impacted? (e.g. Fusion, ECC, Both)"},
    {"key": "Requesting BU",                                         "question": "What is the Requesting Business Unit (BU)?"},
    {"key": "Potential_savings",                                     "question": "What are the Potential Savings? (enter amount or describe)"},
]

# =========================================================
# SESSION STATE + DEMAND ID COUNTER
# =========================================================

session_state = {"captured": {}, "completed": [], "last_payload": {}}
_demand_counter = 0


def generate_demand_id() -> str:
    """
    Auto-generates a unique Demand ID. Format: YYMM_DEMO####
    e.g. 2605_DEMO0001 = first demand created in May 2026 (IST)
    Acts as primary key for each BRD / demand record.
    """
    global _demand_counter
    _demand_counter += 1
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    prefix = datetime.now(IST).strftime("%y%m")
    return f"{prefix}_DEMO{_demand_counter:04d}"


def generate_timestamp() -> str:
    """
    Records the exact moment the demand was submitted in IST (UTC+5:30).
    Format: DD-Mon-YYYY HH:MM AM/PM IST  e.g. 07-May-2026 10:45 AM IST
    """
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(IST).strftime("%d-%b-%Y %I:%M %p") + " IST"


def reset_session():
    session_state["captured"]  = {}
    session_state["completed"] = []


def get_next_field():
    for field in MANDATORY_FIELDS:
        if field["key"] not in session_state["completed"]:
            return field
    return None


# =========================================================
# ENTERPRISE REGION MAP
# The ONLY hardcoded element — maps sovereign country names
# to enterprise business regions. Gemini resolves location → country.
# Python maps country → region deterministically (zero tokens).
# =========================================================

COUNTRY_TO_REGION = {
    # North America
    "United States": "North America", "Canada": "North America", "Mexico": "North America",
    # Europe
    "United Kingdom": "Europe", "Germany": "Europe", "France": "Europe", "Italy": "Europe",
    "Spain": "Europe", "Netherlands": "Europe", "Poland": "Europe", "Switzerland": "Europe",
    "Sweden": "Europe", "Norway": "Europe", "Denmark": "Europe", "Finland": "Europe",
    "Ireland": "Europe", "Portugal": "Europe", "Austria": "Europe", "Belgium": "Europe",
    "Greece": "Europe", "Turkey": "Europe", "Czech Republic": "Europe", "Hungary": "Europe",
    "Romania": "Europe", "Slovakia": "Europe", "Croatia": "Europe", "Bulgaria": "Europe",
    "Serbia": "Europe", "Ukraine": "Europe", "Russia": "Europe", "Luxembourg": "Europe",
    "Estonia": "Europe", "Latvia": "Europe", "Lithuania": "Europe", "Slovenia": "Europe",
    "Albania": "Europe", "North Macedonia": "Europe", "Bosnia and Herzegovina": "Europe",
    "Belarus": "Europe", "Moldova": "Europe", "Kosovo": "Europe", "Montenegro": "Europe",
    "Cyprus": "Europe", "Malta": "Europe", "Iceland": "Europe", "Liechtenstein": "Europe",
    "Monaco": "Europe", "San Marino": "Europe", "Andorra": "Europe",
    # Asia-South Pacific
    "India": "Asia-South Pacific", "Australia": "Asia-South Pacific",
    "New Zealand": "Asia-South Pacific", "Singapore": "Asia-South Pacific",
    "Malaysia": "Asia-South Pacific", "Indonesia": "Asia-South Pacific",
    "Thailand": "Asia-South Pacific", "Vietnam": "Asia-South Pacific",
    "Philippines": "Asia-South Pacific", "Pakistan": "Asia-South Pacific",
    "Bangladesh": "Asia-South Pacific", "Sri Lanka": "Asia-South Pacific",
    "Nepal": "Asia-South Pacific", "Myanmar": "Asia-South Pacific",
    "Cambodia": "Asia-South Pacific", "Laos": "Asia-South Pacific",
    "Brunei": "Asia-South Pacific", "Maldives": "Asia-South Pacific",
    "Bhutan": "Asia-South Pacific", "Papua New Guinea": "Asia-South Pacific",
    "Fiji": "Asia-South Pacific", "Timor-Leste": "Asia-South Pacific",
    "Solomon Islands": "Asia-South Pacific", "Vanuatu": "Asia-South Pacific",
    "Samoa": "Asia-South Pacific", "Tonga": "Asia-South Pacific",
    "Kiribati": "Asia-South Pacific", "Micronesia": "Asia-South Pacific",
    "Marshall Islands": "Asia-South Pacific", "Palau": "Asia-South Pacific",
    "Nauru": "Asia-South Pacific", "Tuvalu": "Asia-South Pacific",
    "Afghanistan": "Asia-South Pacific",
    # Middle East & Africa
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
    "Sudan": "Middle East & Africa", "Ivory Coast": "Middle East & Africa",
    "Senegal": "Middle East & Africa", "Cameroon": "Middle East & Africa",
    "Angola": "Middle East & Africa", "Mozambique": "Middle East & Africa",
    "Zambia": "Middle East & Africa", "Zimbabwe": "Middle East & Africa",
    "Rwanda": "Middle East & Africa", "Botswana": "Middle East & Africa",
    "Namibia": "Middle East & Africa", "Mauritius": "Middle East & Africa",
    "Syria": "Middle East & Africa", "Palestine": "Middle East & Africa",
    "Somalia": "Middle East & Africa", "South Sudan": "Middle East & Africa",
    "Chad": "Middle East & Africa", "Niger": "Middle East & Africa",
    "Mali": "Middle East & Africa", "Burkina Faso": "Middle East & Africa",
    "Guinea": "Middle East & Africa", "Sierra Leone": "Middle East & Africa",
    "Liberia": "Middle East & Africa", "Togo": "Middle East & Africa",
    "Benin": "Middle East & Africa", "Gabon": "Middle East & Africa",
    "Equatorial Guinea": "Middle East & Africa", "Congo": "Middle East & Africa",
    "Democratic Republic of the Congo": "Middle East & Africa",
    "Central African Republic": "Middle East & Africa",
    "Madagascar": "Middle East & Africa", "Malawi": "Middle East & Africa",
    "Lesotho": "Middle East & Africa", "Eswatini": "Middle East & Africa",
    "Djibouti": "Middle East & Africa", "Eritrea": "Middle East & Africa",
    "Comoros": "Middle East & Africa", "Seychelles": "Middle East & Africa",
    "Cape Verde": "Middle East & Africa", "Sao Tome and Principe": "Middle East & Africa",
    "Guinea-Bissau": "Middle East & Africa", "Gambia": "Middle East & Africa",
    "Mauritania": "Middle East & Africa",
    # Latin America
    "Brazil": "Latin America", "Argentina": "Latin America", "Chile": "Latin America",
    "Colombia": "Latin America", "Peru": "Latin America", "Ecuador": "Latin America",
    "Venezuela": "Latin America", "Uruguay": "Latin America", "Paraguay": "Latin America",
    "Bolivia": "Latin America", "Panama": "Latin America", "Costa Rica": "Latin America",
    "Dominican Republic": "Latin America", "Guatemala": "Latin America",
    "Honduras": "Latin America", "El Salvador": "Latin America",
    "Nicaragua": "Latin America", "Cuba": "Latin America", "Haiti": "Latin America",
    "Jamaica": "Latin America", "Trinidad and Tobago": "Latin America",
    "Barbados": "Latin America", "Guyana": "Latin America", "Suriname": "Latin America",
    "Belize": "Latin America", "Bahamas": "Latin America",
    "Saint Lucia": "Latin America", "Grenada": "Latin America",
    "Saint Vincent and the Grenadines": "Latin America",
    "Antigua and Barbuda": "Latin America", "Dominica": "Latin America",
    "Saint Kitts and Nevis": "Latin America",
    # China
    "China": "China", "Hong Kong": "China", "Macau": "China",
    # North Asia
    "Japan": "North Asia", "South Korea": "North Asia", "Taiwan": "North Asia",
    "Mongolia": "North Asia", "North Korea": "North Asia",
    # Global
    "Global": "Global",
}


# =========================================================
# GEO INFERENCE — 100% Gemini powered, zero hardcoded lookups
# Gemini resolves ANY location input (city/village/town/street/
# district/state/country) to a sovereign country using its
# built-in world knowledge and chain-of-thought reasoning.
# =========================================================

def infer_geo_from_area(area: str) -> dict:
    """
    Fully Gemini-powered geo resolution. No hardcoded city/state lists.

    Call 1 — PRIMARY: chain-of-thought prompt asking Gemini to reason
              step by step before returning a country. Works for Tier 1/2/3
              cities, villages, streets, districts — anything.

    Call 2 — RECOVERY: fires only if Call 1 returns something not in
              COUNTRY_TO_REGION. Gives Gemini a second chance with a
              more explicit focused prompt.

    Fallback — only if BOTH Gemini calls fail due to network/API issues.
               Returns area as-is with Global region. Never crashes the app.
    """
    area_clean = area.strip()

    # ── CALL 1: PRIMARY — chain-of-thought reasoning ───────────────────────
    prompt_1 = (
        "You are a world geography expert with complete knowledge of every city, "
        "town, village, district, street, neighbourhood, state, and country on Earth.\n\n"
        "Task: Identify the SOVEREIGN COUNTRY that contains the given location.\n\n"
        "The input can be ANYTHING — a major city, a small village, a street name, "
        "a district, a neighbourhood, a state, a territory, or a country name itself.\n\n"
        "Think step by step:\n"
        "1. What type of place is this? (city / town / village / street / district / state / country)\n"
        "2. Which sovereign country does it belong to?\n"
        "3. If uncertain, use the linguistic and cultural origin of the name as a clue.\n\n"
        "STRICT RULES:\n"
        "- The \"country\" field MUST be a sovereign nation — never a city, state, or district\n"
        "- Write the full country name in English (e.g. \"United States\", not \"US\" or \"USA\")\n"
        "- If already a country name, return it exactly in English\n"
        "- Only return \"Global\" if the input is completely non-geographic\n\n"
        "Examples:\n"
        "Bhagalpur → {reasoning: city in Bihar, India, country: India}\n"
        "Wollongong → {reasoning: coastal city in New South Wales, country: Australia}\n"
        "Canberra → {reasoning: capital city of Australia, country: Australia}\n"
        "Ribeirao Preto → {reasoning: city in Sao Paulo state, country: Brazil}\n"
        "Onitsha → {reasoning: city in Anambra state, country: Nigeria}\n"
        "Multan → {reasoning: city in Punjab province, country: Pakistan}\n"
        "Antananarivo → {reasoning: capital of Madagascar, country: Madagascar}\n"
        "Guadalajara → {reasoning: city in Jalisco state, country: Mexico}\n"
        "Chengdu → {reasoning: city in Sichuan province, country: China}\n"
        "MG Road → {reasoning: street name common in Indian cities, country: India}\n"
        "Shibuya → {reasoning: district in Tokyo, country: Japan}\n"
        "Mitte → {reasoning: central district of Berlin, country: Germany}\n\n"
        "Location: \"" + area_clean + "\"\n\n"
        "Return ONLY this JSON, nothing else:\n"
        "{\"reasoning\": \"<one sentence explaining what this place is>\", "
        "\"country\": \"<sovereign country name in full English>\"}"
    )

    country = None
    try:
        r1      = client.models.generate_content(model="gemini-2.5-flash", contents=prompt_1)
        raw     = r1.text.strip()
        raw     = re.sub(r"```[a-zA-Z]*", "", raw).replace("```", "").strip()
        match   = re.search(r'\{.*?\}', raw, re.DOTALL)
        parsed  = json.loads(match.group() if match else raw)
        country = parsed.get("country", "").strip()
        reasoning = parsed.get("reasoning", "")
        print(f"[GEO] Call 1 | reasoning: {reasoning} | country: {country}")

        if country and country in COUNTRY_TO_REGION:
            region = COUNTRY_TO_REGION[country]
            print(f"[GEO] Resolved: {country} → {region}")
            return {"Requesting Countries": country, "Requesting Regions": region, "Bill2Country": country}

        # Country returned but not in our map — still valid, just use Global for region
        if country and country not in ("", "Global"):
            print(f"[GEO] Country '{country}' not in region map — returning with Global region")
            return {"Requesting Countries": country, "Requesting Regions": "Global", "Bill2Country": country}

    except Exception as e:
        print(f"[GEO] Call 1 failed: {type(e).__name__}: {e}")

    # ── CALL 2: RECOVERY — focused retry when Call 1 gives wrong result ────
    print(f"[GEO] Call 2 Recovery triggered for: '{area_clean}'")
    prompt_2 = (
        "Geography question. Give a one-word or short-phrase answer only.\n\n"
        "Location: \"" + area_clean + "\"\n\n"
        "This could be a small village, minor town, district, neighbourhood, "
        "street, or any lesser-known place anywhere in the world.\n\n"
        "What is the SOVEREIGN COUNTRY this location belongs to?\n\n"
        "Rules:\n"
        "- Return the COUNTRY name only — not city, not state, not district\n"
        "- Full English name (e.g. United States, not USA)\n"
        "- Use linguistic, cultural, or regional context if needed\n\n"
        "Return ONLY this JSON:\n"
        "{\"country\": \"<country name>\"}"
    )
    try:
        r2      = client.models.generate_content(model="gemini-2.5-flash", contents=prompt_2)
        raw2    = r2.text.strip()
        raw2    = re.sub(r"```[a-zA-Z]*", "", raw2).replace("```", "").strip()
        match2  = re.search(r'\{.*?\}', raw2, re.DOTALL)
        parsed2 = json.loads(match2.group() if match2 else raw2)
        country2 = parsed2.get("country", "").strip()
        print(f"[GEO] Call 2 Recovery country: {country2}")

        if country2 and country2 in COUNTRY_TO_REGION:
            region2 = COUNTRY_TO_REGION[country2]
            print(f"[GEO] Recovered: {country2} → {region2}")
            return {"Requesting Countries": country2, "Requesting Regions": region2, "Bill2Country": country2}

        if country2 and country2 not in ("", "Global"):
            print(f"[GEO] Recovery country '{country2}' not in region map")
            return {"Requesting Countries": country2, "Requesting Regions": "Global", "Bill2Country": country2}

    except Exception as e:
        print(f"[GEO] Call 2 Recovery failed: {type(e).__name__}: {e}")

    # ── FALLBACK — only if BOTH Gemini calls fail (network/API issue) ──────
    print(f"[GEO] Both Gemini calls failed for '{area_clean}' — using safe fallback")
    return {
        "Requesting Countries": area_clean.title(),
        "Requesting Regions":   "Global",
        "Bill2Country":         area_clean.title()
    }


# =========================================================
# PDF GENERATION
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

AUTO_POPULATED_KEYS  = {"Requesting Countries", "Requesting Regions", "Bill2Country"}
SYSTEM_KEYS          = {"Demand ID", "Demand Timestamp"}


def generate_demand_pdf(payload: dict) -> BytesIO:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=20*mm, bottomMargin=20*mm,
    )
    styles = getSampleStyleSheet()

    style_title = ParagraphStyle("DT", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=20, textColor=WHITE, alignment=TA_CENTER, spaceAfter=2)
    style_subtitle = ParagraphStyle("DS", parent=styles["Normal"],
        fontName="Helvetica", fontSize=8.5, textColor=colors.HexColor("#93C5FD"), alignment=TA_CENTER)
    style_demandid = ParagraphStyle("DID", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=10, textColor=colors.HexColor("#FDE68A"), alignment=TA_CENTER)
    style_section = ParagraphStyle("SH", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=10, textColor=WHITE)
    style_label = ParagraphStyle("FL", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=8.5, textColor=MUTED, spaceAfter=1)
    style_value = ParagraphStyle("FV", parent=styles["Normal"],
        fontName="Helvetica", fontSize=9.5, textColor=DARK_TEXT, leading=13)
    style_auto = ParagraphStyle("AV", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=9.5, textColor=PURPLE_TXT, leading=13)
    style_sys = ParagraphStyle("SV", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=9.5, textColor=TEAL_TXT, leading=13)
    style_footer = ParagraphStyle("FT", parent=styles["Normal"],
        fontName="Helvetica", fontSize=7.5, textColor=MUTED, alignment=TA_CENTER)

    story = []

    demand_id   = payload.get("Demand ID", "N/A")
    timestamp   = payload.get("Demand Timestamp", "N/A")
    demand_title= payload.get("Demand Title", "Demand Request")

    # Header banner
    header_data = [
        [Paragraph("DEMAND CREATION REQUEST", style_title)],
        [Paragraph(f"Demand ID: {demand_id}", style_demandid)],
        [Paragraph(
            f"Recorded: {timestamp}  |  Status: {payload.get('Demand Status','New')}  |  Type: {payload.get('SubmissionType','Chatbot')}",
            style_subtitle
        )],
    ]
    header_table = Table(header_data, colWidths=[174*mm])
    header_table.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), NAVY),
        ("TOPPADDING",    (0,0), (-1,0), 14),
        ("BOTTOMPADDING", (0,0), (-1,0), 2),
        ("TOPPADDING",    (0,1), (-1,1), 2),
        ("BOTTOMPADDING", (0,1), (-1,1), 2),
        ("TOPPADDING",    (0,2), (-1,2), 0),
        ("BOTTOMPADDING", (0,2), (-1,2), 12),
        ("ALIGN",         (0,0), (-1,-1), "CENTER"),
        ("ROUNDEDCORNERS",[6]),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 4*mm))

    # Demand title card
    title_card = Table(
        [[Paragraph(f"<b>{demand_title}</b>", ParagraphStyle("TC",
            fontName="Helvetica-Bold", fontSize=13, textColor=NAVY, alignment=TA_CENTER))]],
        colWidths=[174*mm]
    )
    title_card.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), MID_BG),
        ("TOPPADDING",    (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
        ("BOX",           (0,0), (-1,-1), 1, BLUE),
        ("ROUNDEDCORNERS",[6]),
    ]))
    story.append(title_card)
    story.append(Spacer(1, 5*mm))

    single_key_fields = {
        "Demand Title", "Requirement summary", "Rationale and Purpose",
        "Acceptance Criteria", "Business Benefits",
        "Risks if not implemented on Target date"
    }

    for section_name, keys in SECTIONS.items():
        sec_header = Table(
            [[Paragraph(f"  {section_name.upper()}", style_section)]],
            colWidths=[174*mm]
        )
        sec_header.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,-1), BLUE),
            ("TOPPADDING",    (0,0), (-1,-1), 6),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
            ("ROUNDEDCORNERS",[4]),
        ]))

        field_rows = []
        i = 0
        while i < len(keys):
            key = keys[i]
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
                    nv       = payload.get(next_key)
                    nis_auto = next_key in AUTO_POPULATED_KEYS
                    nis_sys  = next_key in SYSTEM_KEYS
                    nl_p     = Paragraph(next_key.replace("_", " ").upper(), style_label)
                    if nis_auto:
                        nv_p   = Paragraph(f"&#9733; {nv}  <font size='7' color='#7C3AED'>[Auto]</font>", style_auto)
                        nbg    = PURPLE_BG
                    elif nis_sys:
                        nv_p   = Paragraph(f"&#128273; {nv}", style_sys)
                        nbg    = TEAL_BG
                    else:
                        nv_p   = Paragraph(str(nv), style_value)
                        nbg    = LIGHT_BG
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
                    ("BACKGROUND",   (0,ri),(0,ri), bg),
                    ("BACKGROUND",   (1,ri),(1,ri), nbg),
                    ("TOPPADDING",   (0,ri),(1,ri), 1),
                ]
            ri += 1

        field_table = Table(tbl_data, colWidths=[87*mm, 87*mm])
        field_table.setStyle(TableStyle(tbl_styles))

        story.append(KeepTogether([
            sec_header,
            Spacer(1, 2*mm),
            field_table,
            Spacer(1, 4*mm),
        ]))

    story.append(HRFlowable(width="100%", thickness=0.5, color=GREY_LINE))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        f"Demand ID: {demand_id}  |  Recorded: {timestamp}  |  "
        f"Auto-generated by Demand Creation Chatbot powered by Gemini 2.5 Flash",
        style_footer
    ))

    doc.build(story)
    buffer.seek(0)
    return buffer


# =========================================================
# FLASK APP
# =========================================================

app = Flask(__name__)


@app.route("/start", methods=["GET"])
def start():
    reset_session()
    first = MANDATORY_FIELDS[0]
    return jsonify({
        "message": "Welcome to the Demand Creation Chatbot! Let's capture your demand step by step.",
        "next_question": first["question"],
        "field": first["key"],
        "progress": f"1 of {len(MANDATORY_FIELDS)}"
    })


@app.route("/chat", methods=["POST"])
def chat():
    payload = request.get_json(silent=True)
    if not payload or "message" not in payload:
        return jsonify({"error": "message field is required"}), 400
    message = payload["message"].strip()
    if not message:
        return jsonify({"error": "Empty message received"}), 400

    current_field = get_next_field()
    if not current_field:
        return jsonify({"error": "Session already complete. Call /start to reset."}), 400

    session_state["captured"][current_field["key"]] = message
    session_state["completed"].append(current_field["key"])

    if current_field["key"] == "Area":
        geo = infer_geo_from_area(message)
        session_state["captured"]["Requesting Regions"]   = geo.get("Requesting Regions", message)
        session_state["captured"]["Requesting Countries"] = geo.get("Requesting Countries", message)
        session_state["captured"]["Bill2Country"]         = geo.get("Bill2Country", message)

    next_field = get_next_field()
    answered   = len(session_state["completed"])
    total      = len(MANDATORY_FIELDS)

    if next_field:
        return jsonify({
            "captured_field": current_field["key"],
            "captured_value": message,
            "next_question":  next_field["question"],
            "field":          next_field["key"],
            "progress":       f"{answered + 1} of {total}"
        })

    # ── All fields captured — build final payload ──────────────────────────
    final = dict(session_state["captured"])
    final["Demand Status"]    = "New"
    final["SubmissionType"]   = "Chatbot"
    final["Demand ID"]        = generate_demand_id()     # e.g. 2605_DEMO0001
    final["Demand Timestamp"] = generate_timestamp()     # e.g. 07-May-2026 10:45 AM

    if "Requesting BU" in final and "Area" in final:
        final["Demand_route"] = f"{final['Requesting BU']} - {final['Area']}"

    session_state["last_payload"] = final

    # Save to Azure SQL Database (non-blocking — app works even if this fails)
    db_saved = save_to_db(final)

    auto = {
        "Requesting Regions":   final.get("Requesting Regions"),
        "Requesting Countries": final.get("Requesting Countries"),
        "Bill2Country":         final.get("Bill2Country")
    }
    reset_session()
    return jsonify({
        "status":              "All mandatory fields captured successfully!",
        "db_saved":            db_saved,
        "auto_populated":      auto,
        "demand_id":           final["Demand ID"],
        "demand_timestamp":    final["Demand Timestamp"],
        "final_demand_payload": final
    })


@app.route("/download-pdf", methods=["POST"])
def download_pdf():
    payload = request.get_json(silent=True)
    if not payload:
        payload = session_state.get("last_payload", {})
    if not payload:
        return jsonify({"error": "No payload available"}), 400
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
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Demand Creation Chatbot</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<style>
  :root {
    --bg:#f0f4ff;--surface:#fff;--primary:#2563eb;--accent:#7c3aed;
    --user-bg:#2563eb;--bot-bg:#f1f5f9;--bot-text:#1e293b;--user-text:#fff;
    --border:#e2e8f0;--muted:#94a3b8;--radius:16px;
    --shadow:0 4px 24px rgba(37,99,235,0.08);
  }
  *{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:'DM Sans',sans-serif;background:var(--bg);min-height:100vh;
    display:flex;align-items:center;justify-content:center;padding:20px;}
  .container{width:100%;max-width:760px;background:var(--surface);
    border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden;
    display:flex;flex-direction:column;height:90vh;max-height:700px;}
  .header{background:linear-gradient(135deg,var(--primary),var(--accent));
    padding:20px 28px;color:white;display:flex;align-items:center;
    justify-content:space-between;gap:12px;}
  .header-left h1{font-size:18px;font-weight:600;letter-spacing:-0.3px;}
  .header-left p{font-size:12px;opacity:0.8;margin-top:2px;}
  .reset-btn{background:rgba(255,255,255,0.2);border:1px solid rgba(255,255,255,0.3);
    color:white;padding:6px 14px;border-radius:8px;cursor:pointer;
    font-size:12px;font-family:inherit;transition:background 0.2s;}
  .reset-btn:hover{background:rgba(255,255,255,0.3);}
  .progress-wrap{background:#e8eeff;padding:10px 28px;display:flex;
    align-items:center;gap:12px;border-bottom:1px solid var(--border);}
  .progress-bar-bg{flex:1;height:6px;background:#dde4ff;border-radius:99px;overflow:hidden;}
  .progress-bar-fill{height:100%;background:linear-gradient(90deg,var(--primary),var(--accent));
    border-radius:99px;transition:width 0.4s ease;width:0%;}
  .progress-label{font-size:11px;color:var(--primary);font-weight:600;
    white-space:nowrap;font-family:'DM Mono',monospace;}
  .chat-area{flex:1;overflow-y:auto;padding:24px 28px;display:flex;
    flex-direction:column;gap:14px;scroll-behavior:smooth;}
  .bubble-row{display:flex;align-items:flex-end;gap:10px;animation:fadeUp 0.3s ease;}
  .bubble-row.user{flex-direction:row-reverse;}
  @keyframes fadeUp{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
  .avatar{width:32px;height:32px;border-radius:50%;display:flex;
    align-items:center;justify-content:center;font-size:14px;flex-shrink:0;}
  .avatar.bot{background:linear-gradient(135deg,var(--primary),var(--accent));}
  .avatar.user{background:#e0e7ff;}
  .bubble{max-width:78%;padding:12px 16px;border-radius:14px;
    font-size:14px;line-height:1.55;word-break:break-word;}
  .bubble.bot{background:var(--bot-bg);color:var(--bot-text);border-bottom-left-radius:4px;}
  .bubble.user{background:var(--user-bg);color:var(--user-text);border-bottom-right-radius:4px;}
  .field-tag{font-size:10px;font-weight:600;font-family:'DM Mono',monospace;
    color:var(--muted);text-transform:uppercase;letter-spacing:0.8px;margin-bottom:4px;}
  .demand-id-badge{display:inline-block;background:#0f172a;color:#fde68a;
    font-family:'DM Mono',monospace;font-size:12px;font-weight:700;
    padding:5px 14px;border-radius:8px;margin-bottom:8px;letter-spacing:1px;}
  .timestamp-badge{display:inline-block;background:#ccfbf1;color:#0f766e;
    font-family:'DM Mono',monospace;font-size:11px;font-weight:600;
    padding:3px 10px;border-radius:6px;margin-bottom:10px;margin-left:6px;}
  .final-box{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:12px;
    padding:16px;font-size:12px;font-family:'DM Mono',monospace;color:#064e3b;
    white-space:pre-wrap;word-break:break-all;max-height:260px;overflow-y:auto;margin-bottom:12px;}
  .auto-pop-tag{display:inline-block;background:#ede9fe;color:var(--accent);
    font-size:10px;font-weight:600;font-family:'DM Mono',monospace;
    padding:2px 8px;border-radius:99px;margin-bottom:6px;}
  .pdf-btn{display:inline-flex;align-items:center;gap:7px;
    background:linear-gradient(135deg,#059669,#047857);
    color:white;border:none;border-radius:10px;padding:10px 20px;
    font-size:13px;font-weight:600;font-family:inherit;
    cursor:pointer;transition:opacity 0.2s,transform 0.1s;margin-top:4px;}
  .pdf-btn:hover{opacity:0.9;} .pdf-btn:active{transform:scale(0.97);}
  .pdf-btn:disabled{opacity:0.5;cursor:not-allowed;}
  .typing{display:flex;gap:4px;align-items:center;padding:12px 16px;
    background:var(--bot-bg);border-radius:14px;border-bottom-left-radius:4px;width:fit-content;}
  .typing span{width:7px;height:7px;background:var(--muted);border-radius:50%;animation:bounce 1.2s infinite;}
  .typing span:nth-child(2){animation-delay:0.2s;} .typing span:nth-child(3){animation-delay:0.4s;}
  @keyframes bounce{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-6px)}}
  .input-area{padding:16px 28px 20px;border-top:1px solid var(--border);
    display:flex;gap:10px;background:var(--surface);}
  .input-area input{flex:1;padding:12px 16px;border:1.5px solid var(--border);
    border-radius:10px;font-size:14px;font-family:inherit;outline:none;
    transition:border 0.2s;color:var(--bot-text);}
  .input-area input:focus{border-color:var(--primary);}
  .input-area input::placeholder{color:var(--muted);}
  .send-btn{background:linear-gradient(135deg,var(--primary),var(--accent));
    color:white;border:none;border-radius:10px;padding:12px 22px;
    font-size:14px;font-weight:600;font-family:inherit;cursor:pointer;
    transition:opacity 0.2s,transform 0.1s;}
  .send-btn:hover{opacity:0.92;} .send-btn:active{transform:scale(0.97);}
  .send-btn:disabled{opacity:0.5;cursor:not-allowed;}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="header-left">
      <h1>&#9889; Demand Creation Chatbot</h1>
      <p>Powered by Gemini 2.5 Flash &middot; SharePoint &amp; Google Compatible</p>
    </div>
    <button class="reset-btn" onclick="resetChat()">&#8635; Reset</button>
  </div>
  <div class="progress-wrap">
    <div class="progress-bar-bg"><div class="progress-bar-fill" id="progressBar"></div></div>
    <div class="progress-label" id="progressLabel">Starting...</div>
  </div>
  <div class="chat-area" id="chatArea"></div>
  <div class="input-area">
    <input type="text" id="inp" placeholder="Type your answer..."
           onkeydown="if(event.key==='Enter')sendMsg()"/>
    <button class="send-btn" id="sendBtn" onclick="sendMsg()">Send &rarr;</button>
  </div>
</div>
<script>
const TOTAL=TOTAL_PLACEHOLDER;
let started=false;
let lastPayload=null;

async function startChat(){
  const res=await fetch('/start');
  const d=await res.json();
  addBotMsg(d.message);
  setTimeout(()=>addBotMsg(d.next_question,d.field,d.progress),600);
  started=true;
}

async function sendMsg(){
  const inp=document.getElementById('inp');
  const msg=inp.value.trim();
  if(!msg||!started)return;
  addUserMsg(msg); inp.value='';
  document.getElementById('sendBtn').disabled=true;
  const tid=showTyping();
  try{
    const res=await fetch('/chat',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:msg})
    });
    const d=await res.json();
    removeTyping(tid);

    if(d.error){
      addBotMsg('&#9888; '+d.error);
    } else if(d.final_demand_payload){
      lastPayload=d.final_demand_payload;
      const auto=d.auto_populated;

      // Auto-populated geo callout
      addBotMsg(
        '<span class="auto-pop-tag">&#10024; Auto-populated by Gemini</span><br/>'
        +'<b>Requesting Regions:</b> '+auto['Requesting Regions']+'<br/>'
        +'<b>Requesting Countries:</b> '+auto['Requesting Countries']+'<br/>'
        +'<b>Bill2Country:</b> '+auto['Bill2Country']
      );

      // Final payload with Demand ID and Timestamp prominently shown
      setTimeout(()=>{
        addBotMsg(
          '<b>&#10003; '+d.status+'</b><br/><br/>'
          +'<span class="demand-id-badge">&#128273; Demand ID: '+d.demand_id+'</span>'
          +'<span class="timestamp-badge">&#128336; '+d.demand_timestamp+'</span>'
          +'<br/><br/>Complete Demand Payload:<br/><br/>'
          +'<div class="final-box">'+JSON.stringify(d.final_demand_payload,null,2)+'</div>'
          +'<button class="pdf-btn" onclick="downloadPDF()">&#128196; Download as PDF</button>'
        );
        updateProgress('Done',100);
        document.getElementById('inp').disabled=true;
        document.getElementById('sendBtn').disabled=true;
      },700);
    } else {
      addBotMsg(d.next_question,d.field,d.progress);
    }
  } catch(e){
    removeTyping(tid);
    addBotMsg('Network error. Please try again.');
  }
  document.getElementById('sendBtn').disabled=false;
  document.getElementById('inp').focus();
}

async function downloadPDF(){
  if(!lastPayload){alert('No payload available.');return;}
  const btn=document.querySelector('.pdf-btn');
  if(btn){btn.disabled=true;btn.textContent='Generating PDF...';}
  try{
    const res=await fetch('/download-pdf',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(lastPayload)
    });
    if(!res.ok){throw new Error('PDF generation failed');}
    const blob=await res.blob();
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');
    a.href=url;
    a.download=(lastPayload['Demand ID']||'Demand')+'_'+
               (lastPayload['Demand Title']||'Request').replace(/\s+/g,'_').slice(0,30)+'.pdf';
    a.click();
    URL.revokeObjectURL(url);
  } catch(e){
    alert('Error generating PDF: '+e.message);
  } finally {
    if(btn){btn.disabled=false;btn.innerHTML='&#128196; Download as PDF';}
  }
}

function addBotMsg(html,field,progress){
  const area=document.getElementById('chatArea');
  const row=document.createElement('div'); row.className='bubble-row bot';
  let inner=''; if(field) inner='<div class="field-tag">'+field+'</div>'; inner+=html;
  row.innerHTML='<div class="avatar bot">&#129302;</div><div class="bubble bot">'+inner+'</div>';
  area.appendChild(row); area.scrollTop=area.scrollHeight;
  if(progress) updateProgress(progress);
}
function addUserMsg(text){
  const area=document.getElementById('chatArea');
  const row=document.createElement('div'); row.className='bubble-row user';
  row.innerHTML='<div class="avatar user">&#129489;</div><div class="bubble user">'+text+'</div>';
  area.appendChild(row); area.scrollTop=area.scrollHeight;
}
function showTyping(){
  const area=document.getElementById('chatArea');
  const row=document.createElement('div'); const id='t'+Date.now(); row.id=id;
  row.className='bubble-row bot';
  row.innerHTML='<div class="avatar bot">&#129302;</div>'
    +'<div class="typing"><span></span><span></span><span></span></div>';
  area.appendChild(row); area.scrollTop=area.scrollHeight; return id;
}
function removeTyping(id){const el=document.getElementById(id);if(el)el.remove();}
function updateProgress(label,pct){
  const num=pct!==undefined?pct:(()=>{
    const p=label.split(' of ');
    return p.length===2?Math.round(parseInt(p[0])/TOTAL*100):0;
  })();
  document.getElementById('progressBar').style.width=num+'%';
  document.getElementById('progressLabel').textContent=pct===100?'&#10003; Complete':'Q '+label;
}
async function resetChat(){
  await fetch('/reset',{method:'POST'});
  document.getElementById('chatArea').innerHTML='';
  document.getElementById('inp').disabled=false;
  document.getElementById('sendBtn').disabled=false;
  lastPayload=null;
  updateProgress('',0); started=false; startChat();
}
startChat();
</script>
</body>
</html>"""
    html = html.replace("TOTAL_PLACEHOLDER", str(total_fields))
    return render_template_string(html)


# =========================================================
# START SERVER
# =========================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
