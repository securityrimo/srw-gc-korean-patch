[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$SourceIso,

    [Parameter(Position = 1)]
    [string]$OutputIso
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$PatchFileName = 'SRW_GC_Korean_v1.0.7.xdelta'
$ExpectedSourceSha256 = 'AD4CB99FFB3C0383802A2AB87963F98BA417DFC5184ED3FE3DFE077DA02DB229'
$ExpectedFinalSha256 = 'C1CCEA5FF63BD717E57E1EB3820C8D10C6B377C3456C92CE4C28C4EA6A21A3ED'
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PatchPath = Join-Path $ScriptRoot $PatchFileName

function Get-NormalizedSha256 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath
    )

    return (Get-FileHash -LiteralPath $LiteralPath -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Find-Xdelta {
    $LocalXdelta = Join-Path $ScriptRoot 'xdelta3.exe'
    if (Test-Path -LiteralPath $LocalXdelta -PathType Leaf) {
        return $LocalXdelta
    }

    foreach ($CommandName in @('xdelta3', 'xdelta')) {
        $Command = Get-Command $CommandName -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $Command) {
            return $Command.Source
        }
    }

    throw 'xdelta3를 찾을 수 없습니다. xdelta3.exe를 스크립트와 같은 폴더에 두거나 xdelta3/xdelta를 PATH에 등록하세요.'
}

if ($ExpectedFinalSha256 -notmatch '^[0-9A-Fa-f]{64}$') {
    throw '릴리스의 최종 SHA-256이 아직 설정되지 않았습니다. 공식 릴리스 파일을 다시 받으세요.'
}

if (-not (Test-Path -LiteralPath $SourceIso -PathType Leaf)) {
    throw "원본 ISO를 찾을 수 없습니다: $SourceIso"
}

if (-not (Test-Path -LiteralPath $PatchPath -PathType Leaf)) {
    throw "패치 파일을 찾을 수 없습니다: $PatchPath"
}

$ResolvedSource = (Resolve-Path -LiteralPath $SourceIso).Path
$SourceItem = Get-Item -LiteralPath $ResolvedSource

if ([string]::IsNullOrWhiteSpace($OutputIso)) {
    $ResolvedOutput = Join-Path $SourceItem.DirectoryName 'Super Robot Taisen GC_Korean_v1.0.7.iso'
} else {
    $ResolvedOutput = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputIso)
}

$OutputDirectory = Split-Path -Parent $ResolvedOutput
if (-not (Test-Path -LiteralPath $OutputDirectory -PathType Container)) {
    throw "출력 폴더를 찾을 수 없습니다: $OutputDirectory"
}

if ([System.StringComparer]::OrdinalIgnoreCase.Equals($ResolvedSource, $ResolvedOutput)) {
    throw '원본 ISO와 출력 ISO 경로는 달라야 합니다.'
}

if (Test-Path -LiteralPath $ResolvedOutput) {
    throw "출력 파일이 이미 있습니다. 자동으로 덮어쓰지 않습니다: $ResolvedOutput"
}

Write-Host '[1/3] 일본판 원본 SHA-256을 확인합니다...'
$SourceSha256 = Get-NormalizedSha256 -LiteralPath $ResolvedSource
if ($SourceSha256 -ne $ExpectedSourceSha256) {
    throw "지원하지 않는 원본입니다.`n예상: $ExpectedSourceSha256`n실제: $SourceSha256"
}

$Xdelta = Find-Xdelta
Write-Host "[2/3] 패치를 적용합니다: $Xdelta"

try {
    & $Xdelta -d -s $ResolvedSource $PatchPath $ResolvedOutput
    if ($LASTEXITCODE -ne 0) {
        throw "xdelta3가 종료 코드 $LASTEXITCODE 을(를) 반환했습니다."
    }

    if (-not (Test-Path -LiteralPath $ResolvedOutput -PathType Leaf)) {
        throw 'xdelta3가 성공을 보고했지만 출력 ISO가 만들어지지 않았습니다.'
    }

    Write-Host '[3/3] 결과 SHA-256을 확인합니다...'
    $FinalSha256 = Get-NormalizedSha256 -LiteralPath $ResolvedOutput
    if ($FinalSha256 -ne $ExpectedFinalSha256) {
        throw "결과 검증에 실패했습니다.`n예상: $ExpectedFinalSha256`n실제: $FinalSha256"
    }
} catch {
    if (Test-Path -LiteralPath $ResolvedOutput -PathType Leaf) {
        Remove-Item -LiteralPath $ResolvedOutput -Force
    }
    throw
}

Write-Host ''
Write-Host '패치 적용과 SHA-256 검증이 완료되었습니다.' -ForegroundColor Green
Write-Host "출력: $ResolvedOutput"
Write-Host "SHA-256: $ExpectedFinalSha256"
