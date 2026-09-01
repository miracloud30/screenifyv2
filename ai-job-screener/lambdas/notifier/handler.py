"""
Notifier Lambda for the AI Job Application Screener.

Handles three payload types (dispatched on the "type" field):

  recruiter_alert     - a flagged (low-confidence) application; emails the
                        recruiter only. Never the candidate.

  candidate_decision  - a recruiter clicked Shortlist / Interview / Reject
                        in the dashboard; emails the CANDIDATE with a fixed,
                        polished HR template for that outcome.

SES SANDBOX NOTE: while the account is in the SES sandbox, SES delivers only
to verified addresses. Only RECIPIENT_EMAIL is verified here, so all mail is
redirected to it during testing. Set SANDBOX = False once SES is production.
"""

import json
import os
from datetime import datetime, timezone

import boto3

REGION = os.environ.get("TEXTRACT_REGION", "eu-north-1")
ses_client = boto3.client("ses", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)

SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "aturutommy@gmail.com")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "aturutommy@gmail.com")
COMPANY_NAME = os.environ.get("COMPANY_NAME", "Fernwood Systems")
APPLICATIONS_TABLE = os.environ["APPLICATIONS_TABLE"]

SANDBOX = True


def _now():
    return datetime.now(timezone.utc).isoformat()


def lambda_handler(event, context):
    print(f"Notifier received: {json.dumps(event)}")
    try:
        msg_type = event.get("type", "candidate_decision")
        if msg_type == "recruiter_alert":
            return handle_recruiter_alert(event)
        return handle_candidate_decision(event)
    except Exception as e:
        print(f"Error sending email: {e}")
        return {"statusCode": 200, "body": "Email failed"}


def handle_recruiter_alert(event):
    name = event.get("candidate_name", "Candidate")
    score = event.get("score", 0)
    summary = event.get("summary", "")
    subject = f"[Review needed] {name} flagged by AI screener"
    body = f"""Recruiter alert,

An application needs manual review because the AI screener's confidence was below threshold.

Candidate: {name}
AI score: {score}/100
AI notes: {summary}

Open the dashboard to review and decide.

- {COMPANY_NAME} AI Screener"""
    send_email(RECIPIENT_EMAIL, subject, body)
    return {"statusCode": 200, "body": "recruiter alerted"}


def handle_candidate_decision(event):
    decision = event.get("decision", "")
    name = event.get("candidate_name", "Candidate")
    role = event.get("role", "the role")
    candidate_email = event.get("candidate_email", "")
    applicant_id = event.get("applicant_id", "")

    builders = {
        "shortlist": (
            f"You've been shortlisted for {role} at {COMPANY_NAME}",
            build_shortlist(name, role),
        ),
        "interview": (
            f"Interview invitation: {role} at {COMPANY_NAME}",
            build_interview(name, role),
        ),
        "reject": (
            f"Update on your application for {role} at {COMPANY_NAME}",
            build_rejection(name, role),
        ),
    }
    if decision not in builders:
        return {"statusCode": 200, "body": f"no email for decision {decision}"}

    subject, body = builders[decision]
    to_address = RECIPIENT_EMAIL if SANDBOX else (candidate_email or RECIPIENT_EMAIL)
    send_email(to_address, subject, body)
    if applicant_id:
        mark_notified(applicant_id, decision)
    print(f"Candidate email sent (decision={decision}, to={to_address})")
    return {"statusCode": 200, "body": json.dumps({"status": "sent", "decision": decision})}


def build_shortlist(name, role):
    return f"""Dear {name},

Thank you for applying for the {role} position at {COMPANY_NAME}.

We're pleased to let you know that you've been shortlisted. Your background stood out to our team, and we'd like to move your application forward.

We'll be in touch shortly with the next steps. In the meantime, feel free to reply to this email if you have any questions.

Warm regards,
The {COMPANY_NAME} Talent Team"""


def build_interview(name, role):
    return f"""Dear {name},

Congratulations! Following a review of your application for the {role} position at {COMPANY_NAME}, we'd like to invite you to interview.

A member of our team will reach out within the next two business days to arrange a time that works for you. Please keep an eye on your inbox.

We're looking forward to speaking with you.

Warm regards,
The {COMPANY_NAME} Talent Team"""


def build_rejection(name, role):
    return f"""Dear {name},

Thank you for taking the time to apply for the {role} position at {COMPANY_NAME}, and for your interest in joining our team.

After careful consideration, we've decided not to move forward with your application on this occasion. This was a difficult decision - we received a large number of strong applications, and this outcome is not a reflection of your ability or potential.

We genuinely encourage you to apply for future roles that match your experience, and we wish you every success in your search.

Warm regards,
The {COMPANY_NAME} Talent Team"""


def send_email(to_email, subject, body):
    ses_client.send_email(
        Source=SENDER_EMAIL,
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
        },
    )


def mark_notified(applicant_id, decision):
    dynamodb.Table(APPLICATIONS_TABLE).update_item(
        Key={"applicant_id": applicant_id},
        UpdateExpression="SET notification_sent = :sent, notification_decision = :d, notification_time = :t",
        ExpressionAttributeValues={":sent": True, ":d": decision, ":t": _now()},
    )
