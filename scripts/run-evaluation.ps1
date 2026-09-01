$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

function Resolve-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @{ Exe = "py"; Prefix = @("-3") }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @{ Exe = "python"; Prefix = @() }
    }
    if (Get-Command python3 -ErrorAction SilentlyContinue) {
        return @{ Exe = "python3"; Prefix = @() }
    }
    throw "Python 3 was not found."
}

function Invoke-Python([string[]]$Arguments) {
    $cmd = Resolve-Python
    & $cmd.Exe @($cmd.Prefix) @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed: $($Arguments -join ' ')"
    }
}

foreach ($tool in @("cargo", "rustc")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool was not found in PATH."
    }
}

$env:PYTHONPATH = Join-Path $Root "tools"
$Output = Join-Path $Root "artifacts/multi-case/latest"
if (Test-Path $Output) {
    Remove-Item $Output -Recurse -Force
}

Write-Host "[1/2] Multi-case regression tests"
Invoke-Python @(
    "-m", "unittest", "-v",
    "tools/test_application_mutations.py",
    "tools/test_communication_link_case.py",
    "tools/test_multi_case_assurance.py",
    "tools/test_research_baseline_hardening.py"
)

Write-Host "[2/2] Authoritative two-case assurance pipeline"
Invoke-Python @(
    "tools/multi_case_assurance_pipeline.py",
    "--root", $Root,
    "--suite", "applications/research-suite.json",
    "--output", $Output,
    "--compile-backend",
    "--check-reproducibility"
)

$SummaryPath = Join-Path $Output "multi-case-results-summary.json"
$SchemaPath = Join-Path $Output "multi-case-schema-validation.json"
if (-not (Test-Path $SummaryPath)) { throw "multi-case-results-summary.json was not produced" }
if (-not (Test-Path $SchemaPath)) { throw "multi-case-schema-validation.json was not produced" }

$Result = Get-Content $SummaryPath -Raw | ConvertFrom-Json
$Schema = Get-Content $SchemaPath -Raw | ConvertFrom-Json
$Aggregate = $Result.aggregate

if (-not $Result.authoritative_full_pipeline) { throw "Multi-case result is not authoritative" }
if ($Result.assembly_validation_mode) { throw "Authoritative run is marked as assembly validation" }
if ($Result.application_count -ne 2) { throw "Expected two applications" }
if ($Aggregate.equivalent_cycles -ne $Aggregate.executed_cycles) { throw "Three-way cycle mismatch remains" }
if ($Aggregate.generated_three_way_executed_cycles -ne $Aggregate.generated_cycles) { throw "Generated-suite three-way execution is incomplete" }
if ($Aggregate.generated_three_way_equivalent_cycles -ne $Aggregate.generated_three_way_executed_cycles) { throw "Generated-suite three-way equivalence mismatch remains" }
if ($Aggregate.generated_three_way_mismatches -ne 0) { throw "Generated-suite three-way mismatches remain" }
if ($Aggregate.assurance_obligations_covered -ne $Aggregate.assurance_obligations_total) { throw "Assurance obligations remain open" }
if ($Aggregate.selected_mcdc_covered -ne $Aggregate.selected_mcdc_total) { throw "Selected MC/DC remains open" }
if ($Aggregate.controlled_mutants_killed -ne $Aggregate.controlled_mutants_total) { throw "Controlled mutants survived" }
if (-not $Result.reproducibility.byte_identical) { throw "Multi-case reproducibility failed" }
if (-not $Schema.all_valid) { throw "Multi-case root schemas failed" }
if (-not $Result.passed) { throw "Multi-case pipeline did not pass" }

Write-Host ""
Write-Host "AEROST MULTI-CASE FULL PIPELINE PASS" -ForegroundColor Green
Write-Host "Open: $SummaryPath"
