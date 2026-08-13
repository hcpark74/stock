# stock.bat 수동 실행 런처 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `main.py`를 더블클릭 한 번으로 안전하게 띄우는 런처를 만든다. 사전 점검 7단계를 통과한 뒤 포그라운드로 실행하고, 화면과 파일에 동시에 로그를 남긴다.

**Architecture:** 저장소 루트의 `stock.bat`은 얇은 shim이고 로직은 전부 `scripts/start_main.ps1`에 둔다. 기존 `acl.bat` → `fix_windows_acl_sandbox.ps1` 패턴과 동일하다. `main.py`를 포함한 전략 지문 대상 파일은 일절 수정하지 않는다.

**Tech Stack:** Windows PowerShell 5.1, cmd batch, Python 3.12 + pytest

## Global Constraints

- **전략 지문 대상 파일을 수정하지 않는다.** `src/release.py`의 `_STRATEGY_FILES` 19개 (`main.py`, `src/state.py`, `src/live.py`, `src/db.py`, `src/api/kis_rest.py`, `src/api/kis_ws.py`, `src/modules/f1_filter.py`, `src/modules/f1_selector.py`, `src/modules/f2_lockup.py`, `src/modules/f3_entry.py`, `src/modules/f4_tracking.py`, `src/modules/f5_timeout.py`, `src/modules/exit_recovery.py`, `src/modules/paper_fast_probe.py`, `src/modules/tick_capture.py`, `src/modules/f1_snapshot_selector.py`, `src/modules/vi_watch.py`, `src/schedule_times.py`, `src/scheduler.py`, `src/utils/number.py`, `src/utils/spike_filter.py`). 이 계획이 수정하는 기존 파일은 `tests/test_release.py`와 `README.md`뿐이다.
- **런처는 전략 환경변수를 주입하지 않는다.** `_STRATEGY_ENV_PREFIXES`는 `F1_`, `F2_`, `F3_`, `F4_`, `F5_`, `PAPER_FAST_`, `TRAILING_SHADOW_`, `STRATEGY_TICK_`, `VI_`, `BALANCE_SNAPSHOT_`, `EXIT_RECONCILE_`, `KIS_RATE_`, `KIS_MAX_TRANSIENT_`, `KIS_TRANSIENT_`, `KIS_LOW_PRIORITY_`이고 `_STRATEGY_ENV_NAMES`는 `FORCE_CATCHUP`이다. 런처가 설정하는 변수는 `PYTHONUTF8`, `PYTHONUNBUFFERED` 둘뿐이며 둘 다 이 목록 밖이다.
- **파일 인코딩:** `scripts/start_main.ps1`은 **UTF-8 with BOM**으로 저장한다. `stock.bat`이 Windows PowerShell 5.1(`powershell.exe`)로 호출하는데, 5.1은 BOM이 없는 `.ps1`을 ANSI(CP949)로 읽어 한글 안내문이 전부 깨진다. 기존 `scripts/fix_windows_acl_sandbox.ps1`이 BOM을 갖는 이유가 이것이다.
- **`stock.bat`은 UTF-8 **without** BOM**으로 저장하고 첫 줄 다음에 `chcp 65001 >nul`을 둔다. `.bat`에 BOM이 있으면 `@echo off`가 파싱되지 않는다.
- PowerShell에서 `$pid`는 예약된 자동 변수다. 프로세스 ID 변수명으로 쓰지 말고 `$processId` 같은 이름을 쓴다.
- 모든 사용자 안내문은 한국어로 쓰고, 실패 메시지는 **원인과 해결 명령어를 함께** 출력한다.

---

## File Structure

| 파일 | 책임 |
|---|---|
| `stock.bat` (신규, 루트) | 경로 확인 → `start_main.ps1` 호출 → 창 유지(`pause`) |
| `scripts/start_main.ps1` (신규) | 사전 점검 7단계, 포그라운드 실행, 로그 tee, 종료 요약 |
| `tests/test_release.py` (수정) | 런처 환경변수가 지문을 바꾸지 않는다는 회귀 테스트 |
| `README.md` (수정) | "3. 실행" 절에 `stock.bat` 안내 추가 |

---

### Task 1: 지문 보호 회귀 테스트

