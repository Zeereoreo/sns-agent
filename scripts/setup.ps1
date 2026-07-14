# SNS Agent 개발환경 자동 셋업 (Windows PowerShell)
# 사용:  .\scripts\setup.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Write-Host "==> 1) 가상환경(.venv) 생성" -ForegroundColor Cyan
if (-not (Test-Path "$root\.venv")) {
    py -3 -m venv "$root\.venv"
} else {
    Write-Host "   이미 존재 - 건너뜀"
}
$py = "$root\.venv\Scripts\python.exe"

Write-Host "==> 2) pip 업그레이드 & 의존성 설치" -ForegroundColor Cyan
& $py -m pip install --upgrade pip
& $py -m pip install -r "$root\requirements.txt"

Write-Host "==> 3) Playwright 브라우저(Chromium) 설치" -ForegroundColor Cyan
& $py -m playwright install chromium

Write-Host "==> 4) .env 준비" -ForegroundColor Cyan
if (-not (Test-Path "$root\.env")) {
    Copy-Item "$root\.env.example" "$root\.env"
    Write-Host "   .env 생성됨 - ANTHROPIC_API_KEY를 채워주세요." -ForegroundColor Yellow
} else {
    Write-Host "   .env 이미 존재 - 건너뜀"
}

Write-Host "==> 5) 설정 검증" -ForegroundColor Cyan
& $py "$root\verify_setup.py"

Write-Host "`n완료! 다음 단계: .env 에 API 키 입력" -ForegroundColor Green
