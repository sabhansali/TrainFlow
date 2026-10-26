
# TrainFlow

## Automated Employee Training Nomination and Approval Workflow System

TrainFlow is a Streamlit-based HR workflow automation application
for managing employee training requests. It validates structured
data, calculates explainable training-suitability scores, routes
requests for review, generates notification drafts, and maintains
an auditable record of human decisions.

## Technology Stack

- Python
- Streamlit
- Pandas
- SQLite
- CSV and JSON
- Pytest

## Features

- CSV/JSON ingestion and validation
- Duplicate detection and invalid-record quarantine
- Configurable weighted scoring
- Explainable score breakdowns
- Workflow routing and human approval
- Notification drafts and simulated delivery
- Role-based access control
- Audit logging and security testing
- Performance metrics and benchmark comparison

## Run Locally

1. Create and activate a Python virtual environment.
2. Install dependencies using `pip install -r requirements.txt`.
3. Run `streamlit run app.py`.
4. Run tests using `python -m pytest -q`.

All employee and training-request records are synthetic.
Notifications are simulated; no real HR system or email service
is connected.
