README DOCUMENT 


Demand Creation Chatbot
Intelligent BRD automation for FMCG demand management — powered by Gemini 2.5 Flash

Background & Problem Statement
In a large-scale FMCG (Fast-Moving Consumer Goods) organisation, new IT or business demands are formally captured through a Business Requirement Document (BRD). This document records requirements raised by internal stakeholders or business users, and once completed it marks the official creation of a demand in the system.
Previously, a preliminary chatbot was used to collect responses from users and populate the BRD fields. However, two recurring pain points were identified:
#
Issue
Impact
1
Conversation recall — users had to re-enter information if a session was interrupted or reset
Time loss, user frustration
2
Manual field entry — geographic fields such as Requesting Country, Requesting Region, and Billing Country had to be filled in manually by the user
Risk of errors, inconsistent data

This chatbot directly addresses both issues.

What This Chatbot Does
Guides users through 19 mandatory BRD fields in a structured, step-by-step conversational flow
Auto-populates 3 geographic fields using AI — the user only types an area name (city, state, or country) and the rest is filled automatically
Generates a professionally formatted PDF of the complete demand payload, ready to share with the team
Runs as a web application compatible with SharePoint and Google Sites embedding

Auto-Population Logic
This is the core feature that addresses the manual effort problem.
How It Works
When a user enters the Area field (e.g. Maharashtra, Cairo, Oklahoma), the chatbot automatically resolves and fills three fields without asking the user anything further:
Field
Example Output
Requesting Countries
India
Requesting Regions
Asia-South Pacific
Bill2Country
India

The Two-Step Process
User types: "Maharashtra"
        │
        ▼
Step 1 — Gemini 2.5 Flash (1 API call)
        "What country does Maharashtra belong to?"
        → { "country": "India" }
        │
        ▼
Step 2 — Python dictionary lookup (zero API calls)
        "India" → "Asia-South Pacific"
        │
        ▼
Output: Requesting Countries = India
        Requesting Regions   = Asia-South Pacific
        Bill2Country         = India

Why this design?
Gemini is only asked one focused question — the country name. This keeps the prompt small and the response reliable.
The region mapping is handled entirely in Python using a deterministic dictionary of 130+ countries. This means region assignment is always consistent, costs zero tokens, and cannot hallucinate.
The approach works for any city, state, province, or country worldwide — not just a predefined list.
Enterprise Region Mapping
Region Label
Countries Covered
Asia-South Pacific
India, Australia, Singapore, Malaysia, Pakistan, Vietnam, Philippines, and more
Europe
UK, Germany, France, Italy, Spain, Netherlands, and more
North America
United States, Canada, Mexico
Latin America
Brazil, Argentina, Colombia, Chile, and more
Middle East & Africa
Egypt, UAE, Saudi Arabia, Nigeria, Kenya, South Africa, and more
China
China, Hong Kong, Macau
North Asia
Japan, South Korea, Taiwan
Global
Unidentifiable or explicitly global inputs


Tech Stack
Layer
Technology
Language
Python 3.x
Web Framework
Flask
AI Model
Google Gemini 2.5 Flash (gemini-2.5-flash) via google-genai SDK
PDF Generation
ReportLab
Environment Management
python-dotenv
Frontend
Vanilla HTML, CSS, JavaScript (no frameworks — SharePoint compatible)
Fonts
DM Sans, DM Mono via Google Fonts CDN


Project Structure
Final/
├── App.py          # Main application — Flask server, chatbot logic, geo inference, PDF generation
├── .env            # Environment variables (not committed to Git)
├── .gitignore      # Excludes .env and other sensitive files
└── README.md       # This file


Prerequisites
Python 3.9 or higher
A valid Google Gemini API key (obtain from Google AI Studio)

Setup & Installation
1. Clone the repository
git clone https://github.com/your-username/your-repo-name.git
cd your-repo-name/Final

2. Install dependencies
pip install flask google-genai python-dotenv reportlab

3. Configure your API key
Create a .env file in the Final folder:
GEMINI_API_KEY=your_actual_api_key_here

4. Run the application
python App.py

5. Open the chatbot UI
Navigate to:
http://127.0.0.1:5050/ui


API Endpoints
Method
Endpoint
Description
GET
/start
Initialises a new session and returns the first question
POST
/chat
Accepts user message, returns next question or final payload
POST
/download-pdf
Accepts the final payload JSON and returns a formatted PDF
POST
/reset
Resets the current session
GET
/ui
Renders the chatbot web interface


BRD Fields Captured
The chatbot collects the following 19 mandatory fields through conversation:
#
Field
Captured By
1
Demand Title
User
2
Requirement Summary
User
3
Rationale and Purpose
User
4
Acceptance Criteria
User
5
Business Benefits
User
6
Business Process
User
7
E2E Process in GPM
User
8
Area
User
9
Requesting Countries
✨ Auto-populated
10
Requesting Regions
✨ Auto-populated
11
Bill2Country
✨ Auto-populated
12
Application(s) Impacted
User
13
Target Go Live Date
User
14
Risks if Not Implemented
User
15
Month-end / Year-end Dependency
User
16
Legal / Fiscal Change
User
17
Audit Requirement
User
18
GTS Impact
User
19
Impacted Business Groups
User
20
Landscape Impacted
User
21
Requesting BU
User
22
Potential Savings
User

Fields 9, 10, and 11 are auto-populated from the user's Area input — the user never has to enter these manually.

PDF Output
Once all fields are captured, a Download as PDF button appears in the chatbot UI. The generated PDF includes:
A branded header with demand title, generation timestamp, and status
Six structured sections: Demand Overview, Process & Technical, Geographic Information, Timeline & Risk, Compliance & Impact, and Business Information
Auto-populated fields clearly highlighted in purple with an [Auto] badge
A professional footer citing the generation timestamp

Sharing Publicly (for Testing)
To share the chatbot with teammates without deploying to a server, use ngrok:
# Install ngrok
winget install ngrok.ngrok

# Add your auth token (from ngrok.com)
ngrok config add-authtoken YOUR_TOKEN

# Run App.py first, then in a new terminal:
ngrok http 5050

This generates a public URL like https://abc123.ngrok-free.app/ui that anyone can access while your machine is running.

Security Notes
Never commit your .env file to Git
Add .env to .gitignore before pushing
The Gemini API key should be treated as a secret credential
# .gitignore
.env
__pycache__/
*.pyc


Limitations & Future Scope
Session state is currently stored in-memory — restarting the server clears all active sessions
For production deployment, session state should be persisted using a database (e.g. Redis or PostgreSQL)
The chatbot currently supports a single concurrent session — multi-user support would require session isolation per user
PDF branding (logo, colours) can be customised in the generate_demand_pdf function in App.py

Author
Developed as part of an internal FMCG demand management automation initiative.

