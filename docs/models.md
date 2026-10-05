# Models, Tiers and Ollama

A Step asks for a tier, never for a model. The workspace's `harness.yaml` says which model each tier
means. See [ADR 0010](adr/0010-model-tiers-small-first.md) for why. This page shows how to choose a
tier, set effort, handle low confidence, pick a provider and run local models with Ollama.

## Tiers

There are three tiers: `small`, `standard` and `strong`. A Step gets its tier from the first of
these that is set:

1. The Step's own directive: `(model: small)`, `(model: standard)` or `(model: strong)`.
2. The Protocol's `model_tier`.
3. The Colleague's `default_tier`. It is a key in the colleague YAML and defaults to `standard`.

`default_tier` replaces the old `model_policy` key. `model_policy` is no longer accepted, and a
colleague file that still has it is an error.

```markdown
2. Step "Classify": Decide which kind of leave this is. (model: small)
```

`harness.yaml` maps each tier to a model name. Concrete model names live only there, never in a
Protocol, a colleague file or the code.

- `tiers` is the Anthropic mapping. The defaults are `claude-haiku-4-5` for small,
  `claude-sonnet-5-5` for standard and `claude-opus-5-5` for strong. Haiku 4.5 is listed for
  retirement no sooner than 2026-10-15, so check the current model list before you rely on it.
- `provider` picks the provider. It is `anthropic` by default, or `ollama`.
- `providers` holds the mapping for the other providers. All three tiers are required in each one.
  Selecting `ollama` without a mapping here is an error.

Some work defaults to the small tier: classification (routing), extraction, redaction and
compression. They use `defaults.classification_tier`, which is `small` by default. All other work
uses `defaults.judgment_tier`.

Today, routing, redaction and compression call the small tier. Extraction has no call site in
the runtime yet, so the setting is ready but nothing uses it.

### Compression of long Tool results

A Tool result longer than `context.compression_threshold_tokens` (2000 by default, estimated at
four characters a token) is summarised by the compression tier before the model sees it. The
Step's own instruction guides the summary, so it keeps what the Step may still need. The raw
result stays in the Step's `tool_calls` on the Run, with the summary beside it as `compressed`,
for audit and the run history. A later Step that declares this Step's output sees the summary
too. Each compression writes a `context.compressed` AuditEvent, and its tokens and cost count
against the Step and the Run. If the compression fails, the Run fails closed; the raw result is
never passed on instead. `cograil runs <id> --ledger` adds `raw_tokens` and `compressed` columns
when a Run compressed anything; the source columns count what the model was given.

### Saved context