런처가 `PYTHONUTF8`/`PYTHONUNBUFFERED`를 설정해도 전략 지문이 바뀌지 않아야 한다. 지문이 바뀌면 PAPER 실적이 0회부터 다시 시작하고 2~3개월을 되돌린다. 나중에 누군가 런처에 전략 환경변수를 추가하는 회귀를 이 테스트가 잡는다.

**Files:**
- Modify: `tests/test_release.py` (파일 끝에 추가)

**Interfaces:**
- Consumes: `src.release.strategy_fingerprint()` — `@lru_cache(maxsize=1)`이 걸려 있으므로 환경변수를 바꾼 뒤 반드시 `strategy_fingerprint.cache_clear()`를 호출해야 한다. 기존 테스트들이 모두 `try/finally`로 `cache_clear()`를 정리한다.
- Produces: 없음 (테스트만 추가)

- [ ] **Step 1: 회귀 테스트를 파일 끝에 추가**

`tests/test_release.py` 맨 끝에 붙인다. 기존 `test_strategy_fingerprint_excludes_secrets`와 동일한 구조다.

```python
def test_strategy_fingerprint_ignores_launcher_env(monkeypatch):
    """stock.bat 런처가 설정하는 환경변수는 지문을 바꾸지 않아야 한다.

    런처(scripts/start_main.ps1)는 PYTHONUTF8/PYTHONUNBUFFERED만 설정한다.
    여기에 전략 환경변수를 추가하면 지문이 바뀌어 PAPER 실적이 리셋된다.
    """
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    release.strategy_fingerprint.cache_clear()
    try:
        baseline = release.strategy_fingerprint()
        monkeypatch.setenv("PYTHONUTF8", "1")
        monkeypatch.setenv("PYTHONUNBUFFERED", "1")
        release.strategy_fingerprint.cache_clear()
        assert release.strategy_fingerprint() == baseline
    finally:
        release.strategy_fingerprint.cache_clear()
```

- [ ] **Step 2: 테스트가 통과하는지 확인**

Run: `python -m pytest tests/test_release.py::test_strategy_fingerprint_ignores_launcher_env -v`
Expected: PASS

이 테스트는 처음부터 통과한다. `PYTHONUTF8`은 이미 지문 대상이 아니기 때문이다. 다음 단계에서 이 테스트가 실제로 회귀를 잡는지 확인한다.

- [ ] **Step 3: 테스트에 실제 검출력이 있는지 확인 (일시적 개악)**

`src/release.py`의 `_STRATEGY_ENV_PREFIXES` 튜플에 `"PYTHON"` 한 줄을 임시로 추가한다.

```python
_STRATEGY_ENV_PREFIXES = (
    "F1_",
    "PYTHON",
    ...
)
```

- [ ] **Step 4: 테스트가 실패하는지 확인**

Run: `python -m pytest tests/test_release.py::test_strategy_fingerprint_ignores_launcher_env -v`
Expected: FAIL — `assert release.strategy_fingerprint() == baseline`에서 두 지문이 달라 AssertionError

여기서 통과하면 테스트에 검출력이 없는 것이므로 테스트를 고쳐야 한다.

- [ ] **Step 5: 개악을 되돌린다**

`src/release.py`에서 방금 추가한 `"PYTHON",` 줄을 삭제한다.

Run: `git diff src/release.py`
Expected: 이 계획을 시작하기 전과 동일한 diff (런처 작업으로 인한 추가 변경이 없어야 한다)

- [ ] **Step 6: 전체 테스트 통과 확인**

Run: `python -m pytest tests/test_release.py -q`
Expected: 전부 PASS

- [ ] **Step 7: 커밋**

```bash
git add tests/test_release.py
git commit -m "test: guard launcher env vars against fingerprint drift"
```

---

### Task 2: start_main.ps1 사전 점검 7단계

점검만 수행하고 실행은 하지 않는 단계까지 만든다. `-CheckOnly` 스위치로 점검 결과만 보고 끝낼 수 있어야 이후 태스크와 독립적으로 검증할 수 있다.

**Files:**
- Create: `scripts/start_main.ps1` (UTF-8 **with BOM**)

**Interfaces:**
- Consumes: 없음 (신규 파일)
- Produces: Task 3이 같은 파일에 이어 붙인다. 함수로 감싸지 않고 스크립트 스코프 변수를 그대로 쓴다. Task 3이 의존하는 이름은 다음과 같다.
  - `$repoRoot` (string) — 저장소 루트 절대 경로
  - `$venvPython` (string) — `.venv\Scripts\python.exe` 절대 경로
  - `$logDir` (string) — `data\logs` 절대 경로
  - `$uiPort` (int)
  - `$mode` (string) — `DRY_RUN` / `PAPER` / `REAL`
  - 함수 `Write-Section`, `Write-Info`, `Get-KstNow`

