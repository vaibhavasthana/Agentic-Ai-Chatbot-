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

# Increments every time a demand is fully completed in this server session
_demand_counter = 0


def generate_demand_id() -> str:
    """
    Auto-generates a unique Demand ID. Format: YYMM_DEMO####
    e.g. 2605_DEMO0001 = first demand created in May 2026
    Acts as primary key for each BRD / demand record.
    """
    global _demand_counter
    _demand_counter += 1
    prefix = datetime.now().strftime("%y%m")           # YY + MM  e.g. 2605
    return f"{prefix}_DEMO{_demand_counter:04d}"        # e.g. 2605_DEMO0001


def generate_timestamp() -> str:
    """
    Records the exact date and time the demand was submitted.
    Format: DD-Mon-YYYY HH:MM AM/PM  e.g. 07-May-2026 10:45 AM
    """
    return datetime.now().strftime("%d-%b-%Y %I:%M %p")


def reset_session():
    session_state["captured"]  = {}
    session_state["completed"] = []


def get_next_field():
    for field in MANDATORY_FIELDS:
        if field["key"] not in session_state["completed"]:
            return field
    return None


# =========================================================
# COUNTRY TO REGION MAP
# Used by both Gemini path and local lookup path.
# Python maps country → enterprise region deterministically.
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
    "Somalia": "Middle East & Africa",
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
    # China
    "China": "China", "Hong Kong": "China", "Macau": "China",
    # North Asia
    "Japan": "North Asia", "South Korea": "North Asia", "Taiwan": "North Asia",
    "Mongolia": "North Asia",
    # Global
    "Global": "Global",
}


# =========================================================
# LOCAL GEO LOOKUP — Secondary fallback only
# Used ONLY when Gemini API call fails (e.g. org network/proxy issues)
# Covers 230+ cities, states, countries for device-independent reliability
# =========================================================

