# Paytm Resolve — five-minute demo guide

## Before the demo

1. Start with `.\run.ps1` in Windows PowerShell or `./run.sh` on macOS/Linux.
2. Confirm active providers at `/api/config`. Do not assume an external model is active because its adapter exists.
3. Open the operations console and customer app in separate browser tabs/windows.
4. Click **Reset world** before each clean demo run.
5. Keep DevTools Console open during rehearsal. Use synthetic demo identities only.

## 0:00 — Hook (20 seconds)

> “Payment support has two jobs: understand what happened and safely make the right change. Paytm Resolve connects those steps. It investigates the case, checks the evidence, applies policy, verifies the result, and hands control to a person when risk or consent requires it.”

Show the customer app and operations console.

## 0:20 — Hero 1: held settlement (60 seconds)

- In **Live ops**, run **Held settlement, name mismatch (Hindi)** (`settlement_name_mismatch`).
- Show the case trace: evidence reads, root-cause diagnosis, dry-run diff, critic checks, policy tier, gateway writes, verifier result, and Hindi reply.
- Open **Systems** and show settlement `S-5001` moved to `SETTLED` in the simulated world.

Say: “The model does not directly release the settlement. The playbook proposes a typed action; policy chooses the tier; the gateway checks the exact arguments and requester; the verifier confirms the simulated state change.”

Optional voice beat: use the customer app with Hindi selected. Read the transcript before relying on it. If speech recognition changes a critical word such as “not” or “stuck,” retry or use the deterministic scenario/typed input. Do not present an incorrect transcript as successful recognition.

## 1:20 — Hero 2: human approval (45 seconds)

- Run **Chargeback: evidence pack, human approves filing** (`chargeback`).
- Show that the proposed filing pauses at `AWAITING_APPROVAL`.
- Show the evidence pack, then click **Approve and execute**.
- Show the resulting state and audit trace.

Say: “The system can assemble evidence, but this dispute action waits for an operator.”

## 2:05 — Hero 3: red-team paths (60 seconds)

- Run **Red team: prompt injection inside a real complaint** (`prompt_injection`).
- Show the injection flag, stripped attacker-supplied destination/amount, and absence of an executed refund while approval is pending.
- Run **Red team: asks about someone else's transaction** (`cross_account`).
- Show the authorization denial in the trace.

Say: “The security decision is enforced by the guard and gateway, not by asking the model to behave.”

## 3:05 — Hero 4: customer consent (30 seconds)

- Run **EMI help with consent (Hinglish)** (`emi_reschedule`).
- Show `AWAITING_CUSTOMER` before consent.
- Submit “haan kar do” and show the resulting reschedule in the mock loan record.

Say: “No consent, no reschedule.”

## 3:35 — Watchtower (40 seconds)

- Open **Watchtower** and run **Simulate issuer outage**.
- Show the incident, detected failure cluster, affected transactions, notification/workload metrics, and detector comparison.

Say: “This is a simulated stream, but it demonstrates how a burst of failures can be grouped into an incident instead of treated as unrelated tickets.”

## 4:15 — AI stack and evidence (45 seconds)

- Open **AI stack** and show provider status and language matrix.
- Run **Intent evaluation** and point to the ensemble misroute result from the synthetic evaluation.
- Run the **Rigor suite** and explicitly mention that ASR-style conformal coverage is below target in this prototype.
- Run **Benchmark** and state the on-screen measurement with its single-process/mock-system qualification.

Say: “We measure the behaviour, including where it does not meet the target. The next engineering task is representative real-world evaluation, especially speech-recognition shift.”

## 5:00 — Close

> “The idea is not an AI that can do anything. It is an AI teammate that gathers evidence and proposes the next step, while policy, authorization, and verification control what can actually happen.”

## Judge / interviewer Q&A

**Can the LLM move money?**  
No. The LLM is not the gateway. Writes are typed actions selected from application playbooks, dry-run, checked, tiered by policy, and enforced by the gateway. The current world is simulated.

**Is this connected to Paytm production?**  
No. It uses a mock world and synthetic data. Real ledger/UPI/settlement integrations are a production migration step.

**What happens when input is malicious?**  
The guard flags selected injection/scam patterns before planning; policy can raise the tier; the gateway enforces object-level access and exact-argument tokens. These controls still require red-team and security testing before production.

**How accurate is it?**  
The scenario suite and synthetic evaluations passed in the recorded local run. Those results are on authored synthetic data, not live Paytm tickets. The ASR-style conformal coverage result was 85.8%, below the 90% target.

**Does it use OPA?**  
No. The prototype uses a Python policy engine and YAML rules labelled `policy-v3`. OPA has not been deployed.

**Which model handles each request?**  
The router supports local, Sarvam, OpenAI-compatible, and Claude adapters. Active providers depend on runtime configuration; show `/api/config` or the AI overview rather than guessing.

**What would you ship first?**  
A privacy-reviewed shadow-mode pilot on historical tickets, then a narrow assisted workflow. Consider bounded autonomy only after category-specific precision, security, legal, and risk-owner approval.

## Demo discipline

- Say **simulated** whenever showing money movement or a system-of-record change.
- Say **synthetic evaluation** when quoting accuracy, scenario, or noise metrics.
- Do not call the benchmark a production SLA.
- Do not imply a provider was active unless runtime configuration confirms it.
- Never show `.env`, API credentials, real customer information, or the development gate secret.
