# From prototype to production

The current repository is a single-process prototype with synthetic data and mock systems of record. The table below identifies likely replacements while preserving useful module boundaries. These are target directions, not integrations already delivered.

| Prototype component | Production replacement / work | Contract or boundary to preserve |
|---|---|---|
| `resolve/mockworld/world.py` | Authenticated service clients for ledger, UPI, settlements, KYC, disputes, lending, claims, devices and ticketing | Typed tool contracts behind the gateway; no direct model-to-service access |
| In-memory `Engine` cases | Durable case store and workflow engine with checkpoints, retries, timers and replay | Case lifecycle and event contracts |
| `resolve/policy/policy.yaml` + Python engine | Risk-owned policy repository; consider OPA/Rego after formal rule translation and tests | `PolicyInput -> PolicyDecision` boundary and versioned decisions |
| `resolve/knowledge/kb.py` | Approved SOP corpus with multilingual retrieval, vector search, reranking, source versioning and citation validation | Retrieval interface plus source provenance |
| Synthetic corpus and local intent model | Privacy-reviewed labelled corpus; per-language calibration, drift checks and model registry | Intent prediction interface and selective fallback |
| In-process event hub/detector | Durable event bus/stream platform plus stateful incident detector | Incident detector input/output contract |
| `.env` gate secret | Managed KMS/secret manager, key rotation, short-lived scoped tokens | Signed action authorization contract |
| Local metrics and traces | Central observability, SLOs, alerts, trace correlation and on-call runbooks | Structured telemetry and case correlation IDs |
| Local browser app | Authenticated, accessible, tested internal/customer applications with role controls | API contracts and explicit consent/approval transitions |

## Recommended rollout

1. **Read-only integration:** connect sandbox/read-only systems and validate identity, data correctness and evidence provenance.
2. **Offline replay:** run against privacy-reviewed historical cases with no customer-visible messages and no writes.
3. **Shadow mode:** compare recommendations with trained human operators; report precision, recall, abstention and disagreement by category and language.
4. **Assisted pilot:** enable a small number of actions that always require human approval.
5. **Bounded autonomy:** consider low-risk, reversible actions only after risk owners approve category-specific quality and operational thresholds.
6. **Progressive expansion:** add categories and languages with monitoring, rollback and incident-response criteria.

## Governance required before a pilot

- Replace every placeholder monetary limit, confidence threshold and turnaround-time assumption with approved policy.
- Validate workflows against applicable RBI/NPCI requirements, DPDP obligations, contractual terms and internal policy with Legal/Compliance.
- Establish a formal human escalation path and service ownership.
- Complete threat modelling, red-team testing, privacy review, penetration testing, provider-risk review and data-retention design.
- Define model release governance, calibration thresholds, drift monitoring, audit retention, rollback and incident response.
- Measure unit economics with actual provider usage and approved pricing; current UI costs are illustrative only.

## Preserve the central design boundary

The most valuable architectural principle to carry forward is that model output remains advisory. Deterministic policy, identity/access checks, typed tool validation, idempotency and post-action verification must remain independent of the model provider and prompt content.
