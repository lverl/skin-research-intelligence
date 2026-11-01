# Skin Research implementation

Approved requirements: PRD.md and source registry; subsequent conversation takes precedence.

- [x] PostgreSQL additive schema: source provenance, facts with quotes, versioned conclusions, answers, daily digests and per-recipient delivery (live migration pending).
- [x] API/RSS collection, full/abstract distinction, manual ingestion and configurable source schedules; complex sources disabled (live connector tests pending).
- [x] Model stages: triage, extraction, verification, synthesis; explicit evidence limitations, no generated evidence scores; conservative spending reservations.
- [x] Question answering with source references and supplemental search; daily digest, including news without changed scientific conclusions.
- [x] Authenticated Russian web dashboard, facts/history/source settings; Telegram delivery configured by owner, no live sends during development.
- [x] 29 offline tests and independent code review; live PostgreSQL, model and source validation requirements documented in STATUS.md.

Architecture retains collector → PostgreSQL → worker → web/bot. Python 3.12+, psycopg, pypdf, standard-library HTTP. Source originals are retained in upstream/ for comparison; production code lives at project root. No deployment or external messaging is authorized by this implementation task.
