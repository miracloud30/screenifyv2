<#
=====================================================================
 deploy-nosam.ps1  -  Screenify deploy WITHOUT the SAM CLI.

 For machines where sam.exe is blocked by an application-control policy.
 Uses only the AWS CLI (aws.exe) + CloudFormation. The SAM template is
 still valid CloudFormation once transformed server-side, which
 `aws cloudformation deploy` does for us.

 What this does that `sam build` used to:
   - builds the PyPDF2/python-docx layer with `pip --target` (pure-python,
     no Docker) into layer/python/
   - `aws cloudformation package` zips each CodeUri/ContentUri, uploads to
     S3, and rewrites the template with the S3 URIs
   - `aws cloudformation deploy` transforms the SAM macro and deploys

 Run from the PROJECT ROOT:
   powershell -ExecutionPolicy Bypass -File .\scripts\deploy-nosam.ps1

 Prereqs: aws CLI (works), python + pip (works), NO sam, NO Docker.
=====================================================================
#>

$ErrorActionPreference = "Continue"
function Assert-LastExit($what) {
    if ($LASTEXITCODE -ne 0) { throw "FAILED: $what (exit $LASTEXITCODE). See output above." }
}

# ---------- config ----------
$Region       = "eu-north-1"
$StackName    = "ai-screener"
$AccountId    = "834424012688"
$ArtifactBkt  = "ai-screener-sam-artifacts-$AccountId"
$Model        = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
# ----------------------------

Write-Host "`n=== Screenify deploy (SAM-free) ===`n" -ForegroundColor Cyan
if (-not (Test-Path "template.yaml")) { throw "Run from the project root (template.yaml not found)." }

# 1. Artifact bucket (checked by exit code, not stderr).
Write-Host "[1/7] Ensuring artifact bucket..." -ForegroundColor Yellow
aws s3api head-bucket --bucket $ArtifactBkt --region $Region 2>$null
if ($LASTEXITCODE -ne 0) {
    aws s3 mb "s3://$ArtifactBkt" --region $Region | Out-Null
    Write-Host "  created $ArtifactBkt" -ForegroundColor Green
} else { Write-Host "  exists" -ForegroundColor DarkGray }

# 2. Build the layer with pip --target (no Docker; pure-python wheels).
Write-Host "[2/7] Building layer (pip --target)..." -ForegroundColor Yellow
$sitePkgs = "layer/python/lib/python3.12/site-packages"
if (Test-Path "layer/python") { Remove-Item "layer/python" -Recurse -Force }
New-Item -ItemType Directory -Path $sitePkgs -Force | Out-Null
pip install -r layer/requirements.txt --target $sitePkgs --quiet
Assert-LastExit "pip install layer deps"
Write-Host "  layer built" -ForegroundColor Green

# 3. Package: zip CodeUri/ContentUri paths, upload to S3, rewrite template.
Write-Host "[3/7] Packaging (aws cloudformation package)..." -ForegroundColor Yellow
aws cloudformation package `
    --template-file template.yaml `
    --s3-bucket $ArtifactBkt `
    --output-template-file packaged.yaml `
    --region $Region
Assert-LastExit "cloudformation package"
Write-Host "  packaged -> packaged.yaml" -ForegroundColor Green

# 4. Deploy: transforms the SAM macro server-side and applies the stack.
Write-Host "[4/7] Deploying (aws cloudformation deploy)..." -ForegroundColor Yellow
aws cloudformation deploy `
    --template-file packaged.yaml `
    --stack-name $StackName `
    --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND `
    --parameter-overrides "BedrockModel=$Model" `
    --region $Region
Assert-LastExit "cloudformation deploy"
Write-Host "  stack deployed" -ForegroundColor Green

# 5. Read outputs.
Write-Host "[5/7] Reading stack outputs..." -ForegroundColor Yellow
function Get-Output($key) {
    aws cloudformation describe-stacks --stack-name $StackName --region $Region `
        --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue" --output text
}
$apiUrl     = Get-Output "ApiEndpoint"
$websiteUrl = Get-Output "WebsiteURL"
$websiteBkt = Get-Output "WebsiteBucket"
Write-Host "  API:     $apiUrl" -ForegroundColor Green
Write-Host "  Website: $websiteUrl" -ForegroundColor Green

# 6. Seed tenants + jobs.
Write-Host "[6/7] Seeding tenants and jobs..." -ForegroundColor Yellow
python scripts/seed.py $Region
Assert-LastExit "seed"

# 7. Inject API URL into frontend and publish.
Write-Host "[7/7] Publishing frontend..." -ForegroundColor Yellow
New-Item -ItemType Directory -Path "frontend/dist" -Force | Out-Null
(Get-Content frontend/upload.html -Raw)    -replace "REPLACE_WITH_API_URL", $apiUrl | Set-Content frontend/dist/upload.html -NoNewline
(Get-Content frontend/dashboard.html -Raw) -replace "REPLACE_WITH_API_URL", $apiUrl | Set-Content frontend/dist/dashboard.html -NoNewline
"<!DOCTYPE html><html><body><h1>Not found</h1><p><a href='upload.html'>Application form</a></p></body></html>" | Set-Content frontend/dist/error.html -NoNewline
aws s3 cp frontend/dist/upload.html    "s3://$websiteBkt/upload.html"    --content-type "text/html" --region $Region | Out-Null
aws s3 cp frontend/dist/dashboard.html "s3://$websiteBkt/dashboard.html" --content-type "text/html" --region $Region | Out-Null
aws s3 cp frontend/dist/error.html     "s3://$websiteBkt/error.html"     --content-type "text/html" --region $Region | Out-Null
Write-Host "  published" -ForegroundColor Green

Write-Host "`n=== DEPLOY COMPLETE (no SAM) ===" -ForegroundColor Cyan
Write-Host @"

Apply (Acme DevOps): $websiteUrl/upload.html?tenant=acme&job=devops-engineer
Apply (Beta Mktg)  : $websiteUrl/upload.html?tenant=beta&job=marketing-manager
Dashboard (Acme)   : $websiteUrl/dashboard.html?tenant=acme
Dashboard (Beta)   : $websiteUrl/dashboard.html?tenant=beta
API                : $apiUrl

Isolation test: apply to both, open both dashboards, confirm neither shows
the other's candidates.
"@ -ForegroundColor Gray