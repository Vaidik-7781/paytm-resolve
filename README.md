# Paytm Resolve

**An evidence-led, policy-governed AI support teammate for payment and merchant operations.**

Paytm Resolve is a local hackathon/demo prototype. It takes a support message, investigates relevant records, proposes an action, checks that action against policy, executes only through a guarded tool gateway, verifies the result, and responds to the customer. When evidence is weak, consent is missing, or risk is high, it pauses for approval or escalates to a human.

> **Prototype boundary:** the repository uses a simulated Paytm-like world and synthetic records. It is not connected to Paytm production systems and must not be used to move real money or change real customer accounts.

## Why it is interesting

- **Evidence before action:** the workflow retrieves operating procedures and reads simulated transaction/account state before planning a write.
- **Models are not the authority:** actions come from typed application playbooks, are dry-run first, checked by a critic and policy, and executed through the gateway.
- **Risk-based autonomy:** policy tiers separate observe/ask, reversible action, human approval, and human-only escalation.
- **Verifiable writes:** gateway checks include schemas, exact-argument gate tokens, requester-level object authorization, value caps, idempotency, and undo/compensation support.
- **Indian-language experience:** English, Hindi and Hinglish have a local intent path; optional Sarvam integrations provide Indic language, translation, speech and response capabilities when configured.
- **Operational visibility:** the console shows case traces, policy decisions, approvals, incidents, evaluation results and benchmarks.
- **Evaluation beyond a happy path:** tests cover tool safety, scenarios, synthetic noise, model disagreement, selective prediction and audit integrity.

## Interfaces

| Interface | Local URL (default) | Purpose |
|---|---|---|
| Operations console | `http://127.0.0.1:8001/` | Live ops, Watchtower, Autonomy & policy, AI stack, Evals, Systems |
| Customer app | `http://127.0.0.1:8001/customer` | Customer-facing text/voice support demo |
| Health check | `http://127.0.0.1:8001/api/health` | Local service health |
| API reference | `http://127.0.0.1:8001/docs` | FastAPI-generated endpoint docs |

The supplied run scripts default to port **8001**. Set `PORT` before starting to use another free port.

## Quick start

### Windows PowerShell

```powershell
cd path\to\paytm-resolve
.\run.ps1
```

### macOS / Linux

```bash
cd path/to/paytm-resolve
chmod +x ./run.sh
./run.sh
```

The scripts create `.venv` if needed, install runtime dependencies, copy `.env.example` to `.env` if no `.env` exists, and start the local server. Keep the terminal open while using the browser.

### Run tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The verification run recorded on **31 October 2026** completed **33 tests successfully**. See [Test results](docs/TEST_RESULTS.md) for details and caveats.

## Configuration

The application runs with the local intent model, deterministic rules and grounded response templates. Provider credentials are optional. To configure one or more providers, copy `.env.example` to `.env`, add only the credentials you intend to use, and restart the server.

| Variable | Purpose | Default / behaviour |
|---|---|---|
| `LLM_MODE` | Provider routing | `auto`; `offline` forces the local path |
| `SARVAM_API_KEY` | Enables Sarvam chat/voice/translation adapters | Empty means unavailable |
| `OPENAI_COMPAT_BASE_URL` | OpenAI-compatible endpoint | Empty means unavailable |
| `OPENAI_COMPAT_MODEL` | Model name for that endpoint | Empty means unavailable |
| `OPENAI_COMPAT_API_KEY` | Optional compatible-provider credential | Empty if not needed |
| `ANTHROPIC_API_KEY` | Optional Claude adapter | Empty means disabled |
| `RESOLVE_PACE` | Delay between streamed trace steps | `0.45` seconds |
| `PORT` | Port used by the run scripts | `8001` |
| `RESOLVE_GATE_SECRET` | Development signing secret for gateway tokens | Development-only fallback; replace outside local demos |

Never commit `.env`, paste credentials into issues, or include keys in screenshots or ZIP files. `.env.example` is the safe template.

