# Skin Research implementation

Approved requirements: PRD.md and source registry; subsequent conversation takes precedence.

- [x] PostgreSQL additive schema: source provenance, facts with quotes, versioned conclusions, answers, daily digests and per-recipient delivery (live migration pending).
- [x] API/RSS collection, full/abstract distinction, manual ingestion and configurable source schedules; complex sources disabled (live connector tests pending).
- [x] Model stages: triage, extraction, verification, synthesis; explicit evidence limitations, no generated evidence scores; conservative spending reservations.
- [x] Question answering with source references and supplemental search; daily digest, including news without changed scientific conclusions.
- [x] Telegram-first questions, commands and daily digests; durable private-chat queue and per-part delivery. Web dashboard optional, absent from default deployment.
- [x] 46 passing offline/configuration/web tests and independent code review; 5 live PostgreSQL tests skipped in latest run. External validation requirements documented in STATUS.md.

Architecture retains collector → PostgreSQL → worker → Telegram; web is an optional administrator tool. Python 3.12+, psycopg, pypdf, standard-library HTTP. Source originals are retained in upstream/ for comparison; production code lives at project root. No live external messages are sent during development tests.
