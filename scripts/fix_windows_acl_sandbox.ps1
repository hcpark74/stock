<#
.SYNOPSIS
  Repairs common Windows ACL issues that can prevent Codex sandboxing from
  applying temporary deny-read ACLs in this repository.

.DESCRIPTION
  Run this from an elevated PowerShell session when Codex reports an error like:
  "windows sandbox: helper_unknown_error: apply deny-read ACLs"

  The script:
  - verifies that it is running as Administrator
  - grants the current user FullControl on the repository tree
  - enables ACL inheritance on the repository root
  - creates and removes a small write-test file
  - prints a short verification checklist

  It only changes ACLs under the target repository path. -WhatIf is supported
  for the ACL changes and write-test.

.PARAMETER RepoPath
  Repository path to repair. Defaults to the parent directory of this script.

.PARAMETER DisableInheritance
  Optional. Disables inheritance on the repository root while preserving current
  inherited rules as explicit rules. This is normally not needed.
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$RepoPath = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [switch]$DisableInheritance
)

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

if (-not (Test-IsAdministrator)) {
    Write-Error "관리자 권한 PowerShell에서 실행해야 합니다. VSCode도 관리자 권한으로 다시 열어 주세요."
}

$repo = (Resolve-Path -LiteralPath $RepoPath).Path
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name

Write-Step "대상 경로 확인"
Write-Host "RepoPath: $repo"
Write-Host "User    : $currentUser"

Write-Step "현재 사용자에게 저장소 전체 FullControl 부여"
$grant = "${currentUser}:(OI)(CI)F"
if ($PSCmdlet.ShouldProcess($repo, "Grant FullControl to $currentUser recursively")) {
    icacls $repo /grant $grant /T /C | Out-Host
}
else {
    Write-Host "WhatIf: icacls $repo /grant $grant /T /C"
}

if ($DisableInheritance) {
    Write-Step "상속 비활성화, 기존 상속 규칙은 명시 규칙으로 보존"
    if ($PSCmdlet.ShouldProcess($repo, "Disable inheritance and preserve inherited rules")) {
        icacls $repo /inheritance:d | Out-Host
    }
    else {
        Write-Host "WhatIf: icacls $repo /inheritance:d"
    }
}
else {
    Write-Step "상속 활성화"
    if ($PSCmdlet.ShouldProcess($repo, "Enable inheritance")) {
        icacls $repo /inheritance:e | Out-Host
    }
    else {
        Write-Host "WhatIf: icacls $repo /inheritance:e"
    }
}

Write-Step "쓰기 테스트"
$testFile = Join-Path $repo ".codex_acl_write_test"
if ($PSCmdlet.ShouldProcess($testFile, "Create and remove write-test file")) {
    Set-Content -LiteralPath $testFile -Value "ok" -Encoding ASCII
    Remove-Item -LiteralPath $testFile -Force
    Write-Host "쓰기 테스트 성공"
}
else {
    Write-Host "WhatIf: create and remove $testFile"
}

Write-Step "최종 ACL 요약"
icacls $repo | Out-Host

Write-Host ""
Write-Host "완료되었습니다." -ForegroundColor Green
Write-Host "다음 순서로 확인하세요:"
Write-Host "1. VSCode를 완전히 종료"
Write-Host "2. VSCode를 관리자 권한으로 실행"
Write-Host "3. 이 저장소를 다시 열기"
Write-Host "4. Codex에서 'rg --files' 같은 읽기 명령 재시도"
