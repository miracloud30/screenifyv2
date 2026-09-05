"""
API Handler Lambda for Screenify (multi-tenant).

Routes:
  POST /upload                          -> create application (tenant+job scoped), presigned PUT URL
  GET  /applications?tenant_id=&status= -> paginated list + counts, SCOPED TO ONE TENANT
  POST /applications/{id}/decision      -> recruiter decision, emails candidate
  GET  /tenants/{tid}                   -> tenant branding (name, logo, color)
  GET  /tenants/{tid}/jobs              -> list a tenant's jobs
  GET  /tenants/{tid}/jobs/{jid}        -> one job (title + public info) for the apply page

ISOLATION MODEL (steps 1-2, pre-auth):
  Every application read is a Query on the tenant-partitioned GSI, so a
  request for tenant A can only ever return tenant A's rows. tenant_id is
  currently supplied as a request parameter; step 4 (Cognito) will replace
  that with a verified claim from the recruiter's token so it can't be spoofed.
"""

import json
import os
import re
import uuid
import base64
from datetime import datetime, timezone
from decimal import Decimal

import boto3

s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
_lambda = boto3.client("lambda")
bedrock_client = boto3.client(
    "bedrock-runtime", region_name=os.environ.get("BEDROCK_REGION", "us-east-1")
)

APPLICATIONS_TABLE = os.environ["APPLICATIONS_TABLE"]
TENANTS_TABLE = os.environ["TENANTS_TABLE"]
JOBS_TABLE = os.environ["JOBS_TABLE"]
UPLOAD_BUCKET = os.environ.get("UPLOAD_BUCKET", "")
BEDROCK_MODEL = os.environ.get(
    "BEDROCK_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)
VALID_EXTENSIONS = {".pdf", ".docx"}

# Fixed thresholds: an 80 means the same thing across every job/tenant.
SHORTLIST_THRESHOLD = 80
HUMAN_REVIEW_THRESHOLD = 50
EXPERIENCE_POINTS = 20
EDUCATION_POINTS = 10

CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, x-api-key, x-tenant-key",
    "Content-Type": "application/json",
}


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _tenant_key(event):
    """Pull the tenant access key from headers (case-insensitive)."""
    headers = event.get("headers") or {}
    for k, v in headers.items():
        if k.lower() == "x-tenant-key":
            return v
    return None


def verify_tenant_access(event, tenant_id):
    """
    Demo-grade gate: the request must carry the tenant's shared access key in
    the x-tenant-key header. Enforced at the API so hitting the endpoint
    directly (not just the dashboard UI) still requires the key.

    NOTE: this is a lightweight per-tenant secret for demos, NOT production
    auth. Step 4 replaces this with a verified Cognito token claim -- the
    call site stays the same, only this function's body changes.
    Returns None if OK, or an error response dict if denied.
    """
    provided = _tenant_key(event)
    if not provided:
        return _resp(401, {"error": "Missing tenant access key"})
    item = dynamodb.Table(TENANTS_TABLE).get_item(Key={"tenant_id": tenant_id}).get("Item")
    if not item:
        return _resp(404, {"error": "Tenant not found"})
    if provided != item.get("access_key"):
        return _resp(403, {"error": "Invalid tenant access key"})
    return None


def lambda_handler(event, context):
    print(f"API Handler received: {json.dumps(event)}")
    if event.get("httpMethod") == "OPTIONS":
        return _resp(200, "")
    path = event.get("path", "")
    method = event.get("httpMethod", "")
    try:
        if path == "/upload" and method == "POST":
            return handle_upload(event)
        if path == "/applications" and method == "GET":
            return handle_dashboard(event)
        if method == "POST" and path.startswith("/applications/") and path.endswith("/decision"):
            return handle_decision(event)
        # Create a job from a pasted JD (generates rubric under the hood).
        if method == "POST" and re.match(r"^/tenants/[^/]+/jobs$", path):
            return handle_create_job(event, path)
        # tenant/job read routes (used by the branded apply page in step 3)
        if method == "GET" and path.startswith("/tenants/"):
            return handle_tenant_routes(event, path)
        return _resp(404, {"error": f"Not found: {method} {path}"})
    except Exception as e:
        print(f"Error: {e}")
        return _resp(500, {"error": str(e)})