LOCAL_GEO = {
    # India — States
    "india": ("India", "Asia-South Pacific"),
    "maharashtra": ("India", "Asia-South Pacific"),
    "karnataka": ("India", "Asia-South Pacific"),
    "tamil nadu": ("India", "Asia-South Pacific"),
    "tamilnadu": ("India", "Asia-South Pacific"),
    "gujarat": ("India", "Asia-South Pacific"),
    "rajasthan": ("India", "Asia-South Pacific"),
    "uttar pradesh": ("India", "Asia-South Pacific"),
    "up": ("India", "Asia-South Pacific"),
    "madhya pradesh": ("India", "Asia-South Pacific"),
    "west bengal": ("India", "Asia-South Pacific"),
    "andhra pradesh": ("India", "Asia-South Pacific"),
    "telangana": ("India", "Asia-South Pacific"),
    "kerala": ("India", "Asia-South Pacific"),
    "punjab": ("India", "Asia-South Pacific"),
    "haryana": ("India", "Asia-South Pacific"),
    "bihar": ("India", "Asia-South Pacific"),
    "odisha": ("India", "Asia-South Pacific"),
    "jharkhand": ("India", "Asia-South Pacific"),
    "assam": ("India", "Asia-South Pacific"),
    "goa": ("India", "Asia-South Pacific"),
    "himachal pradesh": ("India", "Asia-South Pacific"),
    "uttarakhand": ("India", "Asia-South Pacific"),
    "chhattisgarh": ("India", "Asia-South Pacific"),
    # India — Cities
    "mumbai": ("India", "Asia-South Pacific"),
    "pune": ("India", "Asia-South Pacific"),
    "bengaluru": ("India", "Asia-South Pacific"),
    "bangalore": ("India", "Asia-South Pacific"),
    "delhi": ("India", "Asia-South Pacific"),
    "new delhi": ("India", "Asia-South Pacific"),
    "hyderabad": ("India", "Asia-South Pacific"),
    "chennai": ("India", "Asia-South Pacific"),
    "kolkata": ("India", "Asia-South Pacific"),
    "ahmedabad": ("India", "Asia-South Pacific"),
    "surat": ("India", "Asia-South Pacific"),
    "jaipur": ("India", "Asia-South Pacific"),
    "lucknow": ("India", "Asia-South Pacific"),
    "noida": ("India", "Asia-South Pacific"),
    "gurgaon": ("India", "Asia-South Pacific"),
    "gurugram": ("India", "Asia-South Pacific"),
    "indore": ("India", "Asia-South Pacific"),
    "bhopal": ("India", "Asia-South Pacific"),
    "nagpur": ("India", "Asia-South Pacific"),
    "patna": ("India", "Asia-South Pacific"),
    "vadodara": ("India", "Asia-South Pacific"),
    "coimbatore": ("India", "Asia-South Pacific"),
    "kochi": ("India", "Asia-South Pacific"),
    "cochin": ("India", "Asia-South Pacific"),
    "thiruvananthapuram": ("India", "Asia-South Pacific"),
    "trivandrum": ("India", "Asia-South Pacific"),
    "vizag": ("India", "Asia-South Pacific"),
    "visakhapatnam": ("India", "Asia-South Pacific"),
    "chandigarh": ("India", "Asia-South Pacific"),
    "mysuru": ("India", "Asia-South Pacific"),
    "mysore": ("India", "Asia-South Pacific"),
    "mangalore": ("India", "Asia-South Pacific"),
    "mangaluru": ("India", "Asia-South Pacific"),
    "nashik": ("India", "Asia-South Pacific"),
    "aurangabad": ("India", "Asia-South Pacific"),
    "thane": ("India", "Asia-South Pacific"),
    "navi mumbai": ("India", "Asia-South Pacific"),
    "faridabad": ("India", "Asia-South Pacific"),
    "ghaziabad": ("India", "Asia-South Pacific"),
    "agra": ("India", "Asia-South Pacific"),
    "varanasi": ("India", "Asia-South Pacific"),
    "kanpur": ("India", "Asia-South Pacific"),
    "meerut": ("India", "Asia-South Pacific"),
    "ranchi": ("India", "Asia-South Pacific"),
    "bhubaneswar": ("India", "Asia-South Pacific"),
    "guwahati": ("India", "Asia-South Pacific"),
    "dehradun": ("India", "Asia-South Pacific"),
    "shimla": ("India", "Asia-South Pacific"),
    "amritsar": ("India", "Asia-South Pacific"),
    "ludhiana": ("India", "Asia-South Pacific"),
    "jalandhar": ("India", "Asia-South Pacific"),
    "raipur": ("India", "Asia-South Pacific"),
    # Middle East & Africa
    "cairo": ("Egypt", "Middle East & Africa"),
    "egypt": ("Egypt", "Middle East & Africa"),
    "dubai": ("United Arab Emirates", "Middle East & Africa"),
    "abu dhabi": ("United Arab Emirates", "Middle East & Africa"),
    "sharjah": ("United Arab Emirates", "Middle East & Africa"),
    "uae": ("United Arab Emirates", "Middle East & Africa"),
    "united arab emirates": ("United Arab Emirates", "Middle East & Africa"),
    "riyadh": ("Saudi Arabia", "Middle East & Africa"),
    "jeddah": ("Saudi Arabia", "Middle East & Africa"),
    "saudi arabia": ("Saudi Arabia", "Middle East & Africa"),
    "nairobi": ("Kenya", "Middle East & Africa"),
    "kenya": ("Kenya", "Middle East & Africa"),
    "lagos": ("Nigeria", "Middle East & Africa"),
    "abuja": ("Nigeria", "Middle East & Africa"),
    "nigeria": ("Nigeria", "Middle East & Africa"),
    "johannesburg": ("South Africa", "Middle East & Africa"),
    "cape town": ("South Africa", "Middle East & Africa"),
    "south africa": ("South Africa", "Middle East & Africa"),
    "doha": ("Qatar", "Middle East & Africa"),
    "qatar": ("Qatar", "Middle East & Africa"),
    "kuwait": ("Kuwait", "Middle East & Africa"),
    "muscat": ("Oman", "Middle East & Africa"),
    "oman": ("Oman", "Middle East & Africa"),
    "casablanca": ("Morocco", "Middle East & Africa"),
    "morocco": ("Morocco", "Middle East & Africa"),
    # Europe
    "london": ("United Kingdom", "Europe"),
    "manchester": ("United Kingdom", "Europe"),
    "uk": ("United Kingdom", "Europe"),
    "united kingdom": ("United Kingdom", "Europe"),
    "england": ("United Kingdom", "Europe"),
    "scotland": ("United Kingdom", "Europe"),
    "berlin": ("Germany", "Europe"),
    "munich": ("Germany", "Europe"),
    "hamburg": ("Germany", "Europe"),
    "frankfurt": ("Germany", "Europe"),
    "bavaria": ("Germany", "Europe"),
    "germany": ("Germany", "Europe"),
    "paris": ("France", "Europe"),
    "lyon": ("France", "Europe"),
    "france": ("France", "Europe"),
    "rome": ("Italy", "Europe"),
    "milan": ("Italy", "Europe"),
    "italy": ("Italy", "Europe"),
    "madrid": ("Spain", "Europe"),
    "barcelona": ("Spain", "Europe"),
    "spain": ("Spain", "Europe"),
    "amsterdam": ("Netherlands", "Europe"),
    "netherlands": ("Netherlands", "Europe"),
    "zurich": ("Switzerland", "Europe"),
    "switzerland": ("Switzerland", "Europe"),
    "warsaw": ("Poland", "Europe"),
    "poland": ("Poland", "Europe"),
    "stockholm": ("Sweden", "Europe"),
    "sweden": ("Sweden", "Europe"),
    "oslo": ("Norway", "Europe"),
    "norway": ("Norway", "Europe"),
    "copenhagen": ("Denmark", "Europe"),
    "denmark": ("Denmark", "Europe"),
    "helsinki": ("Finland", "Europe"),
    "finland": ("Finland", "Europe"),
    "dublin": ("Ireland", "Europe"),
    "ireland": ("Ireland", "Europe"),
    "lisbon": ("Portugal", "Europe"),
    "portugal": ("Portugal", "Europe"),
    "vienna": ("Austria", "Europe"),
    "austria": ("Austria", "Europe"),
    "brussels": ("Belgium", "Europe"),
    "belgium": ("Belgium", "Europe"),
    "istanbul": ("Turkey", "Europe"),
    "turkey": ("Turkey", "Europe"),
    "athens": ("Greece", "Europe"),
    "greece": ("Greece", "Europe"),
    "prague": ("Czech Republic", "Europe"),
    "budapest": ("Hungary", "Europe"),
    # North America
    "usa": ("United States", "North America"),
    "united states": ("United States", "North America"),
    "america": ("United States", "North America"),
    "california": ("United States", "North America"),
    "texas": ("United States", "North America"),
    "oklahoma": ("United States", "North America"),
    "new york": ("United States", "North America"),
    "florida": ("United States", "North America"),
    "illinois": ("United States", "North America"),
    "new jersey": ("United States", "North America"),
    "georgia": ("United States", "North America"),
    "north carolina": ("United States", "North America"),
    "washington": ("United States", "North America"),
    "michigan": ("United States", "North America"),
    "ohio": ("United States", "North America"),
    "los angeles": ("United States", "North America"),
    "chicago": ("United States", "North America"),
    "houston": ("United States", "North America"),
    "new york city": ("United States", "North America"),
    "nyc": ("United States", "North America"),
    "san francisco": ("United States", "North America"),
    "seattle": ("United States", "North America"),
    "boston": ("United States", "North America"),
    "canada": ("Canada", "North America"),
    "ontario": ("Canada", "North America"),
    "toronto": ("Canada", "North America"),
    "vancouver": ("Canada", "North America"),
    "montreal": ("Canada", "North America"),
    "mexico": ("Mexico", "North America"),
    "mexico city": ("Mexico", "North America"),
    # Latin America
    "brazil": ("Brazil", "Latin America"),
    "sao paulo": ("Brazil", "Latin America"),
    "são paulo": ("Brazil", "Latin America"),
    "rio de janeiro": ("Brazil", "Latin America"),
    "argentina": ("Argentina", "Latin America"),
    "buenos aires": ("Argentina", "Latin America"),
    "colombia": ("Colombia", "Latin America"),
    "bogota": ("Colombia", "Latin America"),
    "chile": ("Chile", "Latin America"),
    "santiago": ("Chile", "Latin America"),
    "peru": ("Peru", "Latin America"),
    "lima": ("Peru", "Latin America"),
    # Asia-South Pacific (non-India)
    "australia": ("Australia", "Asia-South Pacific"),
    "sydney": ("Australia", "Asia-South Pacific"),
    "melbourne": ("Australia", "Asia-South Pacific"),
    "singapore": ("Singapore", "Asia-South Pacific"),
    "malaysia": ("Malaysia", "Asia-South Pacific"),
    "kuala lumpur": ("Malaysia", "Asia-South Pacific"),
    "indonesia": ("Indonesia", "Asia-South Pacific"),
    "jakarta": ("Indonesia", "Asia-South Pacific"),
    "thailand": ("Thailand", "Asia-South Pacific"),
    "bangkok": ("Thailand", "Asia-South Pacific"),
    "vietnam": ("Vietnam", "Asia-South Pacific"),
    "ho chi minh": ("Vietnam", "Asia-South Pacific"),
    "hanoi": ("Vietnam", "Asia-South Pacific"),
    "philippines": ("Philippines", "Asia-South Pacific"),
    "manila": ("Philippines", "Asia-South Pacific"),
    "pakistan": ("Pakistan", "Asia-South Pacific"),
    "karachi": ("Pakistan", "Asia-South Pacific"),
    "lahore": ("Pakistan", "Asia-South Pacific"),
    "bangladesh": ("Bangladesh", "Asia-South Pacific"),
    "dhaka": ("Bangladesh", "Asia-South Pacific"),
    "sri lanka": ("Sri Lanka", "Asia-South Pacific"),
    "colombo": ("Sri Lanka", "Asia-South Pacific"),
    "new zealand": ("New Zealand", "Asia-South Pacific"),
    "auckland": ("New Zealand", "Asia-South Pacific"),
    # China
    "china": ("China", "China"),
    "shanghai": ("China", "China"),
    "beijing": ("China", "China"),
    "shenzhen": ("China", "China"),
    "guangzhou": ("China", "China"),
    "hong kong": ("Hong Kong", "China"),
    "macau": ("Macau", "China"),
    # North Asia
    "japan": ("Japan", "North Asia"),
    "tokyo": ("Japan", "North Asia"),
    "osaka": ("Japan", "North Asia"),
    "south korea": ("South Korea", "North Asia"),
    "seoul": ("South Korea", "North Asia"),
    "taiwan": ("Taiwan", "North Asia"),
    "taipei": ("Taiwan", "North Asia"),
    # Global
    "global": ("Global", "Global"),
    "worldwide": ("Global", "Global"),
    "all regions": ("Global", "Global"),
}