The parts of a prompt that do not change between calls are sent first and marked for caching, so
repeated calls pay the cache-read rate (ADR 0013). A call's prompt is ordered from the most
stable part to the least: the Step's tool schemas, then the Run's prefix (the Colleague's persona
and the Protocol's text: its steps, error handling and guardrails), then the Step's own
instruction and the per-run data (prior outputs and Tool results). The prefix is built from
the Colleague and the Protocol only, so every Step of a Run sends it unchanged.

The Anthropic provider puts a cache marker on the prefix. The Ollama provider has no saved
context and sends the prefix as the start of its system message. A Step's tool schemas come
before the prefix and differ between Steps with different whitelists, so a cache hit across
Steps needs the same tools; within a Step, every turn after the first can hit.

The provider reports cache read and cache write tokens for each call, and the runner adds them
to the Step's row of the Window Ledger. `cograil runs <id> --ledger` adds `cache_read` and
`cache_write` columns when a Run used any. They are not part of `input_tokens`. They count
against `token_budget_per_step`, and they cost 0.1 (read) and 1.25 (write) times the model's
input price in `usd_budget_per_run`.

## Cost telemetry

Every model call made for a Run adds its dollars to the Run's `cost_usd` and its tokens to the
Run's tally, which `GET /runs` and `GET /runs/{id}` show as `usage`. That is a Step's turn, a
compression, an injection screen, the small tier's redaction of error text, and the routing call
that started the Run from a chat message. A refused message starts no Run, so its routing call is
charged to none. A Run that fails or escalates in the middle of a Step keeps the spend of every
call made before it stopped, and `usd_budget_per_run` counts all of these calls. That includes
the call that breached the bound. A model the provider reports under an id that has no `pricing`
entry — the harness prices every tier model under a dollar budget, so this is an id the provider
renamed — has no dollars to count. An aside call on such a model (a redaction, the routing)
counts its tokens at $0 with a `cost.unpriced` warning, and `usd_budget_per_run` cannot see its
dollars. A Step's own call on one breaches the bound and escalates the Run, with the call's
tokens on its tally at $0. The tally keeps four kinds apart, because they are billed at
different rates: fresh `input_tokens` and `output_tokens`, `cache_read_tokens` and
`cache_write_tokens`, and `batch_tokens` (all the tokens of a call that went through the
provider's batch path).

`cograil runs --cost` prints one row per Protocol over the newest `--limit` Runs (default 20):
the Runs, how many resolved, the tokens by kind, the cost, and the headline number of ADR 0013,
cost per resolved run: the total cost of the Runs that ended `completed`, divided by their count
(`-` when none did). A Run is resolved whenever it ends `completed`, including one that completed
after handing off to a human inside its Protocol. A Run that ended `escalated` or `failed` still
counts in the cost column.

## Effort

Effort tells a model how hard to think. A Step's effort is the first of these that is set:

1. The Step's directive: `(effort: low)`, `(effort: medium)` or `(effort: high)`.
2. `defaults.effort` in `harness.yaml`. It is unset by default, so the provider's own default
   applies.

The Anthropic provider sends effort as the API's `output_config.effort`, and only when it is set.
Ollama ignores it. Only set effort for models that support it.

Ultracode is a Claude Code setting for build sessions. Cograil does not use it at runtime.

## Escalation

At the end of a Step, the model sends the structured `step_complete` signal. The signal carries a
`confidence` from 0 to 1.

- If a small-tier `step_complete` has a confidence below `defaults.min_confidence` (0.7 by
  default), the runner discards it before anything in it runs. It then asks for the same turn again
  on the standard tier.
- A `tier.escalated` AuditEvent records the `step`, the `from_tier`, the `to_tier` and the
  `confidence`. It carries the Run's principal.
- A result with no confidence stands.
- Only the small tier escalates. The higher tier is used for that one turn only. The next turn
  starts on the Step's own tier again.
- Escalated calls count toward the same loop bounds and the same budget as any other call. See
  "How a runbook runs" in [Writing a runbook](runbooks.md).

## Providers

Cograil has one `Provider` interface (see [ADR 0005](adr/0005-provider-interface-anthropic-default.md)).
Anthropic is the default. Ollama is the second provider, for local models. Both read the tier
mapping from `harness.yaml`, so a Protocol does not change when you switch.

- `anthropic` needs `ANTHROPIC_API_KEY`.
- `ollama` needs no API key. It reads the server address from `OLLAMA_HOST`, which defaults to
  `http://localhost:11434`.

If you set a dollar budget (`loop.usd_budget_per_run`), every tier model needs a `pricing` entry.
For local models, use 0.

Local models must support tool calling. A model that cannot call tools cannot follow a Step.

## Ollama on a Laptop

Ollama runs models on your own machine. These steps were not run in CI.

1. Install Ollama from [ollama.com](https://ollama.com).
2. Install the Cograil extra. It adds the `ollama` Python package (MIT licence):

   ```bash
   pip install 'cograil[ollama]'
   ```

3. Pull a small model:

   ```bash
   ollama pull gemma3:4b
   ```

4. Set the provider in the workspace's `harness.yaml`. Every tier needs a model, so pull the others
   too, or point all three at models you have:

   ```yaml
   version: 1.0.0
   provider: ollama
   providers:
     ollama:
       small: gemma3:4b
       standard: qwen3:14b
       strong: qwen3:32b
   loop:
     usd_budget_per_run: 0.50
   pricing:  # local models cost nothing, but a dollar budget still needs a price
     gemma3:4b: {input_per_mtok: 0, output_per_mtok: 0}
     qwen3:14b: {input_per_mtok: 0, output_per_mtok: 0}
     qwen3:32b: {input_per_mtok: 0, output_per_mtok: 0}
   ```

5. Run a Protocol:

   ```bash
   uv run cograil run workspaces/example-smb --protocol leave_request --as alice@example.com --message "Annual leave from 2026-11-02 to 2026-11-04, please"
   ```

A laptop usually fits only the small and perhaps the standard model. Use `(model: ...)` directives
and `default_tier` so that the heavy Steps do not ask for a model you cannot run.

## Ollama on a DGX Spark

The NVIDIA DGX Spark is a small desktop machine with 128 GB of unified memory. A larger model such
as `qwen3:32b` fits there, so it can serve as the standard or strong tier. These steps were not run
in CI.

1. Run Ollama on the Spark and pull the models in your `providers.ollama` mapping.
2. On the machine that runs Cograil, point at the Spark:

   ```bash
   export OLLAMA_HOST=http://<spark-host>:11434
   ```

3. Use the same `harness.yaml` as in the laptop section, and run the same command.
4. Keep the Ollama port on the client's network. The server has no login, so do not expose
   port 11434 to the internet.

## What To Expect From Small Models

A small model is cheaper and faster, and for some tasks it is good enough. For others it is not.
Cograil makes no general claim about this. Quality for a small model is shown per task, with an
eval (a golden set of cases scored against expected results). Run `cograil eval` on your own
Protocols before you move a Step down a tier.
