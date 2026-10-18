# Triad CLI

**Universal Multi-Model Peer Review & Architectural Consultation CLI**

Triad brings automated, multi-model code review and architectural consultation to **any project in any programming language** (Python, Rust, TypeScript, Go, Java, C++, etc.).

Instead of relying on a single AI model's self-critique, Triad prompts multiple independent models concurrently (e.g. via OpenAI-compatible gateways or direct providers), synthesizing their critiques, scores, and verdicts into actionable reports.

---

## Installation

Install Triad as a standalone CLI tool in your Python environment:

```bash
# Editable install from local clone
git clone https://github.com/anixes/triad.git
cd triad
pip install -e .

# Or direct install via pip
pip install git+https://github.com/anixes/triad.git
```

Once installed, the `triad` command is globally available in your terminal.

---

## Setup & Configuration

Triad requires an API key and a list of models to consult. Configuration is resolved hierarchically:

1. **CLI Flags** (`--models`, `--api-key`, `--gateway-url`, etc.) [Highest Priority]
2. **Environment Variables** (`TRIAD_API_KEY`, `TRIAD_MODELS`, `TRIAD_GATEWAY_URL` — standard 12-factor / CI support)
3. **Project Local Config** (`.triad.env` or `.env` in the current project)
4. **User Global Config** (`~/.triad/config.env` — recommended so it works anywhere on your system)

### Initialize Configuration

#### Global Setup (Works anywhere on your system):
```bash
triad init --global
```
This creates `~/.triad/config.env`. Open it and fill in your gateway key and models:
```env
TRIAD_GATEWAY_URL=https://gateway.constellationgate.ai/v1
TRIAD_API_KEY=your-api-key-here
TRIAD_MODELS=provider/model-1,provider/model-2
```

#### Project-Local Setup:
```bash
triad init
```
This creates `.triad.env` in the current working directory.

---

## Usage

### 1. Multi-Model Code Review (`triad review`)

Run Triad inside any git repository:

```bash
# Review unstaged changes in the current repo
triad review

# Review staged changes (--cached)
triad review --target staged

# Review changes against a base branch
triad review --target origin/main...HEAD

# Include specific unit test files for contextual evaluation
triad review --tests tests/unit/test_auth.py tests/unit/test_api.py

# Review changes in a different directory
triad review --repo /path/to/another/repo --target staged

# Strict CI Gate: fail build if any model rejects or score < 8.0
triad review --strict --min-score 8.0
```

### 2. Multi-Model Architectural Consultation (`triad consult`)

Consult multiple models on system design or refactoring before writing code:

```bash
# Ask an architectural question
triad consult --prompt "How to design a durable background task engine with idempotency?"

# Consult on a written design document or RFC specification
triad consult --spec docs/rfc-001-architecture.md --prompt "Evaluate failure modes and security risks."
```

### 3. Multi-Model Deliberation & Debate (`--debate`)

By default, Triad runs in **blind peer review** mode (models generate their reviews independently without seeing each other's outputs, eliminating anchoring bias and groupthink).

When you want the models to **actively challenge, cross-critique, and refine each other's findings**, pass `--debate`:

```bash
# Run a 2-round architectural debate (Round 1: blind, Round 2: cross-critique & synthesis)
triad consult --prompt "Design consensus engine" --debate

# Run multi-round code review debate with calibrated scoring
triad review --target staged --debate --debate-rounds 2
```

In Debate Mode:
- **Round 1 (Independent Assessment):** Each model analyzes the task in isolation.
- **Round 2 (Cross-Critique & Synthesis):** Each model receives the peer models' Round 1 proposals, critiques flaws and oversights, adopts superior recommendations, and delivers a battle-tested, refined verdict.
- **Reporting:** Reports track score and verdict progression across rounds (e.g., `8.0 → 9.0/10`) with full transcripts.

---

## Output & Reports

Triad produces structured Markdown and JSON reports:
- Markdown summary table and individual analyses: `reports/triad/consolidated_review_report.md`
- Machine-readable JSON for CI integration: `reports/triad/consolidated_review_report.json`
- Full individual model output logs: `reports/triad/{model_name}_{mode}.md`

---

## CI / CD Integration (GitHub Actions Example)

Add Triad to your pull request workflow (`.github/workflows/triad-review.yml`):

```yaml
name: Triad Multi-Model Peer Review

on: [pull_request]

jobs:
  peer-review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install Triad
        run: pip install ./packages/triad

      - name: Run Strict Peer Review
        env:
          TRIAD_API_KEY: ${{ secrets.GATE_API_KEY }}
          TRIAD_MODELS: ${{ vars.TRIAD_MODELS }}
        run: triad review --target origin/${{ github.base_ref }}...HEAD --strict --min-score 7.5
```
