# AI design and safety boundaries

## Design principle

**Models propose; deterministic controls decide and enforce.** A model may help interpret a message, summarise evidence, or draft a hypothesis. It does not receive direct permission to move money or mutate a customer record.

## 1. Guard before model calls

`resolve/guard.py` runs before intent reasoning. It detects selected prompt-injection and scam patterns, cleans untrusted instructions, and tokenizes sensitive values. The guard reduces exposure but is not a complete prompt-injection or data-loss prevention solution. Production use requires layered controls, adversarial testing, and review of the data passed to each provider.

## 2. Intent is an ensemble signal

The baseline combines:

- a trained local classifier using hashed text features and a cross-lingual concept lexicon;
- deterministic keyword/rule signals;
- optional provider votes when enabled by configuration.

The ensemble is implemented in `resolve/ml/ensemble.py`. A provider vote is not the final authority. Disagreement, low confidence, or risk flags can cause a conservative/unknown route and human handoff. See [Test results](TEST_RESULTS.md) for the synthetic intent evaluation and the ASR-style coverage shortfall.

## 3. Diagnosis is evidence-led

The engine uses structured reads, SOP retrieval (`resolve/knowledge/kb.py`), episodic recall, and domain specialists. Root-cause outputs are structured rather than accepted as free-form text. Specialist areas include transaction tracing, settlements, disputes, lending, claims, and merchant devices.

Novel cases may use `resolve/agents/investigator.py`. The investigator is bounded (default maximum six steps) and uses read-only tools. Its output is a hypothesis and recommendations for a human, not an executable write.

## 4. Actions are playbook-defined

The planner selects typed gateway tools. Before any write:

1. The system creates a dry-run state diff.
2. The critic checks authorization, evidence support, policy citation, injected parameters, and amount bounds.
3. The policy engine computes the autonomy tier outside any prompt.
4. The gateway validates the tool schema and exact-argument gate token, then enforces authorization, caps, idempotency, and compensation/undo behaviour where supported.
5. The verifier re-reads the mock system of record before the case is considered resolved.

A good-looking LLM response is not proof that an action ran, and an action attempt is not proof that it succeeded. The verifier checks the resulting state.

## 5. Policy-as-code and progressive autonomy

`resolve/policy/policy.yaml` is labelled `policy-v3` and is evaluated by `resolve/policy/engine.py`. It contains placeholder confidence thresholds, per-tool limits, risk categories, and category autonomy modes. The console can change category modes for the demo. All values require risk-owner review before any real deployment.

The policy engine is Python/YAML. It is **not OPA/Rego**; the code is structured so that a future port is possible, but that port has not been implemented.

## 6. Model/provider adapters

`resolve/llm/router.py` chooses providers by task and records usage/fallbacks. Available adapters include:

- local deterministic and trained-model path;
- Sarvam chat/reasoning, translation, STT, and TTS adapters;
- an OpenAI-compatible chat adapter;
- an Anthropic Claude adapter.

Which adapters are active depends on `.env` configuration and `LLM_MODE`. The local path remains the fallback. Use `/api/config` and `/api/ai/overview` to inspect the running configuration. Do not claim a specific cloud model handled a request unless configuration or trace confirms it.

## 7. Faithfulness, audit, and learning

- Customer-facing identifiers and amounts should be grounded in case/system facts; translation/rewrite validation checks that key identifiers and amounts are preserved.
- `resolve/audit.py` builds a hash-linked audit trail and renders an audit pack.
- `resolve/ml/feedback.py` demonstrates a feedback loop: labels are added, a candidate model is retrained, and the update is accepted only if its held-out check does not regress beyond the configured tolerance. A prior model can be restored in the prototype.

These are prototype mechanisms, not an enterprise model-governance or records-management program.

## 8. Incident detection and evaluation

The event detector compares a sliding-window threshold with a one-sided CUSUM. The evaluation harness measures detection delay on simulated bank streams. Scenario tests, synthetic invariant replay, intent evaluation, conformal coverage, selective risk-coverage curves, and local benchmarks are under `resolve/evals/` and `resolve/ml/`.

The rigor run reported **85.8% conformal coverage** on its ASR-style slice against a 90% target. This is an open risk and should be discussed in the demo rather than hidden.

## Non-goals of the current build

- The model is not a payment authorization system.
- This repository does not connect to real Paytm systems or customer data.
- Synthetic accuracy is not a real-world service accuracy guarantee.
- The Python/YAML policy is not a deployed OPA policy bundle.
- The local benchmark is not a production load test or latency SLO.
