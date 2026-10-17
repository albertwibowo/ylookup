# YLookup — DIU CLI workflow builder

Minimal Click CLI that **builds** a reusable workflow from an input workbook + target DIU (+ bank PDF), **updates** that workflow when sheets/columns are renamed or reordered, then **runs** it to fill the **DIU tab only**.

Workflows store **intent + validators + semantic hints** (sheet/header synonyms and column aliases), not Excel cell addresses.

```bash
ylookup --version   # package version
ylookup --help      # build | update | run
```

## Setup

```bash
uv sync
uv pip install -e .
cp .env.example .env   # add OPENROUTER_API_KEY if you want LLM propose/patch/adapt
```

Requires Python ≥ 3.13. Without an API key, `build` / `update` still work using heuristic defaults (sufficient for the sample capital-call files).

When `OPENROUTER_API_KEY` is set, LLM reasoning tokens (if the model returns them) are streamed to **stderr** during `build` (propose/patch) and `update` (adapt/patch).

## Commands

### Build (learn workflow)

```bash
ylookup build \
  --input data/import-capital-call-notice-diu-input.xlsx \
  --pdf data/BS-Test-4.pdf \
  --target data/import-capital-call-notice-diu-output.xlsx \
  --out workflows/capital_call_diu.json
```

Options:

| Flag | Required | Description |
|------|----------|-------------|
| `--input` | yes | Source workbook |
| `--pdf` | yes | Bank statement PDF |
| `--target` | yes | Target workbook with expected DIU |
| `--out` | yes | Path for the saved workflow JSON |
| `--context` | no | Optional free-text file passed to the LLM propose step |
| `--non-interactive` | no | On ambiguity, print questions and exit `2` (no prompts) |

- May ask clarifying questions in the terminal (money / identity / account ambiguity).
- Caps: `MAX_ASK_ROUNDS=3`, `MAX_QUESTIONS_PER_ROUND=5`, `MAX_BUILD_ITERS=3`.
- Question ids are never repeated; answers are stored under workflow `clarifications` / `aliases`.

### Update (adapt workflow to perturbed input)

Stress-test scenario: sheet tabs and/or column headers were renamed or reordered. Seed from an existing workflow, remap bindings via semantic hints, re-validate against the target DIU, and save an adapted workflow (or stop with a precise unresolved explanation).

```bash
ylookup update \
  --workflow workflows/capital_call_diu.json \
  --input data/import-capital-call-notice-diu-input.xlsx \
  --pdf data/BS-Test-4.pdf \
  --target data/import-capital-call-notice-diu-output.xlsx \
  --out workflows/capital_call_diu_updated.json
```

Options: same as `build`, plus required `--workflow` (existing workflow to adapt). `--context` is fed to the LLM adapt step.

- Preserves `intent`, `steps`, and `validators`; rewrites bindings (`sheet_hints`, `header_hints`, `column_aliases`, `resolved_sheet`).
- Clarifications are **additive**: valid prior answers are kept; new `bind:*` / `col:*` / `match:*` answers are upserted as new ambiguities appear.
- Same caps and exit codes as `build`.

### Run (apply workflow)

```bash
ylookup run \
  --workflow workflows/capital_call_diu.json \
  --input data/import-capital-call-notice-diu-input.xlsx \
  --pdf data/BS-Test-4.pdf \
  --out /tmp/diu-filled.xlsx
```

Options: `--workflow`, `--input`, `--pdf`, `--out` (all required). No `--context` / `--non-interactive`.

`run` **never prompts**. It uses clarifications already saved in the workflow. Unresolved runtime ambiguity fails with an explanation (exit `1`). On success it prints the output path and DIU row / investor counts. Only the DIU sheet data rows are written; Staging / Tracker are left unchanged.

## Domain behavior (v1)

- Emit **2 DIU rows per tracker LP** (cash debit + Due From Investors credit).
- Unpaid → amount `0`, blank GL / Effective dates.
- JE Index = tracker order; amounts/dates from matched bank deposits.
- COA lookup: `Cash - Received - USD` → `10000.2`, `Due From Investors` → `18000.1`.
- `Transaction GL Reference` contiguous from `policies.starting_gl_ref` (default 1).

## Model

OpenRouter open-weights under 35B (default): `qwen/qwen2.5-32b-instruct` via `OPENROUTER_MODEL`.

## Hard stop criteria (`build` / `update`)

Stops when any fires:

1. **Success** — normalized DIU matches target  
2. **Max build iterations** (`MAX_BUILD_ITERS`)  
3. **Ask budget exhausted** (`MAX_ASK_ROUNDS`) with ambiguity remaining  
4. **No progress** — DIU score did not improve  
5. **User abort** — empty input / Ctrl-C / `quit`

Exit codes: `0` success, `1` failed, `2` needs input / ambiguous.