# ------------------------------- upload --------------------------------
def handle_upload(event):
    body = json.loads(event.get("body", "{}"))
    for field in ("name", "email", "tenant_id", "job_id", "filename"):
        if field not in body:
            return _resp(400, {"error": f"Missing required field: {field}"})

    name, email = body["name"].strip(), body["email"].strip()
    tenant_id, job_id = body["tenant_id"], body["job_id"]
    filename = body["filename"]

    if not re.match(r"^[\w.\-+]+@[\w.\-]+\.\w+$", email):
        return _resp(400, {"error": "Invalid email format"})

    # The job must exist under this tenant. This both validates the apply
    # link and gives us the role title to store on the application.
    job = dynamodb.Table(JOBS_TABLE).get_item(
        Key={"tenant_id": tenant_id, "job_id": job_id}
    ).get("Item")
    if not job:
        return _resp(404, {"error": "Unknown job for this company"})

    ext = os.path.splitext(filename)[1].lower()
    if ext not in VALID_EXTENSIONS:
        return _resp(400, {"error": f"Invalid file type. Allowed: {', '.join(VALID_EXTENSIONS)}"})

    timestamp = int(datetime.now(timezone.utc).timestamp())
    applicant_id = f"app_{timestamp}_{uuid.uuid4().hex[:8]}"
    # Key includes tenant/job so the processor can recover scope from the key.
    s3_key = f"uploads/{tenant_id}/{job_id}/{applicant_id}-{filename}"
    content_type = CONTENT_TYPES[ext]

    presigned = s3_client.generate_presigned_url(
        ClientMethod="put_object",
        Params={"Bucket": UPLOAD_BUCKET, "Key": s3_key, "ContentType": content_type},
        ExpiresIn=300,
    )
    create_record(applicant_id, tenant_id, job_id, job.get("title", job_id),
                  name, email, filename, s3_key)
    return _resp(200, {
        "applicant_id": applicant_id,
        "upload_url": presigned,
        "content_type": content_type,
        "s3_key": s3_key,
        "expires_in": 300,
    })


# ----------------------------- dashboard -------------------------------
def handle_dashboard(event):
    qs = event.get("queryStringParameters") or {}
    tenant_id = qs.get("tenant_id")
    if not tenant_id:
        return _resp(400, {"error": "tenant_id is required"})
    denied = verify_tenant_access(event, tenant_id)
    if denied:
        return denied
    status = qs.get("status")
    limit = int(qs.get("limit", 20))
    next_token = qs.get("next_token")
    table = dynamodb.Table(APPLICATIONS_TABLE)

    # Always query the tenant-partitioned GSI: a request can only see one
    # tenant's data. Status (when given) is a non-key FilterExpression on top.
    kwargs = {
        "IndexName": "tenant-applied_at-index",
        "KeyConditionExpression": "tenant_id = :t",
        "ExpressionAttributeValues": {":t": tenant_id},
        "Limit": limit,
        "ScanIndexForward": False,
    }
    if status:
        kwargs["FilterExpression"] = "#s = :s"
        kwargs["ExpressionAttributeNames"] = {"#s": "status"}
        kwargs["ExpressionAttributeValues"][":s"] = status
    if next_token:
        kwargs["ExclusiveStartKey"] = _decode_token(next_token)

    resp = table.query(**kwargs)
    applications = [_shape(i) for i in resp.get("Items", [])]
    result = {
        "applications": applications,
        "count": len(applications),
        "stats": tenant_stats(table, tenant_id),
    }
    if "LastEvaluatedKey" in resp:
        result["next_token"] = _encode_token(resp["LastEvaluatedKey"])
    return _resp(200, result)


def tenant_stats(table, tenant_id):
    """Status counts for ONE tenant. Pages the tenant partition and tallies
    in memory -- correct isolation, and fine at the scale of one company's
    pipeline. (A per-status COUNT GSI is the optimization if volume grows.)"""
    counts = {}
    total = 0
    kwargs = {
        "IndexName": "tenant-applied_at-index",
        "KeyConditionExpression": "tenant_id = :t",
        "ExpressionAttributeValues": {":t": tenant_id},
        "ProjectionExpression": "#s",
        "ExpressionAttributeNames": {"#s": "status"},
    }
    while True:
        r = table.query(**kwargs)
        for item in r.get("Items", []):
            st = item.get("status", "unknown")
            counts[st] = counts.get(st, 0) + 1
            total += 1
        if "LastEvaluatedKey" not in r:
            break
        kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    counts["total"] = total
    return counts