# =========================================================
# GEO INFERENCE
# PRIMARY   : Gemini 2.5 Flash — call 1 (broad country resolution)
# RECOVERY  : Gemini 2.5 Flash — call 2 (explicit recovery for unrecognised locations)
# SECONDARY : Local lookup — fires only when BOTH Gemini calls fail
# =========================================================

# Normalisation map — catches common Gemini response variations
# e.g. Gemini returns "Canberra" instead of "Australia"
# or "The Netherlands" instead of "Netherlands"
COUNTRY_NORMALISE = {
    # Australia & NZ
    "canberra": "Australia", "sydney": "Australia", "melbourne": "Australia",
    "brisbane": "Australia", "perth": "Australia", "adelaide": "Australia",
    "gold coast": "Australia", "newcastle": "Australia", "hobart": "Australia",
    "darwin": "Australia", "queensland": "Australia", "new south wales": "Australia",
    "victoria": "Australia", "western australia": "Australia",
    "south australia": "Australia", "tasmania": "Australia",
    "northern territory": "Australia", "australian capital territory": "Australia",
    "auckland": "New Zealand", "wellington": "New Zealand", "christchurch": "New Zealand",
    # India
    "new delhi": "India", "mumbai": "India", "delhi": "India",
    "bangalore": "India", "bengaluru": "India", "hyderabad": "India",
    "chennai": "India", "kolkata": "India", "pune": "India",
    "ahmedabad": "India", "surat": "India", "jaipur": "India",
    "noida": "India", "gurgaon": "India", "gurugram": "India",
    "lucknow": "India", "indore": "India", "nagpur": "India",
    "maharashtra": "India", "karnataka": "India", "tamil nadu": "India",
    "gujarat": "India", "rajasthan": "India", "uttar pradesh": "India",
    "west bengal": "India", "andhra pradesh": "India", "telangana": "India",
    "kerala": "India", "punjab": "India", "haryana": "India",
    "jalalganj": "India", "patna": "India", "ranchi": "India",
    # UK
    "london": "United Kingdom", "manchester": "United Kingdom",
    "england": "United Kingdom", "scotland": "United Kingdom",
    "wales": "United Kingdom", "northern ireland": "United Kingdom",
    "birmingham": "United Kingdom", "liverpool": "United Kingdom",
    "great britain": "United Kingdom", "britain": "United Kingdom",
    # USA
    "new york": "United States", "los angeles": "United States",
    "chicago": "United States", "houston": "United States",
    "washington dc": "United States", "washington d.c.": "United States",
    "san francisco": "United States", "seattle": "United States",
    "california": "United States", "texas": "United States",
    "oklahoma": "United States", "florida": "United States",
    "new york city": "United States", "nyc": "United States",
    "u.s.a.": "United States", "u.s.": "United States",
    "united states of america": "United States",
    # Canada
    "toronto": "Canada", "vancouver": "Canada", "montreal": "Canada",
    "ontario": "Canada", "british columbia": "Canada", "quebec": "Canada",
    # Germany
    "berlin": "Germany", "munich": "Germany", "hamburg": "Germany",
    "frankfurt": "Germany", "bavaria": "Germany",
    # France
    "paris": "France", "lyon": "France", "marseille": "France",
    # Middle East
    "dubai": "United Arab Emirates", "abu dhabi": "United Arab Emirates",
    "sharjah": "United Arab Emirates", "u.a.e.": "United Arab Emirates",
    "riyadh": "Saudi Arabia", "jeddah": "Saudi Arabia",
    "doha": "Qatar", "muscat": "Oman", "amman": "Jordan",
    "beirut": "Lebanon", "tel aviv": "Israel",
    # Africa
    "cairo": "Egypt", "nairobi": "Kenya", "lagos": "Nigeria",
    "johannesburg": "South Africa", "cape town": "South Africa",
    "casablanca": "Morocco", "accra": "Ghana", "addis ababa": "Ethiopia",
    # Asia
    "tokyo": "Japan", "osaka": "Japan", "kyoto": "Japan",
    "seoul": "South Korea", "busan": "South Korea",
    "taipei": "Taiwan", "hong kong": "Hong Kong",
    "beijing": "China", "shanghai": "China", "shenzhen": "China",
    "singapore": "Singapore", "kuala lumpur": "Malaysia",
    "jakarta": "Indonesia", "bangkok": "Thailand",
    "ho chi minh city": "Vietnam", "ho chi minh": "Vietnam", "hanoi": "Vietnam",
    "manila": "Philippines", "dhaka": "Bangladesh",
    "colombo": "Sri Lanka", "kathmandu": "Nepal",
    "karachi": "Pakistan", "lahore": "Pakistan", "islamabad": "Pakistan",
    # Latin America
    "sao paulo": "Brazil", "são paulo": "Brazil", "rio de janeiro": "Brazil",
    "buenos aires": "Argentina", "bogota": "Colombia",
    "santiago": "Chile", "lima": "Peru",
    # Netherlands variant
    "the netherlands": "Netherlands", "amsterdam": "Netherlands",
    "rotterdam": "Netherlands",
    # Other common variants
    "republic of ireland": "Ireland", "irish republic": "Ireland",
    "south korea": "South Korea", "republic of korea": "South Korea",
    "democratic republic of congo": "Democratic Republic of the Congo",
    "uae": "United Arab Emirates",
    "usa": "United States", "uk": "United Kingdom",
}