## How a case moves through the system

1. **Intake and guard:** detect language, inspect risky instructions, and tokenize sensitive values before model processing.
2. **Intent and evidence:** combine the local classifier and rules with optional provider votes; retrieve relevant SOP content and read simulated records.
3. **Diagnosis and planning:** specialist agents produce structured findings; the planner selects a known playbook action rather than executing free-form model output.
4. **Dry run and critic:** calculate a proposed state diff and check authorization, evidence, policy citations, injected parameters and value bounds.
5. **Policy decision:** assign an autonomy tier using versioned Python/YAML policy-as-code.
6. **Gateway execution:** validate the tool schema and gate token, enforce object-level authorization and limits, apply idempotency, and record compensation where supported.
7. **Verification and communication:** re-read the simulated system of record, verify the effect, write an audit event and compose a grounded customer response.
8. **Human handoff:** pause for approval, customer consent or escalation when policy requires it.

See [Architecture](docs/ARCHITECTURE.md) and [AI design](docs/AI_DESIGN.md).

## Project structure

```text
paytm-resolve/
├── resolve/
│   ├── agents/       # Supervisor, specialists, investigator
│   ├── api/          # FastAPI routes and static app serving
│   ├── evals/        # Scenario suite, synthetic replay, benchmark
│   ├── events/       # SSE hub and issuer incident detector
│   ├── gateway/      # Typed tools, authorization, gate tokens, undo
│   ├── knowledge/    # SOP retrieval
│   ├── llm/          # Local, Sarvam, Claude, compatible-provider adapters
│   ├── memory/       # Episodic recall
│   ├── ml/           # Intent model, calibration, feedback, conformal/selective eval
│   ├── mockworld/    # Simulated ledger and business systems
│   └── policy/       # Policy engine and policy-v3 YAML
├── web/              # Operations console and customer app
├── tests/            # Unit, provider-contract, ML and scenario tests
├── docs/             # Architecture, AI design, demo, test and production docs
├── requirements*.txt
├── run.ps1
└── run.sh
```

## Evaluation snapshot

The 31 October 2026 local verification session reported:

- **33/33** Python tests passed.
- **22/22** scenario evaluations passed; unsafe actions `0`, grounded rate `1.0`, script-match rate `1.0`.
- **300-ticket** synthetic replay at noise `0.5`: decision accuracy `1.0`, overreach `0`, invariant violations `0`, escalation recall `1.0`.
- Intent ensemble misroute rate `0.0` at noise `0`, `0.25` and `0.5` in the recorded evaluation.
- Manual browser checks: five ops tabs loaded without reported red console errors; the sample XSS input rendered as text without an alert; the Hindi microphone flow returned a Hindi response.

These are **prototype results on authored synthetic data and local mock systems**, not production accuracy or payment-network performance claims. The rigor evaluation also found a conformal-coverage shortfall under ASR-style errors; see [Test results](docs/TEST_RESULTS.md).

## Documentation

- [Architecture and workflow](docs/ARCHITECTURE.md)
- [AI design and safety boundaries](docs/AI_DESIGN.md)
- [Test results and benchmark evidence](docs/TEST_RESULTS.md)
- [Known limitations and production-readiness checklist](docs/KNOWN_LIMITATIONS.md)
- [Five-minute demo and judge Q&A](docs/DEMO_SCRIPT.md)
- [Prototype-to-production path](docs/PRODUCTION_PATH.md)

## Responsible use and security

- This is a local demo; the API/UI are not a production-authenticated service.
- Do not expose the development server to the public internet.
- Do not connect it to real payment rails or customer accounts.
- Replace mock systems, placeholder policy values, development secrets and in-memory state before a pilot.
- Complete security, privacy, legal and compliance reviews before handling real data.

## Team

**Team DevNex** — Paytm Build for India AI Hackathon, Track 3: Autonomous AI Teammates.

---

*The central design choice: models help reason, while deterministic policy, authorization and verification control what the system is allowed to do.*
