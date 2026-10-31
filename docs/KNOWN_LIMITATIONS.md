# Known limitations and production-readiness checklist

This document separates what the prototype demonstrates from what would be required before a real financial-services deployment. A successful local demo is not production approval.

## Current limitations

### 1. Simulated financial systems and data

- `resolve/mockworld/world.py` is the system of record for the demo. It is not a Paytm ledger, UPI switch, settlement service, loan platform, claims platform, or device-management backend.
- Customer, merchant, transaction, dispute, loan, and incident records are synthetic fixtures.
- Refunds, settlement changes, EMI reschedules, and chargeback filing are simulated state transitions. No real payment or account is changed.

### 2. Model and evaluation data

- The local intent corpus is authored for the prototype. Held-out metrics indicate generalisation over this corpus design, not accuracy on production customer language.
- The conformal evaluation reported **85.8% coverage** on the ASR-style slice against a 90% target. Speech-recognition errors are a known risk area.
- Synthetic replay has latent labels and controlled noise. It cannot represent the full distribution, adversarial behaviour, language variation, or operational edge cases of live traffic.
- Human feedback/retraining is a prototype workflow, not a governed model registry or approved production release process.

### 3. Provider and voice behaviour

- Provider adapters and mocked contract tests are present, but an adapter in the repository does not prove a provider is enabled or used in every run.
- Live provider behaviour depends on valid credentials, account entitlements, model availability, rate limits, network conditions, and provider contracts.
- The customer microphone path can use the server-side speech endpoint and browser fallback. A successful browser interaction alone does not prove which path handled the audio.
- Real-time streaming voice, interruption handling, outbound voice-agent calls, and large-scale telephony integration are not implemented as a production service.

### 4. Retrieval and policy governance

- SOP retrieval is lexical (BM25/trigram-style), not a production-grade multilingual vector retrieval and reranking stack.
- The policy engine is Python plus YAML (`policy-v3`), not OPA/Rego. It is Rego-portable in concept, but an OPA deployment has not been implemented or certified.
- Policy amounts, confidence thresholds, reversal TAT, and cost estimates are prototype placeholders. They must be replaced and approved by risk, operations, legal, and compliance owners.

### 5. Runtime, persistence, and security

- The application runs as a local single-process FastAPI service. Case state, event queues, mock world, and workflow execution are in process; restart/recovery and distributed concurrency guarantees are not production-grade.
- The local console/API does not provide production identity, user authentication, role-based access control, tenant isolation, audit administration, or a hardened public deployment boundary.
- Gateway object-level authorization and action controls operate inside the simulated domain. They do not replace production IAM and service-to-service authorization.
- Secrets are loaded from `.env` for local development. Production credentials need a managed secret store, scoped permissions, rotation, and audit.
- No complete threat model, penetration test, privacy impact assessment, disaster-recovery exercise, or independent compliance review is included in this repository.

## Production-readiness checklist

### Integrations and data

- [ ] Replace `mockworld` with service clients for ledger, UPI, settlements, KYC, lending, claims, disputes, devices, and ticketing.
- [ ] Define idempotency, timeout, retry, reconciliation, and compensation contracts for every write.
- [ ] Use sandbox integrations first; prove read-only and shadow-mode behaviour before enabling writes.
- [ ] Replace synthetic SOPs with approved, versioned operational policies and authoritative source citations.

### Security and identity

- [ ] Add enterprise authentication and role-based access control.
- [ ] Bind every action to an authenticated identity and enforce tenant/object authorization at each service boundary.
- [ ] Move credentials and signing keys to a managed KMS/secret manager; rotate keys and define short token lifetimes and scopes.
- [ ] Add rate limits, abuse controls, CSRF/CORS policy, request-size limits, secure headers, dependency scanning, SAST/DAST, and penetration testing.
- [ ] Review PII handling, retention, redaction, encryption at rest/in transit, access logging, and deletion workflows.

### Reliability and operations

- [ ] Move cases and workflow checkpoints to durable storage; support retries, resumability, dead-letter handling, and replay.
- [ ] Use a durable event bus/stream platform for production event ingestion and incident detection.
- [ ] Add structured logs, metrics, distributed traces, alerting, dashboards, SLOs, and on-call runbooks.
- [ ] Run load, soak, burst, failover, dependency-outage, and regional-language quality tests on representative infrastructure.
- [ ] Define backup, restore, disaster recovery, rollout, rollback, and incident-response processes.

### AI governance and risk

- [ ] Collect consented and privacy-reviewed representative tickets across languages, regions, channels, and issue types.
- [ ] Rebuild calibration and conformal evaluation using representative validation sets; close the ASR-style coverage gap.
- [ ] Define per-category precision/recall and minimum evidence thresholds before enabling autonomy.
- [ ] Operate shadow mode and compare recommendations with trained human operators before any write-enabled pilot.
- [ ] Establish human review, escalation ownership, appeal, model/version approval, drift monitoring, and rollback procedures.
- [ ] Red-team prompt injection, data exfiltration, cross-account access, tool abuse, provider failure, and translation loss.

### Legal and compliance

- [ ] Obtain review against applicable RBI/NPCI requirements, DPDP obligations, contractual obligations, and internal policy with Legal/Compliance.
- [ ] Validate customer communication, consent capture, record retention, complaint handling, and turnaround-time logic.
- [ ] Obtain risk-owner approval for every autonomy tier, monetary limit, and prohibited action.

## Suggested rollout

1. **Offline replay:** evaluate against curated, privacy-reviewed historical samples with no live writes.
2. **Shadow mode:** compare recommendations with human operators without customer-visible actions.
3. **Assisted pilot:** enable read-only support and human-approved actions for one narrow category.
4. **Bounded autonomy:** consider reversible, low-value actions only after category-specific quality and operational thresholds are met.
5. **Progressive expansion:** add categories and languages with monitoring, risk review, and rollback criteria.

## Positioning statement

Present Paytm Resolve as a **working research/hackathon prototype demonstrating a safety-oriented architecture**, not as a production payment-support platform. Its strongest claim is the separation between model reasoning and deterministic action control, backed by a mock gateway, policy tiers, verification, auditability, and explicit synthetic evaluation.
