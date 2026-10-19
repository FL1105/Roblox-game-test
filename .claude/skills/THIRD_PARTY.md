# Third-party skills

These project skills were copied from public repositories on 2026-10-19. They were picked from the ranked catalogue at https://skill-issue.in/ and kept only where they are portable, relevant to this repo, and safe to run here. Each source license is in `licenses/`.

Skills are instructions that Claude follows. Treat every file here as code: read a change before you merge it, and do not re-sync from upstream blindly.

## Installed

### `backtesting-frameworks`
- Source: https://github.com/wshobson/agents/tree/main/plugins/quantitative-trading/skills/backtesting-frameworks
- License: MIT
- Local changes: none (byte-for-byte copy).
- Upstream SHA-256 of each file as fetched:
  - `SKILL.md`: `42342d1dc30596384fb31de81364dbc17c1084610b89b8ab945e26729a57d0bf`
  - `references/details.md`: `7c48f2d5fa11026ad50523ef93b5ff3cd01ba8b6a8f60464010bb3fa3923607f`

### `quantitative-research`
- Source: https://github.com/omer-metin/skills-for-antigravity/tree/main/skills/quantitative-research
- License: Apache-2.0
- Local changes: none (byte-for-byte copy).
- Upstream SHA-256 of each file as fetched:
  - `SKILL.md`: `1844cedd18264e9c864a6e56718212e8fd9f8b462d01ef6cf0cf11c7128d75e0`
  - `references/patterns.md`: `18c4abeb331e9e415135b6b3c4b2daf6fc57db9f6867369c267401ae6f079268`
  - `references/sharp_edges.md`: `54e749c04ada25323d31a831f306cb8d9171bb4b7298ef57cb37adb0b89c0adb`
  - `references/validations.md`: `a859cd30d71ba31e3213c7ad699b01282db6e184518fc35710fd120f59271f36`

### `sql-optimization-patterns`
- Source: https://github.com/wshobson/agents/tree/main/plugins/developer-essentials/skills/sql-optimization-patterns
- License: MIT
- Local changes: none (byte-for-byte copy).
- Upstream SHA-256 of each file as fetched:
  - `SKILL.md`: `cfe8234323daaeb47577261c96933f519004fad2f45f1460f340b883af5d7354`
  - `references/details.md`: `e1b8db12016767f08d2ee85791c856c273cb8db0adbe19415e0d1ed6e4a48887`

### `duckdb-query`
- Source: https://github.com/duckdb/duckdb-skills/tree/main/skills/query
- License: MIT
- Local changes: renamed to `duckdb-query`; removed `allowed-tools: Bash` (no blanket shell pre-approval); replaced pointers to uninstalled sibling skills (install-duckdb, duckdb-docs) with plain instructions.
- Upstream SHA-256 of each file as fetched:
  - `SKILL.md`: `c66aedb3a0c6efe45cbc1fdd6b819002da91b5831e13c087552c2f8eeac8ec99`

### `duckdb-read-file`
- Source: https://github.com/duckdb/duckdb-skills/tree/main/skills/read-file
- License: MIT
- Local changes: renamed to `duckdb-read-file`; removed `allowed-tools: Bash` (no blanket shell pre-approval); replaced pointers to uninstalled sibling skills (install-duckdb, duckdb-docs) with plain instructions.
- Upstream SHA-256 of each file as fetched:
  - `SKILL.md`: `ab75a9963471a178e1bc0db581386d6b8fe4de98483b19008d037325d263d729`

### `duckdb-attach-db`
- Source: https://github.com/duckdb/duckdb-skills/tree/main/skills/attach-db
- License: MIT
- Local changes: renamed to `duckdb-attach-db`; removed `allowed-tools: Bash` (no blanket shell pre-approval); replaced pointers to uninstalled sibling skills (install-duckdb, duckdb-docs) with plain instructions.
- Upstream SHA-256 of each file as fetched:
  - `SKILL.md`: `778075c35b56dbb4c2a4974d2377a091f84d476dd13df28c2c546d8e22114af0`

## Why these fit this repo

- `backtesting-frameworks` and `quantitative-research` cover look-ahead bias, overfitting, multiple testing, costs and held-out testing. That is the discipline behind the pre-registered trials in `research/`.
- `duckdb-*` let Claude run SQL straight over the recordings, for example `data/snapshots/*.jsonl.gz`, without loading them into Python first. They need the `duckdb` command-line tool, which is not installed by this repo.
- `sql-optimization-patterns` is a general SQL reference to use alongside DuckDB. It leans toward PostgreSQL and MySQL.

## Considered and held back

| Skill | Why not |
|---|---|
| gmgn-market | Asks the user to paste an API key into chat, depends on a separate third-party CLI, and points on to a swap skill. That conflicts with this repo's rules: keys stay in the environment and nothing sends a transaction. |
| twelvedata, coinglass | Written for the Starchild agent platform (platform-injected keys and a bundled `exports.py` at a platform path). Stocks, forex and BTC/ETH derivatives, not Solana tokens. |
| backtest-vectorbt | Built around the OpenAlgo broker API for Indian markets, with templates and a sibling skill that were not part of the download. |
| firecrawl-build-scrape | Needs a hosted Firecrawl account and API key. Ingest here is a websocket feed. |
| neon-postgres, postgres-read-only | No Postgres in this repo. The second also needs a bundled script and plain-text passwords in a config file. |
| dbt-agent-skills | No dbt project here, and the package could not be enumerated from raw files. |
| xlsx | Already enabled in the Claude account. |
| csv-data-summarizer | Tells Claude never to ask the user a question, and needs pandas, matplotlib, seaborn and a bundled script. |
| ai-research-explore, ai-research-reproduction | A deep-learning reproduction toolchain with orchestration scripts and many sibling skills. |
| autoresearch | An autonomous keep-or-discard loop against a metric. Judgment call: on small samples that pattern finds noise, which is what the held-out tests here guard against. Revisit if you add a sealed evaluation it cannot see. |
| longbridge-quant | Steers all data recommendations to one vendor's platform. |
| stock-analysis (claude-office-skills) | A stock-report template for US, China A-share and Hong Kong equities. |
| claude-data-analysis | Not a skill. It is a project scaffold of agents and commands. |
