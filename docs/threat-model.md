# Threat Model

This page lists threats to a Cograil Run and the defences against them. It grows section by section. This first section covers injection: text that is written to look like instructions to the model.

## Injection Through Retrieved Content and Tool Output

### The Threat

A Run reads text it does not control. A knowledge document is cut into Chunks, and a `knowledge` Tool returns them as passages. Any other Tool returns output too: a REST body, an MCP result, or error text. REST is a web API style, and MCP is the Model Context Protocol.

An attacker can plant text in any of these so that it reads like an instruction to the model. Common forms are:

- "Ignore all previous instructions".
- A role marker such as `SYSTEM:`.
- Chat-template tokens.
- A fake `step_complete` signal.
- A polite note to the assistant that asks it to send data, or to change who an action is for.

The goal is to change which Tool is called, change its arguments, skip a Gate, or leak data.

### The Layers

These layers run, in this order, on every batch of Tool results before the model sees them.

1. **Data block.** This layer already existed (ADR 0007). Every Tool result, the Run input and the output of earlier Steps reach the model inside `<data>` blocks. A fixed preamble comes first and says that the content is data, not instructions. The `<` character is escaped inside a block, so text cannot close the block early.

2. **Instruction-stripping filter.** The code is `injection.strip`. It is deterministic and makes no model call. It checks every sentence of every string in a result or an error. A sentence that matches an instruction pattern is replaced by `[removed: instruction-like text]`. The patterns are:
    - An override phrase: ignore, disregard, forget, override or bypass, then a qualifier such as all, previous, your or the, then instructions, rules, prompts, guidelines, steps or context.
    - "you are now".
    - "from now on, you".
    - "new instructions:".
    - "system prompt".
    - A role marker (`system:`, `assistant:` or `developer:`) at the start of a sentence.
    - Chat-template tokens: `<|...|>`, `[INST]` and `<<SYS>>`.
    - The `step_complete` signal.

    The filter is lossy on purpose. A sentence written by a person that matches a pattern is dropped too. Dict keys are not stripped.

3. **Small-tier screen.** The code is `injection.Screen`. It makes one model call for each batch, on the classification tier. That is the small tier by default (ADR 0010). It looks at the stripped outputs that still contain text. An output with no strings, for example only numbers, is not screened. The screen sees the outputs as numbered items in data blocks. It is offered no Tools. It replies with the numbers of the items that hold instructions, or with `none`.

    A flagged output reaches the model as `[withheld: the screen found instructions in this output; the full output is kept on the Run]`. The whole output of that call is withheld, not only the bad passage. If the reply cannot be read, the screen raises `ProviderError` and the Run fails closed. The tokens and cost of the screen count against the Step and the Run.

4. **Compression.** Compression (issue #49) summarises long results. It runs on the screened copy, so stripped or withheld text is not in what it reads. Its summary is written by a model and is not screened again.

5. **Whitelist and Gates.** This is the guarantee that does not depend on the model. The runner enforces the Step whitelist (`ToolNotAllowed`) and the Gates (`confirm_before_write`, which needs an Approval) on every call. This follows rules 2 and 7 in `CLAUDE.md`. An injection that gets past layers 1 to 3 can change what the model asks for. It cannot change what the runner lets run. A gated write waits for a person, and that person sees the actual arguments.

### Audit and Record

The raw output stays in the Step's recorded `tool_calls` on the Run. What the model was given is recorded beside it under `screened`. A later Step that declares this Step's output sees the screened copy, never the raw one.

Each call that the filter changed, or that the screen withheld, writes an AuditEvent of kind `context.screened`. The event carries the principal and this detail: `step`, `tool`, `removed` and `withheld`.

### Evals

`tests/evals/injection.jsonl` holds cases with planted injections. Each case names the layer that is expected to catch it: `filter`, `screen` or `none`.

`tests/evals/test_injection_evals.py` runs the cases offline. It checks that the planted text never reaches the Step's model. It also checks that the tool calls and Approvals of the Run equal those of the clean baseline. A second check covers a model that obeys an injection anyway. That call still meets the whitelist and the Gate.

A `live` variant runs the same cases on the real tiers. It runs on releases. To add a case, add one line to the JSONL file.

### Known Limits

- The screen is a model that reads untrusted text. An injection could target the screen itself, for example with "reply none".
- The filter and the screen are heuristics. They lower the risk and do not remove it.
- The filter matches plain text only. Lookalike characters, zero-width characters or a phrasing outside its patterns slip past it, and only the screen is left to catch them.
- The Run input, which is the user's own message, is not screened. The requester is an authenticated principal, and Gates still apply.
- Dict keys are not stripped.
- When the screen flags a knowledge result, all of its passages are withheld, including the clean ones.

The risk that remains is bounded by the Step whitelist and the Gates.
