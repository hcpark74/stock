<#
.SYNOPSIS
    stock.bat이 호출하는 수동 실행 런처.
.DESCRIPTION
    사전 점검 7단계를 수행한 뒤 main.py를 포그라운드로 실행하고,
    화면과 data\logs\launcher_*.log에 동시에 출력한다.

    이 스크립트는 실행 중인 프로세스를 종료하지 않는다. 재시작은
    안전점검(restart_guard.py)을 거치는 scripts\restart_main.ps1이 담당한다.

    설계: docs/superpowers/plans/2026-08-14-stock-bat-launcher.md
#>
[CmdletBinding()]
param(
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$mainScript = Join-Path $repoRoot "main.py"
$envPath = Join-Path $repoRoot ".env"
$pidPath = Join-Path $repoRoot "main.pid"
$logDir = Join-Path $repoRoot "data\logs"

# 장 시간 안내용 상수. src/schedule_times.py의 값을 옮겨 적었다.
# 런처가 전략 모듈을 import하면 기동 경로가 무거워지므로 상수로 둔다.
# 변경 시 src/schedule_times.py와 함께 맞춘다.
$FIRST_JOB = [TimeSpan]::new(8, 59, 45)      # PAPER_FAST_PROBE
$ENTRY_DEADLINE = [TimeSpan]::new(9, 11, 0)  # F3_FILL_DEADLINE
$EXIT_TIME = [TimeSpan]::new(15, 15, 0)      # F5_EXEC

function Write-Section([string]$Text) {
    Write-Host ""
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) {
    Write-Host "  [OK] $Text" -ForegroundColor Green
}

function Write-Info([string]$Text) {
    Write-Host "  $Text"
}

function Fail([string]$Reason, [string[]]$HowToFix) {
    Write-Host ""
    Write-Host "  [실패] $Reason" -ForegroundColor Red
    if ($HowToFix) {
        Write-Host ""
        Write-Host "  해결 방법:" -ForegroundColor Yellow
        foreach ($line in $HowToFix) {
            Write-Host "    $line" -ForegroundColor Yellow
        }
    }
    Write-Host ""
    exit 1
}

function Read-DotEnv([string]$Path) {
    $map = @{}
    foreach ($line in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $idx = $trimmed.IndexOf("=")
        if ($idx -lt 1) { continue }
        $key = $trimmed.Substring(0, $idx).Trim()
        $value = $trimmed.Substring($idx + 1).Trim()
        if ($value.Length -ge 2) {
            $quoted = ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                      ($value.StartsWith("'") -and $value.EndsWith("'"))
            if ($quoted) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        $map[$key] = $value
    }
    return $map
}

function Get-KstNow() {
    $kst = [System.TimeZoneInfo]::FindSystemTimeZoneById("Korea Standard Time")
    return [System.TimeZoneInfo]::ConvertTime([DateTimeOffset]::Now, $kst)
}

Write-Section "사전 점검"

# 1. 가상환경
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Fail "가상환경을 찾을 수 없습니다: $venvPython" @(
        "저장소 루트에서 아래를 차례로 실행하세요.",
        "",
        "  python -m venv .venv",
        "  .\.venv\Scripts\Activate.ps1",
        "  pip install -r requirements.txt"
    )
}
Write-Ok "가상환경"

# 2. main.py
if (-not (Test-Path -LiteralPath $mainScript -PathType Leaf)) {
    Fail "main.py를 찾을 수 없습니다: $mainScript" @(
        "stock.bat이 저장소 루트에 있는지 확인하세요.",
        "다른 폴더로 복사해서 실행하면 이 오류가 납니다."
    )
}
Write-Ok "main.py"

# 3. .env
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    Fail ".env 파일이 없습니다: $envPath" @(
        "저장소 루트에서 아래를 실행한 뒤 계좌 정보를 채우세요.",
        "",
        "  copy .env.example .env",
        "",
        "앱 키 발급과 Telegram 봇 설정은 docs\DEV_ENV.md 6~7장을 보세요."
    )
}
$envMap = Read-DotEnv $envPath
Write-Ok ".env"

# 4. 실행 모드
$mode = $envMap["KIS_MODE"]
if (-not $mode) {
    Fail ".env에 KIS_MODE가 없습니다." @(
        ".env에 아래 한 줄을 추가하세요.",
        "",
        "  KIS_MODE=PAPER"
    )
}
if ($mode -notin @("DRY_RUN", "PAPER", "REAL")) {
    Fail "KIS_MODE 값을 알 수 없습니다: $mode" @(
        "DRY_RUN, PAPER, REAL 중 하나여야 합니다.",
        "처음이라면 PAPER를 쓰세요."
    )
}

$account = $envMap["KIS_ACCT_NO"]
if (-not $account) { $account = $envMap["KIS_ACCOUNT_NO"] }
if (-not $account) { $account = "(미설정)" }
$maskedAccount = if ($account.Length -gt 4) {
    ("*" * ($account.Length - 4)) + $account.Substring($account.Length - 4)
} else {
    $account
}

