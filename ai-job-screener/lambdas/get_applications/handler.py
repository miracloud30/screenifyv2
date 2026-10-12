import json
import os
import logging
from decimal import Decimal
from boto3 import resource
from boto3.dynamodb.conditions import Key, Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)

APPLICATIONS_TABLE = os.environ.get("APPLICATIONS_TABLE", "screenify-applications")
GSI_NAME           = os.environ.get("GSI_NAME", "tenant-applied_at-index")
DYNAMODB_REGION    = os.environ.get("AWS_REGION", "eu-north-1")

dynamodb = resource("dynamodb", region_name=DYNAMODB_REGION)
table    = dynamodb.Table(APPLICATIONS_TABLE)

def decimal_to_python(obj):
    if isinstance(obj, list):
        return [decimal_to_python(i) for i in obj]
    if isinstance(obj, dict):
        return {k: decimal_to_python(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj

def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Authorization,Content-Type",
            "Access-Control-Allow-Methods": "GET,OPTIONS",
        },
        "body": json.dumps(body),
    }

def flatten_item(item):
    return {
        "applicant_id":   item.get("applicant_id", ""),
        "name":           item.get("name", ""),
        "email":          item.get("email", ""),
        "score":          int(item.get("score", 0)),
        "status":         item.get("status", "pending_upload"),
        "summary":        item.get("summary", ""),
        "skills_matched": list(item.get("skills_matched", [])),
        "skills_missing": list(item.get("skills_missing", [])),
        "cv_filename":    item.get("cv_filename", ""),
        "job_id":         item.get("job_id", ""),
        "tenant_id":      item.get("tenant_id", ""),
        "applied_at":     item.get("applied_at", ""),
        "updated_at":     item.get("updated_at", ""),
    }

def handler(event, context):
    if event.get("httpMethod") == "OPTIONS":
        return response(200, {})

    params    = event.get("queryStringParameters") or {}
    tenant_id = params.get("tenant_id", "").strip()
    job_id    = params.get("job_id", "").strip()
    status_f  = params.get("status", "").strip()
    sort_by   = params.get("sort", "score").strip()
    try:
        limit = min(int(params.get("limit", 50)), 200)
    except ValueError:
        limit = 50

    if not tenant_id:
        return response(400, {"error": "tenant_id is required"})

    logger.info("get_applications tenant=%s job=%s status=%s sort=%s limit=%d",
                tenant_id, job_id, status_f, sort_by, limit)

    try:
        kwargs = {
            "IndexName":              GSI_NAME,
            "KeyConditionExpression": Key("tenant_id").eq(tenant_id),
            "ScanIndexForward":       False,
            "Limit":                  min(limit * 4, 200),
        }

        filter_parts = []
        if job_id:
            filter_parts.append(Attr("job_id").eq(job_id))
        if status_f:
            filter_parts.append(Attr("status").eq(status_f))
        if filter_parts:
            combined = filter_parts[0]
            for part in filter_parts[1:]:
                combined = combined & part
            kwargs["FilterExpression"] = combined

        result = table.query(**kwargs)
        items  = result.get("Items", [])
        items  = [decimal_to_python(flatten_item(i)) for i in items]

        if sort_by == "score":
            items.sort(key=lambda x: x["score"], reverse=True)
        elif sort_by == "name":
            items.sort(key=lambda x: x["name"].lower())

        items = items[:limit]
        return response(200, {"items": items, "count": len(items)})

    except Exception as exc:
        logger.exception("get_applications error: %s", exc)
        return response(500, {"error": "Internal server error", "detail": str(exc)})
