# Architecture

```mermaid
flowchart LR
    subgraph Browser
        U[Operations user]
    end

    subgraph Streamlit["Streamlit app (app.py + ui/)"]
        NAV[st.navigation - 6 pages]
        P1[Executive Overview]
        P2[Catalog Management]
        P3[Catalog Quality Audit]
        P4[Merchant Requests]
        P5[SQL Operations Analytics]
        P6[AI Operations Assistant]
    end

    subgraph Core["catalog_intel/ (pure Python, no Streamlit imports)"]
        VAL[validation.py<br/>CSV validation + import]
        QUA[quality.py<br/>duplicates / price anomalies / images]
        CLS[classifier.py<br/>rules + priority scoring]
        AI[ai.py<br/>optional LLM, strict JSON validation]
        REQ[requests_mgmt.py<br/>lifecycle + SLA state]
        ANA[analytics.py<br/>SQL query registry + reconcile]
        AST[assistant.py<br/>intent router]
        REP[reports.py<br/>CSV / HTML exports]
        SEED[seed.py<br/>synthetic data, reproducible]
    end

    DB[(SQLite<br/>data/catalog.db)]
    LLM[[Optional OpenAI-compatible API<br/>only if OPENAI_API_KEY is set]]

    U --> NAV --> P1 & P2 & P3 & P4 & P5 & P6
    P2 --> VAL --> DB
    P3 --> QUA --> DB
    P4 --> REQ --> DB
    P4 --> CLS
    CLS -. optional .-> AI -.-> LLM
    P1 & P5 --> ANA --> DB
    P1 & P5 --> REP
    P6 --> AST --> ANA
    AST --> QUA
    AST --> CLS
    SEED --> DB
    Streamlit -- "init_demo(): seed if DB empty or older than 3 days" --> SEED
```

## Data model

```mermaid
erDiagram
    merchants ||--o{ products : owns
    merchants ||--o{ merchant_requests : submits
    merchant_requests ||--o{ request_events : "has timeline"
    merchants ||--o{ upload_log : uploads
    merchants { text merchant_id PK
                text tier }
    products { int product_id PK
               text sku
               real price
               text image_url }
    merchant_requests { text request_id PK
                        text request_type
                        text priority
                        text status
                        text due_at
                        text resolved_at
                        int rework_count }
    request_events { int event_id PK
                     text from_status
                     text to_status
                     text created_at }
```

## Request lifecycle

```mermaid
stateDiagram-v2
    [*] --> New
    New --> Triaged
    New --> Rejected
    Triaged --> InProgress: In Progress
    Triaged --> Rejected
    InProgress --> WaitingOnMerchant: Waiting on Merchant
    WaitingOnMerchant --> InProgress
    InProgress --> InReview: In Review
    InReview --> Resolved
    InReview --> InProgress: rework
    Resolved --> Closed
    Resolved --> InProgress: reopen = rework
    Closed --> [*]
    Rejected --> [*]
```

## Key design decisions

| Decision | Reason |
| --- | --- |
| Business logic in `catalog_intel/` with no Streamlit imports | Unit-testable without a UI; UI stays thin. |
| SQLite file, auto-seeded on first start | Zero infrastructure; works on Streamlit Community Cloud. |
| `now` is always a parameter (`:now`) | Deterministic tests and reproducible SQL results. |
| Rules-first classification, LLM optional | The app must work with no external API or secret. LLM output is strictly validated and falls back on any failure. |
| Read-only SQL console (validator + `mode=ro` connection + `query_only`) | Defence in depth: three independent layers prevent writes. |
| Median/MAD z-score on log-price per category | Robust to the outliers it is hunting; symmetric for too-high and too-low prices. |
| Titles with different numbers are never duplicates | Avoids flagging "Pro 500" vs "Pro 510" or "128GB" vs "256GB". |