def normalise_country(raw: str) -> str:
    """
    Normalises Gemini's raw country response to a standard name.
    Handles cases where Gemini returns a city name instead of country,
    common abbreviations, and alternate country name spellings.
    """
    if not raw:
        return raw
    check = raw.strip().lower()
    if check in COUNTRY_NORMALISE:
        return COUNTRY_NORMALISE[check]
    return raw.strip()


def infer_geo_from_area(area: str) -> dict:
    """
    Step 1 — Gemini PRIMARY call:
        Asks Gemini which country this location belongs to.
        Result is normalised to handle cases where Gemini returns
        a city name, abbreviation, or alternate country spelling.

    Step 2 — Gemini RECOVERY call:
        Only fires when Step 1 returns a value not found in COUNTRY_TO_REGION
        after normalisation. Uses a more explicit focused prompt.

    Step 3 — Local lookup (SECONDARY):
        Fires only when both Gemini calls fail (e.g. org network blocking API).

    Step 4 — Safe absolute fallback:
        Never crashes. Returns area title + Global as last resort.
    """
    area_clean = area.strip()
    key = area_clean.lower()

    # ── STEP 1: Gemini PRIMARY ─────────────────────────────────────────────
    prompt_1 = (
        "You are a geography expert with complete world knowledge.\n"
        "Given any location — city, state, province, country, or region — "
        "identify which COUNTRY it belongs to.\n\n"
        "IMPORTANT: Always return the COUNTRY NAME, never the city or state name.\n\n"
        "Rules:\n"
        "- City or town -> return its COUNTRY  (Canberra -> Australia, Noida -> India, Lyon -> France)\n"
        "- State or province -> return its COUNTRY  (Maharashtra -> India, Bavaria -> Germany, Queensland -> Australia)\n"
        "- Already a country name -> return it exactly in English\n"
        "- Abbreviations -> full country name  (UAE -> United Arab Emirates, UK -> United Kingdom, USA -> United States)\n"
        "- Truly unidentifiable -> return Global\n\n"
        "Location: \"" + area_clean + "\"\n\n"
        "Respond with ONLY this JSON and absolutely nothing else:\n"
        "{\"country\": \"<COUNTRY name in English, NOT city or state>\"}"
    )
    country = None
    try:
        r1 = client.models.generate_content(model="gemini-2.5-flash", contents=prompt_1)
        raw = r1.text.strip()
        raw = re.sub(r"```[a-zA-Z]*", "", raw).replace("```", "").strip()
        match = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
        parsed = json.loads(match.group() if match else raw)
        country_raw = parsed.get("country", "").strip()

        # Normalise — handles Gemini returning city names, abbreviations, variants
        country = normalise_country(country_raw)
        print(f"[GEO] Step 1 Gemini raw: '{country_raw}' → normalised: '{country}'")

        if country and country in COUNTRY_TO_REGION:
            region = COUNTRY_TO_REGION[country]
            print(f"[GEO] Resolved: {country} → {region}")
            return {"Requesting Countries": country, "Requesting Regions": region, "Bill2Country": country}

    except Exception as e:
        print(f"[GEO] Step 1 Gemini failed: {type(e).__name__}: {e}")

    # ── STEP 2: Gemini RECOVERY ────────────────────────────────────────────
    # Fires when Step 1 returned unrecognised value even after normalisation
    if country and country not in COUNTRY_TO_REGION and country != "Global":
        print(f"[GEO] Step 2 Recovery: '{country}' not in region map — asking Gemini explicitly")
        prompt_2 = (
            "Geography question — answer in one word or short phrase only.\n\n"
            "What is the NAME OF THE COUNTRY that contains this location: \"" + area_clean + "\"\n\n"
            "Do NOT return the city name. Do NOT return the state name.\n"
            "Return ONLY the sovereign country name in English.\n\n"
            "For example:\n"
            "- Canberra → Australia\n"
            "- Queensland → Australia\n"
            "- Bavaria → Germany\n"
            "- Jalalganj → India\n\n"
            "Respond with ONLY this JSON:\n"
            "{\"country\": \"<country name>\"}"
        )
        try:
            r2 = client.models.generate_content(model="gemini-2.5-flash", contents=prompt_2)
            raw2 = r2.text.strip()
            raw2 = re.sub(r"```[a-zA-Z]*", "", raw2).replace("```", "").strip()
            match2 = re.search(r'\{[^{}]+\}', raw2, re.DOTALL)
            parsed2 = json.loads(match2.group() if match2 else raw2)
            country2_raw = parsed2.get("country", "").strip()
            country2 = normalise_country(country2_raw)
            print(f"[GEO] Step 2 Recovery raw: '{country2_raw}' → normalised: '{country2}'")

            if country2 and country2 in COUNTRY_TO_REGION:
                region2 = COUNTRY_TO_REGION[country2]
                print(f"[GEO] Recovered: {country2} → {region2}")
                return {"Requesting Countries": country2, "Requesting Regions": region2, "Bill2Country": country2}

            if country2 and country2 != "Global":
                print(f"[GEO] Recovery country '{country2}' not in region map")
                return {"Requesting Countries": country2, "Requesting Regions": "Global", "Bill2Country": country2}

        except Exception as e:
            print(f"[GEO] Step 2 Recovery failed: {type(e).__name__}: {e}")

    # ── STEP 3: Local lookup — fires only when both Gemini calls fail ──────
    if key in LOCAL_GEO:
        c, r = LOCAL_GEO[key]
        print(f"[GEO] Step 3 Local lookup: '{area_clean}' → {c} / {r}")
        return {"Requesting Countries": c, "Requesting Regions": r, "Bill2Country": c}

    # ── STEP 4: Absolute safe fallback ────────────────────────────────────
    print(f"[GEO] Step 4 Safe fallback triggered for '{area_clean}'")
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

    auto = {
        "Requesting Regions":   final.get("Requesting Regions"),
        "Requesting Countries": final.get("Requesting Countries"),
        "Bill2Country":         final.get("Bill2Country")
    }
    reset_session()
    return jsonify({
        "status": "All mandatory fields captured successfully!",
        "auto_populated": auto,
        "demand_id":       final["Demand ID"],
        "demand_timestamp":final["Demand Timestamp"],
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