$modeColor = if ($mode -eq "REAL") { "Red" } else { "Green" }
Write-Host "  [OK] 실행 모드: " -ForegroundColor Green -NoNewline
Write-Host $mode -ForegroundColor $modeColor
Write-Info "     계좌: $maskedAccount"

if ($mode -eq "REAL") {
    Write-Host ""
    Write-Host "  ****  실계좌 모드입니다. 실제 돈으로 주문이 나갑니다.  ****" -ForegroundColor Red
    Write-Host ""
    $answer = Read-Host "  계속하려면 y를 입력하세요"
    if ($answer -ne "y") {
        Write-Host ""
        Write-Host "  사용자가 취소했습니다." -ForegroundColor Yellow
        Write-Host ""
        exit 0
    }
}

# 5. 중복 실행
$uiPort = 8080
if ($envMap.ContainsKey("UI_PORT")) {
    $parsedPort = 0
    if ([int]::TryParse($envMap["UI_PORT"], [ref]$parsedPort)) {
        $uiPort = $parsedPort
    }
}

function Get-RunningMain([string]$PidFile, [string]$ExpectedPython) {
    if (-not (Test-Path -LiteralPath $PidFile -PathType Leaf)) { return $null }
    $raw = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    $processId = 0
    if (-not [int]::TryParse($raw, [ref]$processId)) { return $null }
    if ($processId -le 0) { return $null }
    $proc = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if (-not $proc) { return $null }
    if (-not $proc.ExecutablePath) { return $null }
    # PID는 재사용된다. 실행 파일이 이 저장소의 venv python인지 대조한다.
    if ([System.IO.Path]::GetFullPath($proc.ExecutablePath) -ne $ExpectedPython) {
        return $null
    }
    return $proc
}

$running = Get-RunningMain $pidPath $venvPython
if ($running) {
    Write-Host ""
    Write-Host "  이미 실행 중입니다." -ForegroundColor Yellow
    Write-Info "     PID: $($running.ProcessId)"
    Write-Info "     화면: http://127.0.0.1:$uiPort"
    Write-Host ""
    Write-Host "  재시작하려면 이 창을 닫고 아래를 실행하세요." -ForegroundColor Yellow
    Write-Host "    .\scripts\restart_main.ps1" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  (지금 실행 중인 봇은 그대로 둡니다.)"
    Write-Host ""
    exit 0
}
Write-Ok "중복 실행 없음"

# 6. UI 포트
$probe = [System.Net.Sockets.TcpListener]::new(
    [System.Net.IPAddress]::Loopback,
    $uiPort
)
$portFree = $true
$portError = ""
try {
    $probe.Start()
} catch {
    $portFree = $false
    $portError = $_.Exception.Message
} finally {
    try { $probe.Stop() } catch { }
}
if (-not $portFree) {
    Fail "화면 포트 $uiPort 를 쓸 수 없습니다. ($portError)" @(
        "다른 프로그램이 이 포트를 쓰고 있습니다.",
        ".env에 아래처럼 다른 포트를 지정하세요.",
        "",
        "  UI_PORT=8081"
    )
}
Write-Ok "화면 포트 $uiPort"

# 7. 장 시간 안내 (차단하지 않음)
$now = Get-KstNow
$timeOfDay = $now.TimeOfDay
$isWeekend = $now.DayOfWeek -in @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)

Write-Section "지금 시각 기준 안내"
Write-Info "현재 (KST): $($now.ToString('yyyy-MM-dd HH:mm:ss'))"

if ($isWeekend) {
    Write-Host "  주말입니다. 장이 열리지 않아 매매는 일어나지 않습니다." -ForegroundColor Yellow
} elseif ($timeOfDay -lt $FIRST_JOB) {
    Write-Host "  장 시작 전입니다. 모든 단계가 예정대로 동작합니다." -ForegroundColor Green
} elseif ($timeOfDay -lt $ENTRY_DEADLINE) {
    Write-Host "  진입 시도 구간입니다. 늦게 켰다면 일부 단계를 건너뜁니다." -ForegroundColor Yellow
} elseif ($timeOfDay -lt $EXIT_TIME) {
    Write-Host "  09:11이 지나 신규 매수는 하지 않습니다." -ForegroundColor Yellow
    Write-Info "보유 중이라면 감시와 마감 청산은 정상 동작합니다."
} else {
    Write-Host "  오늘 매매 시간(15:15)이 지났습니다." -ForegroundColor Yellow
}
Write-Info "공휴일 여부는 확인하지 않습니다. 휴장일이면 봇이 스스로 판단합니다."

if ($CheckOnly) {
    Write-Host ""
    Write-Host "점검만 수행했습니다 (-CheckOnly). 실행하지 않습니다." -ForegroundColor Cyan
    Write-Host ""
    exit 0
}
