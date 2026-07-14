# 변경사항을 커밋하고 GitHub에 올린다.
# 사용:  .\scripts\push.ps1 "커밋 메시지"
param([string]$Message = "update")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

git add -A
git commit -m $Message
git push -u origin main

Write-Host "`n푸시 완료: https://github.com/Zeereoreo/sns-agent" -ForegroundColor Green