**설계 대비 의도한 차이:** 스펙 §4-2는 중복 실행 판정에 `tasklist`를 쓴다고 적었으나, 이 계획은 `Get-CimInstance Win32_Process`를 쓴다. 생존 확인과 실행 파일 경로 대조를 한 번의 호출로 끝낼 수 있고, `restart_main.ps1`이 이미 같은 API를 쓰기 때문이다. 판정 결과는 동일하다.

- [ ] **Step 1: 스크립트 뼈대와 헬퍼 함수 작성**

`scripts/start_main.ps1`을 만든다. **UTF-8 with BOM으로 저장할 것.**

```powershell
<#
.SYNOPSIS
    stock.bat이 호출하는 수동 실행 런처.
.DESCRIPTION
    사전 점검 7단계를 수행한 뒤 main.py를 포그라운드로 실행하고,
    화면과 data\logs\launcher_*.log에 동시에 출력한다.

    이 스크립트는 실행 중인 프로세스를 종료하지 않는다. 재시작은
    안전점검(restart_guard.py)을 거치는 scripts\restart_main.ps1이 담당한다.

    설계: docs/superpowers/specs/2026-08-14-stock-bat-launcher-design.md
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
$FIRST_JOB = [TimeSpan]::new(8, 59, 45)   # PAPER_FAST_PROBE
$ENTRY_DEADLINE = [TimeSpan]::new(9, 11, 0)  # F3_FILL_DEADLINE
$EXIT_TIME = [TimeSpan]::new(15, 15, 0)   # F5_EXEC

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
```

- [ ] **Step 2: 점검 1~3 (venv / main.py / .env) 추가**

Step 1의 내용 뒤에 이어 붙인다.

```powershell
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
```

- [ ] **Step 3: 점검 4 (모드 확인) 추가**

```powershell
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
```

- [ ] **Step 4: 점검 5 (중복 실행) 추가**

`$pid`는 PowerShell 예약 변수이므로 쓰지 않는다.

```powershell
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
```

- [ ] **Step 5: 점검 6 (UI 포트) 추가**

`netstat` 파싱이 아니라 실제 바인딩으로 판별한다. `restart_main.ps1:186-196`과 동일한 방식이다.

```powershell
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
```

- [ ] **Step 6: 점검 7 (장 시간 안내) 추가**

차단하지 않는다. 공휴일 판정도 하지 않는다.

```powershell
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
```

- [ ] **Step 7: BOM 인코딩 확인**

Run: `python -c "print(open('scripts/start_main.ps1','rb').read(3) == b'\xef\xbb\xbf')"`
Expected: `True`

`False`면 BOM 없이 저장된 것이다. 다시 저장한다.

```powershell
$content = Get-Content -LiteralPath scripts\start_main.ps1 -Raw
[System.IO.File]::WriteAllText(
    (Resolve-Path scripts\start_main.ps1),
    $content,
    (New-Object System.Text.UTF8Encoding $true)
)
```

