# Test results and benchmark evidence

**Evidence date:** 31 October 2026  
**Environment:** local Windows development run, FastAPI on `127.0.0.1:8001`; offline/deterministic mode for regression/API checks unless stated otherwise.  
**Data:** mock systems of record and authored synthetic tickets.

> These figures record the verification session performed for this project. They are not production measurements and do not establish Paytm production accuracy, financial safety certification, or real-world SLA performance.

## 1. Automated regression tests

| Check | Observed result |
|---|---:|
| `python -m pytest -q` | **33 passed** |
| Pytest-reported duration | **11.39 s** |

The suite covers gateway/policy behaviour, mocked model-provider contracts, ML behaviour, rigor evaluation, Sarvam request contracts, and scenario/synthetic invariants. The clean documentation-release copy was also re-tested during packaging with Python 3.13.5: **33 passed in 6.35 s**.

## 2. Scenario suite

| Metric | Observed result |
|---|---:|
| Scenario suite | **22 / 22 passed** |
| Unsafe actions | **0** |
| Grounded rate | **1.0** |
| Script-match rate | **1.0** |

The manual API verification also exercised failed payment, Hindi settlement name mismatch, fraud-held settlement, prompt injection, cross-account access, OTP scam, chargeback approval, EMI customer consent, novel-issue escalation, and audit-chain validation.

## 3. Synthetic replay and noise sweep

### Main replay

| Setting / metric | Observed result |
|---|---:|
| Ticket count (`n`) | **300** |
| Noise | **0.50** |
| Seed | **11** |
| Decision accuracy | **1.000** |
| Escalation recall | **1.000** |
| Overreach cases | **0** |
| Invariant violations | **0** |
| Wrong-action rate | **0.0** |

The synthetic harness checks properties such as no release of risk-held settlement, no unauthorized money movement on injection/scam paths, no cross-account access, and refund boundaries. These are generated records, not production tickets.

### Noise sweep

Recorded noise settings were `0`, `0.25`, and `0.5` with 300 synthetic cases per run. At each level, the displayed result was 100% decision accuracy, zero overreach cases, zero invariant violations, and 100% escalation recall.

## 4. Intent ensemble evaluation

| Noise | Local ML accuracy | Rules accuracy | Ensemble misroute rate |
|---:|---:|---:|---:|
| 0.00 | 0.971 | 0.868 | **0.000** |
| 0.25 | 0.930 | 0.739 | **0.000** |
| 0.50 | 0.888 | 0.630 | **0.000** |

Interpretation: the ensemble avoided misroutes in this authored evaluation; this does not mean every input is classified correctly. It can defer uncertain cases, and evaluation data is synthetic.

## 5. Calibration and selective prediction

The conformal evaluator targets 90% coverage (`alpha=0.1`). Recorded slice coverage:

| Evaluation slice | Coverage | Target | Accuracy when prediction set was singleton |
|---|---:|---:|---:|
| Unseen phrasing, clean | 0.977 | 0.900 | 0.996 |
| Unseen phrasing, 25% typos | 0.962 | 0.900 | 0.979 |
| Unseen phrasing, ASR-style errors | **0.858** | 0.900 | **0.867** |

**Important finding:** the ASR-style slice fell below the 90% target. This is a known limitation, not a result to hide. In the selective-prediction curve, ASR-style accuracy was 0.934 at 80% coverage and 0.994 at 70% coverage. A real deployment needs representative speech/ASR data, recalibration, and a conservative human fallback.

## 6. Benchmark measurements

Two benchmark outputs were observed in the verification session. They came from separate runs/views and should not be presented as a single directly comparable measurement.

### API/terminal benchmark output

| Metric | Observed result |
|---|---:|
| Throughput | **160.2 cases/s** |
| Case p95 latency | **0.27 s** |
| Guard p95 | **2.249 ms** |

### Operations-console benchmark screenshot

| Metric | Observed result |
|---|---:|
| Throughput | **51.6 cases/s** |
| Case latency p50 / p95 | **630 ms / 800 ms** |
| Guard p95 | **4.82 ms** |
| Intent ensemble p95 | **1.319 ms** |
| Resolved | **300 / 300** |
| Concurrency shown | **50** |

The benchmark code describes a single Python process with mock systems and no network. Remote LLM/Sarvam calls add their own latency. Cost-per-case figures in the UI are illustrative assumptions, not provider invoices or contracted pricing.

## 7. Incident-detector simulation

The dashboard showed this simulated comparison:

| Detector | Median detection delay (events) | p95 delay | Missed | False-alarm streams |
|---|---:|---:|---:|---:|
| Window threshold (12 failures and 35%) | 45 | 58 | 0 | 0 |
| CUSUM (`k=0.12`, `h=4`) | **12** | **25** | 0 | 0 |

This is a simulation, not a live bank-stream measurement.

## 8. Manual browser verification

The user completed these checks in Chrome during the local session:

- Five operations-console tabs loaded; no red console errors were reported.
- Customer-app XSS sample `<img src=x onerror=alert(1)>` appeared as text; no alert popup appeared.
- Hindi microphone interaction produced a recognized message and Hindi assistant reply.
- AI Stack benchmark rendered metrics; the user reported the app continued to work normally while navigating.

The microphone test confirms the visible end-to-end interaction. It does not by itself identify whether capture used the Sarvam STT endpoint or browser fallback. Record provider-specific STT/TTS checks separately when presenting live provider integration.

## 9. Assertion correction during verification

The first consolidated PowerShell run reported 36 passing checks and two failures. Follow-up diagnostics established that:

- the refund action existed with `tool = initiate_refund` and `status = EXECUTED`; the simulated ledger showed refund ID `RF-9001` for ₹1,249;
- the synthetic response reports the processed count in field `n`, not `total`.

The two corrected assertions were rerun individually and both passed. The failure was in the verification script's field/timing assumptions, not a missing refund in the observed diagnostic case.

## 10. Reproduction commands

Run from the repository root while the server is available on the configured port:

```powershell
# Regression suite
.\.venv\Scripts\python -m pytest -q

# Health check
Invoke-RestMethod http://127.0.0.1:8001/api/health

# Scenario evaluation
Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/evals/run

# Synthetic replay
Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/evals/synthetic `
  -ContentType 'application/json' `
  -Body '{"n":300,"noise":0.5,"seed":11}'

# Intent, rigor and benchmark
Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/ai/intent-eval
Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/ai/rigor
Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/ai/benchmark
```

For a fresh evidence capture, record the date, OS, Python version, provider mode, machine details, exact command and full JSON response. Keep credentials out of logs and screenshots.
