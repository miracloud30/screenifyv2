# AI Job Application Screener — Fernwood Systems

Serverless CV screening on AWS. A candidate uploads a CV, it is stored in S3,
an S3 event triggers extraction (PyPDF2 / python-docx / async Textract), the
text is scored by Amazon Bedrock (Claude Haiku 4.5), the application is routed
(shortlist / review / reject / flag), results land in DynamoDB, SES emails the
outcome, and a dashboard shows everything.

## Architecture

```
Browser (S3 static site)
  -> API Gateway (POST /upload, GET /applications)
    -> API Handler Lambda  (presigned URL + DynamoDB record)
Browser --PUT--> S3 uploads bucket
  -> S3 event -> CV Processor Lambda
       extract text -> Comprehend enrich -> Bedrock score -> route
       -> DynamoDB, move file to processed bucket, invoke Notifier
         -> Notifier Lambda -> SES email
```

## What this build fixes versus the first attempt

- **Bedrock model**: uses the current `us.anthropic.claude-haiku-4-5-20251001-v1:0`
  inference profile (the old `claude-3-haiku` was retired and returned
  ResourceNotFoundException).
- **DynamoDB reserved words**: every write aliases attribute names via
  ExpressionAttributeNames, so `score`, `status`, `summary`, `route`,
  `confidence` never break an UpdateExpression.
- **Textract region**: the Textract client runs in the bucket's region
  (eu-north-1); the cross-region call that caused InvalidS3ObjectException is gone.
- **Textract API shape**: multi-page/scanned PDFs use *async*
  StartDocumentTextDetection with polling + pagination, not the sync-bytes call
  that only handles single-page docs.
- **Extraction order**: PyPDF2 (free, instant on text PDFs) -> python-docx ->
  async Textract fallback. The dead utf-8-decode path was removed.
- **Presigned content-type**: the URL is signed with the type matching the file
  extension, and the frontend PUTs that exact type, so DOCX no longer 403s.
- **Dashboard stats**: totals come from server-side COUNT queries across the
  whole table, not just the current 20-row page.
- **S3 trigger in-template**: bucket names are plain strings (never `!Ref`) in
  the CV Processor's env and policies, breaking the classic SAM circular
  dependency so the S3 event source deploys cleanly with no manual step.

## Deploy

Prerequisites: AWS CLI (configured), AWS SAM CLI, Docker running.

```powershell
# from the project root
.\scripts\deploy.ps1
```

Then, one-time manual steps the script prints:
1. Enable Claude Haiku 4.5 in the Bedrock console (us-east-1 -> Model access).
2. Verify the SES sender/recipient address (sandbox delivers only to verified).

## Project layout

```
template.yaml                  SAM template (all resources, APN-tagged)
layer/requirements.txt         PyPDF2 + python-docx, built by `sam build`
lambdas/api_handler/           presigned URLs, dashboard API
lambdas/cv_processor/          extraction + scoring + routing
lambdas/notifier/              SES emails per route
frontend/upload.html           candidate form
frontend/dashboard.html        recruiter dashboard
seed/seed-requirements.json    DevOps role rubric (attribute-value JSON)
scripts/deploy.ps1             build, deploy, seed, publish frontend
```

## Notes

- SES is in sandbox mode; the Notifier redirects candidate mail to the verified
  address until you leave sandbox (flip `SANDBOX = False` in the notifier).
- Processing is under ~5s for text PDFs (PyPDF2). Scanned PDFs go through async
  Textract and take longer — that path favours correctness over the 5s target.
- All resources carry the tag `aws-apn-id: pc:biytoe4tqjehdsa25lc534ba2`.
```
