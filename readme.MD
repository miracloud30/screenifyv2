# Screenify — AI Job Application Screener

Multi-tenant serverless CV screening on AWS. Candidates apply through a
per-company, per-role link; each CV is automatically scored against that
role's rubric by Amazon Bedrock (Claude Haiku 4.5); a recruiter reviews a
ranked, isolated dashboard and makes the decision; the candidate is emailed
on that decision. The AI triages — a human decides.

## How it works

```
Candidate opens a per-job link  (upload.html?tenant=acme&job=devops-engineer)
  -> API Gateway (POST /upload)
    -> API Handler Lambda: validates job, presigned URL, DynamoDB record
Candidate --PUT CV--> S3 uploads bucket  (key: uploads/<tenant>/<job>/<id>-<file>)
  -> S3 event -> CV Processor Lambda
       extract text (PyPDF2 / python-docx / async Textract)
       load the job's saved rubric  ->  Bedrock scores against it
       AI SUGGESTS a routing; status becomes 'needs_review'
       persist results (stamped with tenant_id + job_id)
Recruiter opens dashboard  (dashboard.html?tenant=acme)  [scoped to one company]
  -> clicks Shortlist / Interview / Hold / Reject
    -> API Handler -> Notifier Lambda -> SES emails the candidate
```

## Multi-tenancy

Every company is a `tenant_id`. All four product concerns hang off it:

- **Isolation** — the applications GSI partitions by `tenant_id`, so a
  dashboard query is physically confined to one company. A request for
  tenant A can never return tenant B's candidates (verified with moto).
- **Per-company jobs** — jobs belong to a tenant (`screenify-jobs`, keyed
  by tenant_id + job_id). Each job carries its own scoring rubric.
- **Branded apply links** — each job has its own application link carrying
  `tenant_id` + `job_id`.

> **Auth status:** tenant scope is currently passed as a URL parameter
> (`?tenant=`). This is a **testing bridge only and is not secure** — it
> proves isolation end-to-end but anyone could change the parameter. Real
> authentication (Amazon Cognito, tenant bound to the recruiter's token)
> is the next step; code comments mark exactly where the swap happens.

## Scoring: hybrid JD → rubric

A recruiter creates a job by pasting a plain **job description**. One Bedrock
call turns it into a structured rubric (weighted required/preferred skills),
which is **saved and frozen** on the job. Every candidate for that job is
scored against the same saved rubric — so scoring stays consistent and
explainable, while the recruiter never has to author point weights by hand.

- Skill weights: inferred by the AI from the JD.
- Thresholds: fixed platform-wide (80 shortlist / 50 review) so an 80 means
  the same thing across every job and tenant.
- Both the JD text and the generated rubric are stored (JD for display and
  future rubric regeneration; rubric for scoring).

## Data model

| Table | Key | Holds |
|-------|-----|-------|
| `screenify-tenants` | `tenant_id` | company name, logo, brand colour, plan |
| `screenify-jobs` | `tenant_id` + `job_id` | title, JD text, rubric, thresholds |
| `screenify-applications` | `applicant_id` (GSI: `tenant_id` + `applied_at`) | candidate, score, status, AI suggestion |

## Deploy

Prerequisites: AWS CLI configured, AWS SAM CLI, Docker running, Python with
boto3 installed (used by the seeder).

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\deploy.ps1
```

The script builds (containerised, so no local python3.12 needed), deploys the
stack, seeds tenants + jobs via `scripts/seed.py`, and publishes the frontend
with the live API URL injected.

Manual one-time steps the script prints:
1. Enable Claude Haiku 4.5 in the Bedrock console (us-east-1 → Model access).
2. Verify the SES sender/recipient address in eu-north-1 (sandbox delivers
   only to verified addresses).

> **Table rename note:** this build renames the DynamoDB tables. Deploying
> over an older `ai-screener-*` stack drops the old application data — that
> data is not migrated. Tenants and jobs are reseeded fresh.

## Test the isolation

After deploy, the seeded data gives you two companies:

- Acme — jobs: `devops-engineer`, `backend-engineer`
- Beta — job: `marketing-manager`

```
Apply (Acme):  .../upload.html?tenant=acme&job=devops-engineer
Apply (Beta):  .../upload.html?tenant=beta&job=marketing-manager
Dashboard:     .../dashboard.html?tenant=acme     (only Acme's candidates)
               .../dashboard.html?tenant=beta     (only Beta's candidates)
```

Submitting to each and confirming neither dashboard shows the other's
candidates is the confidentiality wall working.

## Project layout

```
template.yaml              SAM template (3 tables, 3 Lambdas, API, layer; APN-tagged)
layer/requirements.txt     PyPDF2 + python-docx, built by `sam build`
lambdas/api_handler/       upload, dashboard, decision, job creation (rubric gen)
lambdas/cv_processor/      extraction + scoring against saved rubric
lambdas/notifier/          SES emails (recruiter alerts + candidate decisions)
seed/tenants.json          seed companies
seed/jobs.json             seed jobs (with JD text + rubric)
scripts/seed.py            loads seed JSON into DynamoDB
scripts/deploy.ps1         build, deploy, seed, publish frontend
frontend/upload.html       per-job apply page (reads ?tenant=&job=)
frontend/dashboard.html    recruiter decision dashboard (reads ?tenant=)
```

## Notes & known limitations

- **SES sandbox:** candidate emails are redirected to the verified address
  until SES is moved to production. The sender is currently a Gmail address,
  which Gmail's DMARC will often route to Spam — for production, use a sender
  on a domain you control with DKIM/SPF/DMARC configured.
- **Auth:** `?tenant=` URL params are not a security boundary yet (see above).
- **Stats:** per-tenant status counts page the tenant partition and tally in
  memory — correct and fine at one company's volume; a per-status COUNT GSI
  is the optimisation if a tenant's pipeline grows large.
- **Processing time:** text PDFs/DOCX score in a few seconds; scanned PDFs go
  through async Textract OCR and take longer.
- All resources carry the tag `aws-apn-id: pc:biytoe4tqjehdsa25lc534ba2`.

## Roadmap

- [x] Single-tenant pipeline: upload → extract → score → route
- [x] Human-in-the-loop decisions + candidate emails
- [x] Multi-tenant data model + query isolation
- [x] Hybrid JD → saved rubric scoring
- [ ] Branded per-company apply pages (step 3)
- [ ] Recruiter authentication via Cognito (step 4)
- [ ] SES production access + verified sending domain