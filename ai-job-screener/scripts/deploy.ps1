<#
=====================================================================
 deploy.ps1  -  AI Job Application Screener
 Full build + deploy via AWS SAM, then seed data and publish frontend.

 Run from the PROJECT ROOT (not from scripts/):
   .\scripts\deploy.ps1

 Prerequisites:
   - AWS CLI configured (screenify1 credentials, eu-north-1)
   - AWS SAM CLI installed
   - Docker running (SAM builds the PyPDF2/python-docx layer in a
     Lambda-like container so the layer is Linux-correct)

 One-time MANUAL step this script cannot do reliably:
   Enable Claude Haiku 4.5 model access in the Bedrock console,
   region us-east-1  ->  Model access.
=====================================================================
#>

# Continue (not Stop): native CLI tools (aws, sam) write progress and
# harmless notices to stderr, which under Stop would be misread as fatal.
# We check $LASTEXITCODE explicitly after the commands that matter instead.
$ErrorActionPreference = "Continue"

function Assert-LastExit($what) {
    if ($LASTEXITCODE -ne 0) {
        throw "FAILED: $what (exit code $LASTEXITCODE). See output above."
    }
}

# ---------- config ----------
$Region       = "eu-north-1"
$StackName    = "ai-screener"
$AccountId    = "834424012688"
$ArtifactBkt  = "ai-screener-sam-artifacts-$AccountId"
$Model        = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
# ----------------------------

Write-Host "`n=== AI Screener deploy ===`n" -ForegroundColor Cyan

# 0. Sanity: are we at the project root?
if (-not (Test-Path "template.yaml")) {
    throw "Run this from the project root (template.yaml not found here)."
}

# 1. Artifact bucket for SAM (separate from the app's upload bucket).
# Note: check by exit code, not stderr. The AWS CLI writes to stderr on a
# missing bucket, and with ErrorActionPreference=Stop that would otherwise
# be treated as a fatal error even though "not found" is a valid answer.
Write-Host "[1/8] Ensuring SAM artifact bucket..." -ForegroundColor Yellow
aws s3api head-bucket --bucket $ArtifactBkt --region $Region 2>$null
if ($LASTEXITCODE -ne 0) {
    aws s3 mb "s3://$ArtifactBkt" --region $Region | Out-Null
    Write-Host "  created $ArtifactBkt" -ForegroundColor Green
} else {
    Write-Host "  exists" -ForegroundColor DarkGray
}

# 2. Build (compiles the layer from layer/requirements.txt in a container).
Write-Host "[2/8] sam build (containerized)..." -ForegroundColor Yellow
# --use-container builds the layer inside a Lambda-like Docker image, so it
# does NOT need a local python3.12 on PATH (you have 3.14, which SAM rejects
# for a 3.12 layer). Requires Docker running.
sam build --use-container --template-file template.yaml --region $Region
Assert-LastExit "sam build"

# 3. Deploy.
Write-Host "[3/8] sam deploy..." -ForegroundColor Yellow
sam deploy `
    --stack-name $StackName `
    --s3-bucket $ArtifactBkt `
    --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND `
    --region $Region `
    --no-confirm-changeset `
    --no-fail-on-empty-changeset `
    --parameter-overrides "BedrockModel=$Model"
Assert-LastExit "sam deploy"

# 4. Read stack outputs.
Write-Host "[4/8] Reading stack outputs..." -ForegroundColor Yellow
function Get-Output($key) {
    aws cloudformation describe-stacks --stack-name $StackName --region $Region `
        --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue" --output text
}
$apiUrl     = Get-Output "ApiEndpoint"
$websiteUrl = Get-Output "WebsiteURL"
$websiteBkt = Get-Output "WebsiteBucket"
Write-Host "  API:     $apiUrl" -ForegroundColor Green
Write-Host "  Website: $websiteUrl" -ForegroundColor Green

# 5. Seed tenants and jobs (plain JSON -> DynamoDB via python seeder).
Write-Host "[5/8] Seeding tenants and jobs..." -ForegroundColor Yellow
python scripts/seed.py $Region
Assert-LastExit "seed"

# 6. Inject the real API URL into the frontend (copies first, never edits source in place).
Write-Host "[6/8] Injecting API URL into frontend..." -ForegroundColor Yellow
New-Item -ItemType Directory -Path "frontend/dist" -Force | Out-Null
(Get-Content frontend/upload.html -Raw)    -replace "REPLACE_WITH_API_URL", $apiUrl | Set-Content frontend/dist/upload.html -NoNewline
(Get-Content frontend/dashboard.html -Raw) -replace "REPLACE_WITH_API_URL", $apiUrl | Set-Content frontend/dist/dashboard.html -NoNewline
"<!DOCTYPE html><html><body><h1>Not found</h1><p><a href='upload.html'>Go to application form</a></p></body></html>" | Set-Content frontend/dist/error.html -NoNewline
Write-Host "  built frontend/dist" -ForegroundColor Green

# 7. Publish frontend to the website bucket.
Write-Host "[7/8] Uploading frontend..." -ForegroundColor Yellow
aws s3 cp frontend/dist/upload.html    "s3://$websiteBkt/upload.html"    --content-type "text/html" --region $Region | Out-Null
aws s3 cp frontend/dist/dashboard.html "s3://$websiteBkt/dashboard.html" --content-type "text/html" --region $Region | Out-Null
aws s3 cp frontend/dist/error.html     "s3://$websiteBkt/error.html"     --content-type "text/html" --region $Region | Out-Null
Write-Host "  uploaded" -ForegroundColor Green

# 8. Done.
Write-Host "`n=== DEPLOY COMPLETE ===" -ForegroundColor Cyan
Write-Host @"

Upload form : $websiteUrl/upload.html
Dashboard   : $websiteUrl/dashboard.html
API         : $apiUrl

REMAINING MANUAL STEPS:
  1. Bedrock console (us-east-1) -> Model access -> enable Claude Haiku 4.5.
     Verify: aws bedrock list-foundation-models --region us-east-1 ``
        --query "modelSummaries[?contains(modelId,'haiku-4-5')].modelId" --output table
  2. SES: verify the sender/recipient address (aturutommy@gmail.com) in the
     SES console (eu-north-1) if not already verified. Sandbox only delivers
     to verified addresses.

Test end-to-end: open the upload form, submit a PDF, then watch the dashboard.
Tail the processor logs:
  `$cvStream = aws logs describe-log-streams --log-group-name /aws/lambda/ai-screener-cv-processor ``
     --order-by LastEventTime --descending --limit 1 --region $Region ``
     --query "logStreams[0].logStreamName" --output text
  aws logs get-log-events --log-group-name /aws/lambda/ai-screener-cv-processor ``
     --log-stream-name `$cvStream --limit 40 --region $Region --query "events[].message" --output text
"@ -ForegroundColor Gray