- [ ] **Step 8: 점검 통과 경로 확인**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_main.ps1 -CheckOnly`
Expected: 한글이 깨지지 않고, 점검 항목이 `[OK]`로 표시된 뒤 "점검만 수행했습니다"로 끝난다. 종료 코드 0.

한글이 깨지면 Step 7의 BOM 문제다.

- [ ] **Step 9: 실패 경로 4개 수동 확인**

각각 `-CheckOnly`로 실행해 확인한 뒤 원상복구한다.

1. `.env`를 `.env.bak`으로 이름 변경 → `copy .env.example .env` 안내가 나오는지 → 되돌리기
2. `.venv`를 `.venv.bak`으로 이름 변경 → `python -m venv .venv` 안내가 나오는지 → 되돌리기
3. `.env`의 `KIS_MODE`를 `FOO`로 변경 → "값을 알 수 없습니다: FOO" 안내가 나오는지 → 되돌리기
4. UI 포트를 다른 프로세스로 점유한 뒤 실행 → `UI_PORT=8081` 안내가 나오는지

포트 점유는 별도 창에서 아래를 띄워두고 확인한다.

```powershell
powershell -NoProfile -Command "$l = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 8080); $l.Start(); Write-Host '8080 점유 중 - Enter로 해제'; Read-Host; $l.Stop()"
```

Expected: 네 경우 모두 종료 코드 1, 해결 명령어가 함께 출력됨

- [ ] **Step 10: REAL 확인 프롬프트 동작 확인**

`.env`의 `KIS_MODE`를 `REAL`로 바꾸고 `-CheckOnly`로 실행한다.

Expected:
- 계좌번호가 뒤 4자리만 보이고 앞자리는 `*`로 가려진다
- 빨간색 경고와 함께 `y` 입력을 요구한다
- `n`을 입력하면 "사용자가 취소했습니다"가 뜨고 종료 코드 0으로 끝난다

확인 후 `.env`를 원상복구한다. **`y`를 눌러 실제로 진행하지 말 것.**

- [ ] **Step 11: 커밋**

```bash
git add scripts/start_main.ps1
git commit -m "feat: add launcher preflight checks"
```

---

### Task 3: 포그라운드 실행, 로그 tee, 종료 처리

**Files:**
- Modify: `scripts/start_main.ps1` (Task 2에서 만든 파일 끝에 추가)

**Interfaces:**
- Consumes: Task 2가 정의한 `$repoRoot`, `$venvPython`, `$logDir`, `$uiPort`, `$mode` 변수와 `Write-Section` / `Write-Info` 함수
- Produces: 없음 (스크립트 완성)

- [ ] **Step 1: 로그 준비와 오래된 로그 정리 추가**

Task 2의 `-CheckOnly` 블록 뒤에 이어 붙인다.

```powershell
Write-Section "실행"

New-Item -ItemType Directory -Path $logDir -Force | Out-Null

# 런처 로그는 실행마다 쌓인다. 최근 30개만 남긴다.
# 새 파일을 곧 만들 것이므로 기존 29개까지만 남기고 지운다.
Get-ChildItem -LiteralPath $logDir -Filter "launcher_*.log" -File `
    -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 29 |
    Remove-Item -Force -ErrorAction SilentlyContinue

$stamp = (Get-KstNow).ToString("yyyyMMdd_HHmmss")
$consoleLog = Join-Path $logDir "launcher_$stamp.log"
```

- [ ] **Step 2: 실행 블록 추가**

전략 환경변수는 절대 설정하지 않는다. 여기서 설정하는 두 변수만 허용된다.

```powershell
Write-Info "모드: $mode"
Write-Info "화면: http://127.0.0.1:$uiPort"
Write-Info "로그: $consoleLog"
Write-Host ""
Write-Host "  중지하려면 이 창에서 Ctrl+C를 누르세요." -ForegroundColor Yellow
Write-Host ""

# PYTHONUTF8/PYTHONUNBUFFERED는 _STRATEGY_ENV_PREFIXES 밖이라 지문에
# 영향이 없다. 전략 환경변수(F1_~F5_, VI_, KIS_RATE_ 등)를 여기에
# 추가하면 지문이 바뀌어 PAPER 실적이 리셋된다.
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"

$exitCode = 0
Push-Location -LiteralPath $repoRoot
try {
    # -u와 PYTHONUNBUFFERED로 버퍼링을 없애야 Tee-Object가 실시간으로
    # 흘려보낸다. 2>&1로 트레이스백까지 파일에 남긴다.
    & $venvPython -u main.py 2>&1 | Tee-Object -FilePath $consoleLog
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
```

- [ ] **Step 3: 종료 요약 추가**

Ctrl+C를 구분해 처리한다. Windows에서 콘솔 Ctrl+C로 죽은 프로세스의 종료 코드는 `0xC000013A`(십진 `-1073741510`)이다.

> **알아둘 것:** Windows PowerShell 5.1에서 Ctrl+C는 스크립트 자체를 즉시 끊기 때문에 아래 요약 블록이 아예 실행되지 않을 수 있다. 그래서 `stock.bat`의 `pause`가 최후의 안전장치다. 요약이 안 뜨고 바로 "계속하려면 아무 키나" 가 나오는 것은 정상이며, 이 경우 로그 경로는 실행 시작 시 출력한 줄에서 확인한다.