# ------------------------------ decision -------------------------------
def handle_decision(event):
    parts = event.get("path", "").strip("/").split("/")
    if len(parts) < 3:
        return _resp(400, {"error": "Malformed decision path"})
    applicant_id = parts[1]

    body = json.loads(event.get("body", "{}"))
    decision = (body.get("decision") or "").lower()
    valid = {"shortlist", "interview", "reject", "hold"}
    if decision not in valid:
        return _resp(400, {"error": f"Invalid decision. Allowed: {', '.join(sorted(valid))}"})

    status_map = {"shortlist": "shortlisted", "interview": "interview",
                  "reject": "rejected", "hold": "on_hold"}
    new_status = status_map[decision]

    table = dynamodb.Table(APPLICATIONS_TABLE)
    item = table.get_item(Key={"applicant_id": applicant_id}).get("Item")
    if not item:
        return _resp(404, {"error": "Application not found"})

    # Key must match the tenant that owns this record. This both authenticates
    # the recruiter and enforces isolation: you can only act on your own
    # tenant's candidates.
    denied = verify_tenant_access(event, item.get("tenant_id"))
    if denied:
        return denied

    table.update_item(
        Key={"applicant_id": applicant_id},
        UpdateExpression="SET #s = :s, decided_at = :t, decided_by = :by",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": new_status, ":t": _now(),
            ":by": body.get("recruiter", "recruiter"),
        },
    )

    if decision != "hold":
        _lambda.invoke(
            FunctionName="ai-screener-notifier",
            InvocationType="Event",
            Payload=json.dumps({
                "type": "candidate_decision",
                "decision": decision,
                "applicant_id": applicant_id,
                "candidate_name": item.get("name", "Candidate"),
                "candidate_email": item.get("email", ""),
                "role": item.get("role", "the role"),
                "tenant_id": item.get("tenant_id"),
            }),
        )

    return _resp(200, {"applicant_id": applicant_id, "status": new_status,
                       "emailed": decision != "hold"})


# --------------------------- tenant/job reads --------------------------
def handle_tenant_routes(event, path):
    parts = path.strip("/").split("/")  # tenants/{tid}[/jobs[/{jid}]]
    if len(parts) == 2:
        tid = parts[1]
        item = dynamodb.Table(TENANTS_TABLE).get_item(Key={"tenant_id": tid}).get("Item")
        if not item:
            return _resp(404, {"error": "Tenant not found"})
        return _resp(200, {
            "tenant_id": item.get("tenant_id"),
            "name": item.get("name"),
            "logo_url": item.get("logo_url", ""),
            "brand_color": item.get("brand_color", "#3b82f6"),
        })
    if len(parts) == 3 and parts[2] == "jobs":
        tid = parts[1]
        resp = dynamodb.Table(JOBS_TABLE).query(
            KeyConditionExpression="tenant_id = :t",
            ExpressionAttributeValues={":t": tid},
        )
        jobs = [{"job_id": j["job_id"], "title": j.get("title", j["job_id"])}
                for j in resp.get("Items", [])]
        return _resp(200, {"jobs": jobs})
    if len(parts) == 4 and parts[2] == "jobs":
        tid, jid = parts[1], parts[3]
        j = dynamodb.Table(JOBS_TABLE).get_item(
            Key={"tenant_id": tid, "job_id": jid}
        ).get("Item")
        if not j:
            return _resp(404, {"error": "Job not found"})
        return _resp(200, {"job_id": j["job_id"], "title": j.get("title", jid),
                           "job_description": j.get("job_description", ""),
                           "tenant_id": tid})
    return _resp(404, {"error": "Not found"})


# --------------------------- job creation ------------------------------
def handle_create_job(event, path):
    """
    POST /tenants/{tid}/jobs
    Body: {"title": "...", "job_description": "<prose JD>", "job_id": "optional-slug"}

    Generates a structured rubric from the JD via Bedrock, then saves BOTH the
    JD text and the rubric. Scoring later runs against the saved rubric, so a
    job's criteria are frozen at creation -> consistent, explainable scoring.
    """
    tenant_id = path.strip("/").split("/")[1]
    denied = verify_tenant_access(event, tenant_id)
    if denied:
        return denied
    body = json.loads(event.get("body", "{}"))
    title = (body.get("title") or "").strip()
    jd = (body.get("job_description") or "").strip()
    if not title or not jd:
        return _resp(400, {"error": "title and job_description are required"})

    # Confirm the tenant exists (isolation: can't create jobs for a company
    # that isn't yours; step 4 ties this to the auth token).
    if not dynamodb.Table(TENANTS_TABLE).get_item(Key={"tenant_id": tenant_id}).get("Item"):
        return _resp(404, {"error": "Tenant not found"})

    job_id = body.get("job_id") or _slugify(title)

    try:
        rubric = generate_rubric_from_jd(title, jd)
    except Exception as e:
        print(f"Rubric generation failed: {e}")
        return _resp(502, {"error": "Could not generate rubric from the job description"})

    item = {
        "tenant_id": tenant_id,
        "job_id": job_id,
        "title": title,
        "job_description": jd,
        "skills_required": rubric["skills_required"],
        "skills_preferred": rubric["skills_preferred"],
        "experience_years_required": rubric.get("experience_years_required", 2),
        "experience_points": EXPERIENCE_POINTS,
        "education_points": EDUCATION_POINTS,
        "shortlist_threshold": SHORTLIST_THRESHOLD,
        "human_review_threshold": HUMAN_REVIEW_THRESHOLD,
        "created_at": _now(),
    }
    dynamodb.Table(JOBS_TABLE).put_item(Item=_to_dynamo_numbers(item))
    return _resp(200, {"tenant_id": tenant_id, "job_id": job_id, "title": title,
                       "rubric": rubric})


