# tests/batch_mab_test.ps1 — v2: saves full responses for Langfuse evaluation
# Run from project root: .\tests\batch_mab_test.ps1

$API_KEY = "5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE"
$BASE_URL = "http://localhost:8000/v1/infer"
$TEMP_FILE = "tests\_tmp.json"
$RESULTS_FILE = "tests\batch_results.json"

$TESTS = @(
    @{ prompt = "What is the time complexity of quicksort in best, average, and worst case?"; policy = "sla-aware" }
    @{ prompt = "Write a Python function that finds all primes up to N using Sieve of Eratosthenes."; policy = "latency-first" }
    @{ prompt = "Explain gradient descent in machine learning with a simple analogy."; policy = "cost-first" }
    @{ prompt = "What are the SOLID principles in software engineering? Give a one-line example for each."; policy = "sla-aware" }
    @{ prompt = "How does HTTPS work? Explain the TLS handshake in simple terms."; policy = "latency-first" }
    @{ prompt = "What is the difference between Docker and a virtual machine?"; policy = "cost-first" }
    @{ prompt = "Explain the CAP theorem with a real-world example for each property."; policy = "sla-aware" }
    @{ prompt = "Write a SQL query to find the top 5 customers by total revenue from an orders table."; policy = "latency-first" }
    @{ prompt = "What is backpropagation and why is it important in neural networks?"; policy = "cost-first" }
    @{ prompt = "Explain how Redis handles persistence with RDB and AOF."; policy = "sla-aware" }
    @{ prompt = "What is the difference between TCP and UDP? When would you use each?"; policy = "latency-first" }
    @{ prompt = "Explain eventual consistency vs strong consistency in distributed databases."; policy = "cost-first" }
    @{ prompt = "How does a load balancer decide which server to route traffic to?"; policy = "sla-aware" }
    @{ prompt = "What is the difference between heap and stack memory management?"; policy = "latency-first" }
    @{ prompt = "Explain idempotency in REST APIs and why it matters."; policy = "cost-first" }
    @{ prompt = "What are the differences between PostgreSQL and MongoDB? When would you use each?"; policy = "sla-aware" }
    @{ prompt = "How does the Linux kernel handle process scheduling?"; policy = "latency-first" }
    @{ prompt = "What is a Bloom filter and what problem does it solve?"; policy = "cost-first" }
    @{ prompt = "Explain the difference between authentication and authorisation with examples."; policy = "sla-aware" }
    @{ prompt = "What is the observer design pattern? Give a Python code example."; policy = "latency-first" }
)

$allResponses = @()
$results = @()
$total = $TESTS.Count

Write-Host "====================================================="
Write-Host " TINAI MAB Batch Test -- $total prompts"
Write-Host "====================================================="

for ($i = 0; $i -lt $total; $i++) {
    $t = $TESTS[$i]
    $num = $i + 1
    $short = $t.prompt.Substring(0, [Math]::Min(50, $t.prompt.Length))

    Write-Host ("[$num/$total] {0,-14} {1}..." -f $t.policy, $short) -NoNewline

    $body = '{"prompt":"' + $t.prompt.Replace('"', '\"') + '","policy":"' + $t.policy + '"}'
    [System.IO.File]::WriteAllText((Resolve-Path ".").Path + "\" + $TEMP_FILE, $body)

    try {
        $raw = curl.exe -s -X POST $BASE_URL -H "Content-Type: application/json" -H "x-api-key: $API_KEY" -d "@$TEMP_FILE"
        $response = $raw | ConvertFrom-Json

        if ($response.PSObject.Properties.Name -contains "output_text") {
            $row = [PSCustomObject]@{
                Num        = $num
                Policy     = $t.policy
                Prompt     = $t.prompt
                Provider   = $response.provider
                Model      = $response.model
                LatencyMs  = $response.latency_ms
                Tokens     = $response.token_count
                CostC      = $response.cost_cents
                Cache      = $response.cache_hit
                RequestId  = $response.request_id
                OutputText = $response.output_text
            }
            $results += $row
            $allResponses += $row

            Write-Host ("  -> {0} ({1}ms, {2:N4}c)" -f $response.provider.ToUpper(), $response.latency_ms, $response.cost_cents)
        }
        else {
            Write-Host ("  -> FAILED: {0}" -f $response.detail)
            $results += [PSCustomObject]@{
                Num = $num; Policy = $t.policy; Prompt = $t.prompt; Provider = "ERROR"
                Model = "-"; LatencyMs = 0; Tokens = 0; CostC = 0; Cache = $false
                RequestId = ""; OutputText = ""
            }
        }
    }
    catch {
        Write-Host "  -> EXCEPTION: $_"
    }

    Start-Sleep -Milliseconds 300
}

# Save full results to JSON for Langfuse evaluation
$allResponses | ConvertTo-Json -Depth 5 | Out-File -Encoding utf8 $RESULTS_FILE
Write-Host ""
Write-Host "Full responses saved to $RESULTS_FILE"

Remove-Item -ErrorAction SilentlyContinue $TEMP_FILE

$ok = $results | Where-Object { $_.Provider -ne "ERROR" }

Write-Host ""
Write-Host "====================================================="
Write-Host " SUMMARY"
Write-Host "====================================================="
Write-Host ("Total: {0} | OK: {1} | Failed: {2}" -f $total, $ok.Count, ($results | Where-Object { $_.Provider -eq "ERROR" }).Count)
Write-Host ""

Write-Host "-- Provider Distribution --"
$results | Group-Object Provider | Sort-Object Count -Descending | ForEach-Object {
    $pct = [int](($_.Count / $total) * 100)
    Write-Host ("  {0,-12} {1,2} calls  ({2}%)" -f $_.Name, $_.Count, $pct)
}

Write-Host ""
Write-Host "-- Avg Latency by Provider --"
$ok | Group-Object Provider | ForEach-Object {
    $avg = [int](($_.Group | Measure-Object LatencyMs -Average).Average)
    Write-Host ("  {0,-12} avg {1}ms" -f $_.Name, $avg)
}

Write-Host ""
Write-Host "-- Avg Cost by Provider (cents) --"
$ok | Group-Object Provider | ForEach-Object {
    $avg = ($_.Group | Measure-Object CostC -Average).Average
    Write-Host ("  {0,-12} avg {1:N6}c" -f $_.Name, $avg)
}

Write-Host ""
Write-Host "-- Policy -> Provider Routing --"
$ok | Group-Object Policy | Sort-Object Name | ForEach-Object {
    $pol = $_.Name
    $dist = ($_.Group | Group-Object Provider | ForEach-Object { "$($_.Name)x$($_.Count)" }) -join ", "
    Write-Host ("  {0,-14} -> {1}" -f $pol, $dist)
}

Write-Host ""
Write-Host "-- Full Table (no output_text) --"
$results | Select-Object Num, Policy, Provider, LatencyMs, Tokens, CostC, Cache | Format-Table -AutoSize

Write-Host "====================================================="
Write-Host " Next: run .\tests\langfuse_eval.ps1 to score quality"
Write-Host "====================================================="