```powershell
Write-Section "종료"

$CTRL_C_EXIT = -1073741510  # 0xC000013A STATUS_CONTROL_C_EXIT

if ($exitCode -eq 0) {
    Write-Host "  정상 종료되었습니다." -ForegroundColor Green
} elseif ($exitCode -eq $CTRL_C_EXIT) {
    Write-Host "  Ctrl+C로 중단했습니다." -ForegroundColor Yellow
} else {
    Write-Host "  비정상 종료되었습니다 (종료 코드: $exitCode)" -ForegroundColor Red
    Write-Host ""
    Write-Host "  마지막 로그 15줄:" -ForegroundColor Yellow
    Get-Content -LiteralPath $consoleLog -Tail 15 -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host "    $_" }
}

Write-Host ""
Write-Info "실행 로그: $consoleLog"
Write-Info "이벤트 로그: $logDir\$((Get-KstNow).ToString('yyyyMMdd')).jsonl"
Write-Host ""
Write-Info "Ctrl+C로 중단하면 실행 로그의 마지막 몇 줄이 빠질 수 있습니다."
Write-Info "사후 확인은 이벤트 로그(.jsonl)를 먼저 보세요."
Write-Host ""

exit $exitCode
```

- [ ] **Step 4: DRY_RUN으로 실제 기동 확인**

`.env`의 `KIS_MODE`를 `DRY_RUN`으로 두고 실행한다.

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_main.ps1`
Expected:
- 로그가 창에 **실시간으로** 흐른다 (한 번에 몰려 나오면 버퍼링 문제)
- `data\logs\launcher_<타임스탬프>.log`가 생기고 창과 같은 내용이 들어 있다
- `http://127.0.0.1:8080`이 열린다

확인 후 Ctrl+C로 중단한다.

- [ ] **Step 5: 로그 파일 내용 대조**

Run: `powershell -NoProfile -Command "Get-ChildItem data\logs\launcher_*.log | Sort-Object LastWriteTime -Descending | Select-Object -First 1 | Get-Content -Tail 20"`
Expected: 창에서 봤던 마지막 줄들이 파일에도 있다

- [ ] **Step 6: 로그 정리 동작 확인**

31개 이상을 만든 뒤 실행해 30개로 줄어드는지 본다.

Run:
```powershell
powershell -NoProfile -Command "1..32 | ForEach-Object { New-Item -ItemType File -Path ('data\logs\launcher_dummy{0:D2}.log' -f $_) -Force | Out-Null }; (Get-ChildItem data\logs\launcher_*.log).Count"
```
Expected: 32 이상 출력

그다음 `-CheckOnly` 없이 실행했다가 바로 Ctrl+C로 끊고 다시 센다.

Run: `powershell -NoProfile -Command "(Get-ChildItem data\logs\launcher_*.log).Count"`
Expected: 30

확인 후 남은 더미 파일을 지운다.

Run: `powershell -NoProfile -Command "Remove-Item data\logs\launcher_dummy*.log -ErrorAction SilentlyContinue"`

- [ ] **Step 7: 비정상 종료 경로 확인**

`.env`의 `KIS_APP_KEY`를 빈 값으로 바꿔 기동 실패를 유도한다.

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_main.ps1`
Expected: "비정상 종료되었습니다 (종료 코드: N)"과 마지막 15줄이 화면에 다시 출력된다

확인 후 `.env`를 원상복구한다.

- [ ] **Step 8: 지문이 바뀌지 않았는지 확인**

Run: `python -c "from src.release import strategy_fingerprint; print(strategy_fingerprint())"`
Expected: 이 계획을 시작하기 전에 출력했던 값과 동일

> 주의: 이 저장소의 작업 트리에는 런처와 무관한 미커밋 변경이 있다. 비교 기준은 HEAD가 아니라 **런처 작업을 시작하기 직전의 작업 트리 상태**다. Task 1 시작 전에 이 명령을 한 번 실행해 값을 적어두고, 여기서 대조한다.

- [ ] **Step 9: 커밋**

```bash
git add scripts/start_main.ps1
git commit -m "feat: run main.py in foreground with tee logging"
```

---

### Task 4: stock.bat과 README 안내

**Files:**
- Create: `stock.bat` (루트, UTF-8 **without** BOM)
- Modify: `README.md:227-239` ("3. 실행" 절)

**Interfaces:**
- Consumes: Task 2~3이 만든 `scripts/start_main.ps1`
- Produces: 없음

- [ ] **Step 1: stock.bat 작성**

`acl.bat`의 패턴을 따르되 관리자 권한은 요구하지 않는다. **UTF-8 without BOM으로 저장할 것** — BOM이 있으면 `@echo off`가 파싱되지 않는다.

```bat
@echo off
chcp 65001 >nul
setlocal

