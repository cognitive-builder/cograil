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

Today, routing and redaction call the small tier. Extraction and compression have no call site in
the runtime yet, so the setting is ready but nothing uses it.

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
   uv run cograil run workspaces/example-smb --protocol leave_request --as alice@example.com
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