def generate_rubric_from_jd(title, jd):
    """
    One Bedrock call: JD prose -> structured rubric. The model chooses the
    skills and their weights; thresholds are fixed by the platform, not the AI.
    Required skills sum to ~70 pts, preferred to ~20; experience(20)+education(10)
    fill the rest so a well-formed rubric tops out near 100 with the fixed points.
    """
    prompt = f"""You are designing a CV screening rubric for this role.

ROLE TITLE: {title}

JOB DESCRIPTION:
{jd}

Extract the skills that matter and assign point weights. Rules:
- "skills_required": the must-have skills. Their points should sum to about 70.
- "skills_preferred": nice-to-have skills. Their points should sum to about 20.
- Pick 4-8 required and 3-6 preferred skills. Use concise skill names.
- "experience_years_required": integer years of experience the role implies.

Respond with ONLY valid JSON in exactly this shape, no prose:
{{"skills_required": {{"Skill A": 15, "Skill B": 10}}, "skills_preferred": {{"Skill C": 5}}, "experience_years_required": 3}}"""

    resp = bedrock_client.invoke_model(
        modelId=BEDROCK_MODEL,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 700,
            "temperature": 0.2,
            "messages": [{"role": "user", "content": prompt}],
        }),
    )
    payload = json.loads(resp["body"].read().decode("utf-8"))
    text = payload["content"][0]["text"]
    match = re.search(r"\{.*\}", text, re.DOTALL)
    rubric = json.loads(match.group() if match else text)

    # Validate shape; fall back to safe empties rather than saving garbage.
    if not isinstance(rubric.get("skills_required"), dict) or not rubric["skills_required"]:
        raise ValueError("model did not return skills_required")
    rubric.setdefault("skills_preferred", {})
    rubric.setdefault("experience_years_required", 2)
    # Coerce weights to ints.
    rubric["skills_required"] = {k: int(v) for k, v in rubric["skills_required"].items()}
    rubric["skills_preferred"] = {k: int(v) for k, v in rubric["skills_preferred"].items()}
    rubric["experience_years_required"] = int(rubric["experience_years_required"])
    return rubric


def _slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "job"


def _to_dynamo_numbers(item):
    """Recursively convert ints/floats to Decimal for DynamoDB."""
    if isinstance(item, dict):
        return {k: _to_dynamo_numbers(v) for k, v in item.items()}
    if isinstance(item, list):
        return [_to_dynamo_numbers(v) for v in item]
    if isinstance(item, bool):
        return item
    if isinstance(item, (int, float)):
        return Decimal(str(item))
    return item


# ------------------------------- helpers -------------------------------
def _shape(item):
    return {
        "applicant_id": item.get("applicant_id"),
        "tenant_id": item.get("tenant_id"),
        "job_id": item.get("job_id"),
        "name": item.get("name"),
        "email": item.get("email"),
        "role": item.get("role"),
        "status": item.get("status"),
        "ai_suggested_status": item.get("ai_suggested_status"),
        "score": item.get("score", 0),
        "confidence": item.get("confidence", 0),
        "skills_matched": item.get("skills_matched", []),
        "skills_missing": item.get("skills_missing", []),
        "years_experience": item.get("years_experience", 0),
        "summary": item.get("summary", ""),
        "applied_at": item.get("applied_at"),
        "cv_filename": item.get("cv_filename"),
    }


def create_record(applicant_id, tenant_id, job_id, role, name, email, filename, s3_key):
    dynamodb.Table(APPLICATIONS_TABLE).put_item(Item={
        "applicant_id": applicant_id,
        "tenant_id": tenant_id,
        "job_id": job_id,
        "name": name,
        "email": email,
        "role": role,
        "cv_filename": filename,
        "cv_s3_key": s3_key,
        "status": "pending_upload",
        "applied_at": _now(),
        "updated_at": _now(),
        "score": 0,
        "confidence": 0,
        "skills_matched": [],
        "skills_missing": [],
        "years_experience": 0,
        "summary": "",
        "notification_sent": False,
    })


def _encode_token(key):
    return base64.b64encode(json.dumps(key, cls=DecimalEncoder).encode()).decode()


def _decode_token(token):
    return json.loads(base64.b64decode(token).decode())


def _resp(status_code, data):
    return {
        "statusCode": status_code,
        "headers": CORS_HEADERS,
        "body": data if isinstance(data, str) else json.dumps(data, cls=DecimalEncoder),
    }