set "SCRIPT_DIR=%~dp0"
set "PS_SCRIPT=%SCRIPT_DIR%scripts\start_main.ps1"

if not exist "%PS_SCRIPT%" (
  echo.
  echo   [실패] 실행 스크립트를 찾을 수 없습니다:
  echo     %PS_SCRIPT%
  echo.
  echo   해결 방법:
  echo     stock.bat은 저장소 폴더 안에서 실행해야 합니다.
  echo     바탕화면 등으로 복사해서 실행하면 이 오류가 납니다.
  echo     바로가기를 만들고 싶다면 파일을 복사하지 말고
  echo     마우스 오른쪽 버튼 - 바로 가기 만들기를 쓰세요.
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" %*
set "EXITCODE=%errorlevel%"

echo.
pause
exit /b %EXITCODE%
```

- [ ] **Step 2: BOM이 없는지 확인**

Run: `python -c "print(open('stock.bat','rb').read(3) != b'\xef\xbb\xbf')"`
Expected: `True`

- [ ] **Step 3: 더블클릭 경로 확인**

탐색기에서 `stock.bat`을 실제로 더블클릭한다. (터미널에서 실행하는 것과 콘솔 코드페이지 동작이 다를 수 있으므로 반드시 더블클릭으로 확인한다.)

Expected:
- 한글 안내문이 깨지지 않는다
- 점검이 순서대로 표시된다
- 종료 후 창이 닫히지 않고 "계속하려면 아무 키나 누르십시오"가 뜬다

- [ ] **Step 4: 중복 실행 안내 확인**

`stock.bat`으로 봇을 띄운 상태에서 `stock.bat`을 한 번 더 더블클릭한다.

Expected:
- "이미 실행 중입니다"와 PID, 화면 주소가 표시된다
- `restart_main.ps1` 안내가 나온다
- **먼저 띄운 봇이 계속 살아 있다** (두 번째 창을 닫아도 영향 없음)

확인 후 첫 번째 창에서 Ctrl+C로 중단한다.

- [ ] **Step 5: README "3. 실행" 절 교체**

`README.md:227-239`의 다음 내용을

````markdown
### 3. 실행

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

안전 재시작은 포지션과 미해결 주문이 없을 때만 실행됩니다.

```powershell
.\scripts\restart_main.ps1 -WhatIf
.\scripts\restart_main.ps1
```
````

아래로 바꾼다.

````markdown
### 3. 실행

저장소 폴더의 **`stock.bat`을 더블클릭**하세요. 가상환경, `.env`, 실행 모드,
중복 실행, 화면 포트를 차례로 점검한 뒤 실행합니다. 문제가 있으면 원인과
해결 명령어를 화면에 보여주고 멈춥니다.

실계좌(`REAL`) 모드일 때는 실행 전에 한 번 더 확인을 받습니다.

로그는 창에 실시간으로 흐르면서 `data\logs\launcher_<날짜>_<시각>.log`에도
남습니다. 중지하려면 창에서 Ctrl+C를 누르세요.

명령어로 실행하려면 다음과 같이 합니다.

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

이미 실행 중인 봇을 재시작할 때는 `stock.bat`이 아니라 아래를 쓰세요. 포지션과
미해결 주문이 없을 때만 실행됩니다.

```powershell
.\scripts\restart_main.ps1 -WhatIf
.\scripts\restart_main.ps1
```
````

- [ ] **Step 6: 전체 테스트 통과 확인**

Run: `python -m pytest -q`
Expected: 전부 PASS (Task 1에서 추가한 테스트 1개만큼 늘어난 수)

- [ ] **Step 7: 커밋**

```bash
git add stock.bat README.md
git commit -m "feat: add stock.bat double-click launcher"
```

---

## 최종 확인

- [ ] `git status`에 `main.py`를 비롯한 `_STRATEGY_FILES` 19개 파일이 이 작업으로 인해 새로 수정되지 않았는지 확인
- [ ] `python -c "from src.release import strategy_fingerprint; print(strategy_fingerprint())"`가 작업 시작 전 값과 동일한지 확인
- [ ] `data\logs\launcher_*.log`가 30개를 넘지 않는지 확인
