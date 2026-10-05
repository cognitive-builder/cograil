# Threat Model

This page lists threats to a Cograil Run and the defences against them. It has these sections:

- Injection through retrieved content and tool output: text written to look like instructions to the model.
- Tool argument injection: what a model steered by that text can make a Tool do.
- Approval link replay: reusing, forging or stealing the link an approver gets by email.
- Secret handling: where credentials come from and where they must never end up.
- Sign-in and sessions.
- The security review of October 2026 (issue #36): what it found and what became of each finding.

One guarantee runs through every section. The runner enforces the Step whitelist and the Gates as data (rules 2 and 7 in `CLAUDE.md`). Every other layer lowers a risk. That one bounds it.

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

## Tool Argument Injection

### The Threat

The model proposes every Tool call, arguments included. Injected text that gets past the layers above can change those arguments. It can also lead to a call the Step allows that the user never asked for. The attacker wants to:

- Reach another endpoint, host or file.
- Widen a search past what the principal may see.
- Act for someone other than the principal.
- Slip a harmful value past the person who approves a write.

### The Defences

1. **Whitelist first.** Every planned call is checked against the Step's Tools before any call of the batch runs (`ToolNotAllowed`). The model is only offered the Step's Tools.
2. **Schema check.** Arguments are validated against the Tool's `args_schema` (JSON Schema 2020-12, with format checks) before every call. Schemas are checked when Tools are registered. The Tool gets a deep copy of the arguments. Python Tools reject unknown keyword arguments.
3. **REST calls stay where the pack put them.**
    - The base URL comes from the Connection. The method and headers come from `tools.yaml`, never from arguments.
    - Path arguments are percent-encoded with nothing kept safe, so `/`, `?`, `#` and `:` cannot change the path.
    - Empty values, `.` and `..` are refused.
    - A next-page URL must be on the same origin, so the bearer token never leaves it.
4. **Entitlement is not an argument.** A `knowledge` search filters by the groups of the Run's principal, taken from the call context and never from the arguments (rule 3). The filter runs in SQL, with bound parameters, before ranking. Its sources are fixed by the workspace. Its `limit` is 1 to 20.
5. **Python Tools.** The module comes from the Tool's name when the pack is loaded, inside the pack's `tools/` folder, never from an argument.
6. **Decision tables.** Missing, unknown or mistyped inputs are refused. The table, not the model, picks the rule (rule 10).
7. **The Gate binds the exact call.** A write with `confirm_before_write` needs an Approval for the same Run, Step, Tool and arguments. The approver is shown the full arguments. On approval, the saved plan is replayed with no model turn in between, so nothing can change between what was approved and what runs. Each Approval is spent atomically before the write and authorises one call. The approver can never be the Run's own principal.
8. **Approvers see the whole call.** The approval page and the email link page show every argument. A Slack prompt clips arguments at 1000 characters. A clipped prompt has no Approve button: padding in one argument cannot push another out of sight.
9. **Audit.** Every call writes an AuditEvent with the principal. A write Tool writes `tool.started` before it runs.

### Known Limits

- **The model fills in whom a read is for.** A Tool cannot yet bind an argument to the principal. In the example pack, `hris.get_balance(employee=...)` is read scope and ungated. Writes for someone else are gated, and the approver sees the argument. Tracked in #272.
- **Unknown keys reach REST Tools.** An `args_schema` that does not set `additionalProperties: false` lets extra keys through into the query string or body. Pack authors should close their schemas. Tracked in #273.
- **REST Tools act as the service account.** On-behalf-of calls are not supported yet. A free-form query Tool sees whatever the service account sees.
- **MCP arguments are checked against the server's own schema,** as listed by the server. An MCP tool the pack does not list is write scope and gated.
- **Argument size is bounded only by the provider's output limit.** A cap is tracked in #286.
- **An ungated write is a way out.** A write Tool with `confirm_before_write: false` and a free recipient can send data anywhere if the model is steered. The example pack's `notify.send` is a mock. Gate real ones.

## Approval Link Replay

### The Threat

When mail is configured, an approver gets a one-click link: `/approvals/link/{token}?exp=…&sig=…`. Slack approvers get buttons. An attacker wants to:

- Approve a gate without being its approver.
- Use a link twice, or on another gate.
- Use a link after it expired.
- Get a link approved by a mail scanner that opens it.

### The Defences

1. **Signed for one Approval and one approver.** The signature is HMAC-SHA256 over the Approval's token, the normalised approver and the expiry. It is keyed by `COGRAIL_APPROVAL_LINK_SECRET` (32 characters or more) and compared in constant time. The token is 128 random bits. The Approval row it names fixes the Run, Step, Tool and arguments. No other Approval, approver or expiry verifies, and a forged link answers 403.
2. **Single use, atomically.** An Approval is decided by a conditional update from `pending`, in one transaction with the Run and the AuditEvent. Of two concurrent clicks, exactly one wins. Any later use answers 409.
3. **Expiry.** The link expires with its Approval (`approvals.timeout_hours`). A sweep inside the service (`cograil.approval_sweep`, at startup and every minute) records every overdue Approval as expired and escalates its Run, whether or not anyone opens the link. A late link answers 410.
4. **Opening it decides nothing, and no GET writes anything.** GET shows the call, or answers 410 for an expired link without touching the Run or the Approval. Only a POST with a JSON body decides. The page sends `no-store`, `no-referrer`, a strict Content Security Policy and `frame-ancestors 'none'`.
5. **The Runner's rules still apply.** The link decides as the Approval's approver, who can never be the Run's own principal. A Run started under another harness or tool pack is refused.
6. **Slack.** Bolt checks Slack's request signature and timestamp. A replay inside Slack's window hits the single-use decision. The decider is the member who clicked, mapped through `slack_id`. Only a `kind: user` principal can be mapped.
7. **Kept out of the logs.** The service strips the query from approval-link paths in its access log. The link is never written to an AuditEvent.

### Known Limits

- **A link is a bearer credential until it is used.** Whoever holds the email, through forwarding, a delegate or a shared inbox, can decide as the approver until the Approval is decided or expires. The AuditEvent records `via: email_link` but cannot tell who clicked. Deployments that need proof of identity should turn approval email off and approve in the signed-in web chat or in Slack.
- **Request logs in front of the service** still record the full URL, for example Cloud Run's request log or a reverse proxy. Restrict who can read them.
- **Decision AuditEvents carry the requester as principal.** The approver is in `detail.decided_by`. Tracked in #275.
- **Rotating the link secret** voids every outstanding link. This fails safe: the approver decides in the web chat instead.
- **An overdue Approval waits for the next sweep.** The sweep runs once a minute while the service is up, and once at startup for what fell due while it was down. An Approval can stay `awaiting_approval` for up to a minute past its expiry, and for as long as the service is stopped. A Run started on a Protocol version the workspace no longer has is skipped by the sweep (logged as `approval.sweep_skipped`) and stays `awaiting_approval`, as no decision can resume it either. A late POST of the link still escalates the Run at once.

## Secret Handling

### The Threat

Cograil holds:

- The model provider's key.
- The Connections' client secrets and tokens.
- The session and approval-link secrets.
- The Slack bot token and signing secret.
- SMTP or Resend credentials.

An attacker wants one of them from:

- The repository or the image.
- A log or a trace.
- A Run record or an AuditEvent.
- An API response.
- The model's context.

### The Defences

1. **Environment only.** A Connection names environment variables. It never holds values, and extra fields are refused. Every other setting is read from the environment and kept as a `SecretStr`. The image holds no secrets: Cloud Run injects them from Secret Manager. The deploy uses Workload Identity, not keys.
2. **Not in the repository.** `.gitignore` and `.dockerignore` exclude `.env`, `.env.*`, `*.env` (a copied `sso.env`) and `workspaces/*/.secrets`. The example packs hold only variable names and placeholders.
3. **Minimum lengths.** The session and link secrets must be 32 characters or more, or the service refuses to start.
4. **Errors that never echo values.** Configuration errors name the setting, not its value. REST errors carry the method, path and status, never the URL's query or the headers. A token response is never logged. Only the response body is returned to the model.
5. **Redaction before persistence.** Tool error text is redacted before it is stored in a ToolCall or an AuditEvent, and so is the reason a Run failed. Two passes do this:
    - Patterns: emails, bearer and Basic credentials, provider keys, JWTs, URL userinfo, connection strings, and `name=value` pairs whose name ends in a secret-like word, such as `access_token`, `x-api-key`, `private_key` or `sig`.
    - Then the small tier, for what the patterns miss.
6. **Logs and traces.** Spans carry no prompts, arguments or outputs, and an exception is recorded by its type only. Errors shown to a client are generic. The detail goes to the log, redacted.
7. **CI.** No workflow runs on `pull_request_target`. Workflows that use secrets are gated in one of three ways:
    - The review and auto-merge jobs run only for branches of this repository.
    - The `@claude` jobs answer only owners, members and collaborators.
    - The deploy and the lane 3 job run only on a tag or when started by hand.
    - **History is scanned for secrets.** A `secrets` job runs gitleaks 8.28.0 (pinned, checksum verified) over the full git history of every pull request and every push to `main`. A new fake secret that a test needs carries a `gitleaks:allow` comment on its line. The six fake tokens already in `tests/test_redaction.py` are listed in `.gitleaksignore` by their fingerprints.
    - **The deploy identity must be pinned.** Other workflows here, including the ones that run a model on text from issues, also request an identity token, so the Workload Identity condition names the deploy workflow on a version tag and not just the repository (`docs/deploy.md`). More hardening is a decision for the owner: #285.

### Known Limits

- **Successful Tool output is stored raw.** Arguments, results and Step outputs are kept on the Run as the audit trail of what happened, and only error text is redacted. The Run's own principal can read them through `GET /runs/{id}`. A Tool that returns a secret puts that secret in the Run and in the model's context. Do not build Tools that return credentials.
- **Pattern redaction is a heuristic.** Without a configured model, as in a CLI or eval run without a key, only the patterns apply. The patterns miss a camelCase name (`accessToken=`) and a name with letters glued in front (`mytoken=`). They also mask some harmless text, for example the word after `Signature:`.
- **MCP servers cannot take credentials from a Connection yet,** so a token can end up in `tools.yaml`. Tracked in #274.
- **The length check cannot tell a placeholder from a secret.** The placeholder in `sso.env.example` is long enough to pass. Generate the secret as `docs/auth.md` shows.
- **`http://` base URLs are allowed** for local mocks. Use `https://` for anything that carries a credential.
- **Python Tools run in the service's process** and can read its environment. Review a pack's Python like any other code you deploy.
- **The sign-in refusal log keeps the provider's error text,** which names the principal, so operators can see why a sign-in failed.

## Sign-In and Sessions

### The Defences

1. **No default mode.** The service refuses to start without `COGRAIL_AUTH`.
2. **OIDC sign-in.** It uses the authorization code flow with state and nonce. The ID token's signature, issuer, audience and expiry are validated. When the id claim is `email`, the provider must have verified it, and its domain must be allowed. An Entra sign-in must match the `oid` in `principals.yaml`. A system principal can never sign in.
3. **The session cookie.**
    - It is HttpOnly, Secure and SameSite=Lax, and signed with `COGRAIL_SESSION_SECRET`.
    - It is cleared before a new sign-in, which prevents session fixation.
    - It ends 8 hours after sign-in, counted from the sign-in time it carries. A cookie the middleware signs again later, for example on a visit to `/auth/login`, does not live longer.
4. **Every non-public route needs a signed-in Principal.**
    - A Run is visible only to the principal who started it.
    - An Approval is visible only to its approver.
    - Audit events are visible to the Run's owner and to auditors listed in `principals.yaml`.
5. **The web pages insert server text with `textContent` only,** under a Content Security Policy.
6. **Size and rate limits.** A request body over 1 MiB answers 413. A chat message is 1 to 8000 characters, on the web chat and in Slack. `POST /chat` is rate limited for each Principal (429 with `Retry-After`; see `docs/api.md`).

### Known Limits

- **The chat rate limit is held in each process.** With several workers or replicas, the real limit is the limit times the number of processes.
- **Slack messages are not rate limited.** Only `POST /chat` is.
- **Signing out clears the browser's copy only.** A copied cookie stays valid until its 8 hours are up, and so do the groups fixed at sign-in. Rotate `COGRAIL_SESSION_SECRET` to end every session at once.
- **`COGRAIL_AUTH=dev` signs everyone in as one principal.** It is for a laptop, never for a reachable service. The service refuses to start with it when `COGRAIL_ENV=production`, which the deploy workflow sets.
- **The OpenAPI docs at `/docs` are public.** They list the routes and hold no data.

## Security Review, October 2026

Issue #36 asked for a run of `anthropics/claude-code-security-review`. That action is a GitHub workflow that needs an Anthropic API key, which this repository's CI does not hold (its review job runs through another provider). A build session cannot start it either. So the same review was done in the session instead, with Claude Code's `/security-review` method:

- Four reviewers read the code, one for each area: tool arguments, approval links, secrets, and sign-in with the API surface.
- Every finding was checked against the code before it was acted on.

Adding the action to CI is left to the maintainers (see the pull request's open questions).

No finding was high severity. None bypasses the Step whitelist, a Gate, or the knowledge access filter.

| Finding | Severity | Outcome |
| --- | --- | --- |
| A Slack prompt clipped the arguments but still offered Approve | Medium | Fixed: a clipped prompt offers Decline only |
| The access log recorded approval links with their signature | Medium | Fixed: the query is stripped from link paths; proxy logs are documented above |
| `/auth/login` signed the session again, so a session could be kept alive past 8 hours | Medium | Fixed: the session carries its sign-in time and ends 8 hours after it |
| Redaction patterns missed `access_token=`, `refresh_token=`, `private_key=`, `Authorization: Basic`, `sig=` | Medium | Fixed |
| A system principal given a `slack_id` could be used from Slack | Low | Fixed: Slack maps `kind: user` principals only |
| A copied `sso.env` was neither git- nor docker-ignored | Low | Fixed: `*.env`, `.env` and `.env.*` are ignored by git at every depth and by docker at every depth (`**/`; a pattern without it matches only the build context root) |
| The model chooses whom a read is for | Medium | Documented; #272 |
| An `args_schema` accepts unknown keys by default | Medium | Documented; #273 |
| MCP servers have no Connection credentials | Low | Documented; #274 |
| Decision AuditEvents name the requester, not the decider | Low | Documented; #275 |
| Dev mode is not refused in production | Low | Documented; #276 |
| An email link is a bearer credential | Medium | Documented above |
| Successful Tool output is stored raw | Medium | Documented above, by design |
| Sign-out does not revoke a copied cookie | Low | Documented above |
| No argument size cap | Low | Issue #286 |
| `http://` base URLs; placeholder secrets pass the length check; public OpenAPI docs | Info | Documented above |
