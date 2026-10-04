# Declaring Tools

The tool registry turns a Tool name into something the runner can call. Before a Tool runs, the registry checks the arguments against the Tool's `args_schema`. This is a JSON Schema. The draft comes from its `$schema` field and defaults to 2020-12. Formats such as `date` are checked. A failed check raises `ToolArgumentError` and the Tool does not run. Every call, successful or not, records a `ToolCall` and a `tool.called` AuditEvent through the RunStore. A `scope: write` Tool also records a `tool.started` AuditEvent after the check and before it runs, so a write that crashes partway still leaves a record. It names the Tool and the step but not the arguments; those stay in the `ToolCall`.

The registry does not decide who may call a Tool. Whitelists and gates stay with the runner. This page covers the `python`, `rest`, `mcp` and `decision` kinds. The `knowledge` and `directory` kinds register from their own modules.

## Kind: python

The Tool name is `module.function`. Every part must be a valid Python identifier. The registry looks for the module in two places, in this order:

1. The workspace file `tools/<module>.py`.
2. The package module `cograil.tools.<module>`.

Workspace tool files are trusted code: they run when the registry is built, so review them like any other code. Names with a part starting with `_` are rejected. Tools that share a module share one loaded copy of it, so module-level state is shared. A sync function runs in a worker thread. An async function is awaited directly.

```yaml
# tools.yaml
tools:
  - name: hris.get_balance
    kind: python
    scope: read
    args_schema:
      type: object
      properties: {employee: {type: string}}
      required: [employee]
```

This resolves to `get_balance` in `tools/hris.py`.

## Kind: rest

A `rest` Tool needs a `rest:` block and a `connection` that supplies the base URL.

```yaml
# tools.yaml
tools:
  - name: hris.list_requests
    kind: rest
    scope: read
    connection: hris
    args_schema:
      type: object
      properties: {employee: {type: string}, status: {type: string}}
      required: [employee]
    rest:
      method: GET
      path: /employees/{employee}/requests
      pagination:
        items: data.items
        next: data.next
        cursor_param: cursor
        max_pages: 10
      max_attempts: 3
```

```yaml
# connections.yaml
connections:
  - name: hris
    auth: oauth_client_credentials
    base_url: https://hris.example.com/api
    token_url: https://hris.example.com/oauth/token
    scopes: [leave.read]
    secret_env:
      client_id: HRIS_CLIENT_ID
      client_secret: HRIS_CLIENT_SECRET
```

`secret_env` maps each secret to the name of an environment variable. The secrets themselves are never stored in the workspace. Only `auth: none` and `auth: oauth_client_credentials` work so far.

### How a Call Is Built

- Placeholders such as `{employee}` are filled from the arguments and URL-encoded. A placeholder value that is empty, `.` or `..` raises `ToolArgumentError`, so an argument can never move the call to another endpoint. Other arguments go to the query string for GET and DELETE, and to the JSON body for the other methods.
- The access token is cached until 30 seconds before it expires. On a 401 response it is fetched again and the call is retried once.

### Pagination

`items` is a dotted field holding the list of results. `next` is a dotted field holding either a next-page URL or a cursor.

- Without `cursor_param`, `next` holds a URL. It must stay on the same origin as the connection's `base_url`.
- With `cursor_param`, the value is sent as that query parameter.
- If there are more than `max_pages` pages, the call fails with an error. It never returns a silently shortened list.

### Retries

`max_attempts` is the total number of tries. The wait starts at 0.5 seconds and doubles each time. A call is retried in these cases:

- HTTP 429, for every method.
- HTTP 5xx and dropped connections, only for GET, PUT and DELETE.
- A connection that never opened, for every method.

### Logging

The registry writes JSON lines on the `cograil` logger: `tool.rest.response`, `tool.rest.retry` and `tool.rest.token`. They show the path template, such as `/employees/{employee}/requests`, and not the filled values. They never include secrets.

## Kind: mcp

One entry names one Model Context Protocol (MCP) server. The server's tools register as `<entry name>.<server tool name>`. Each one takes the server's input schema and the entry's `confirm_before_write`. An entry named `github` whose server lists `create_issue` gives the Tool `github.create_issue`.

Server tools do not inherit the entry's `scope`. One server often has both read and write tools, so each server tool is `scope: write` unless `mcp.read_tools` lists it by its server name. Every unlisted tool on a `scope: read` entry pauses for approval, even if the entry sets `confirm_before_write: false`. Only a `scope: write` entry can waive confirmation for its server's tools, because the author has then acknowledged that the server writes. A name in `read_tools` that the server does not list raises `ToolConfigError`, so a typo cannot quietly leave a tool gated or ungated.

```yaml
# tools.yaml
tools:
  - name: files
    kind: mcp
    scope: read
    mcp:
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
      read_tools: [read_text_file, list_directory, search_files]  # write_file stays gated
  - name: github
    kind: mcp
    scope: write
    mcp: {transport: http, url: "https://mcp.example.com/mcp"}
```

A `stdio` server needs `command`, which the registry starts as a local process, so treat it as trusted configuration. An `http` server needs `url` and uses streamable HTTP. This kind needs the `mcp` extra: `pip install cograil[mcp]`.

The server's output is returned as data, never as instructions. Because Protocol `@` references can only name Tools whose names are word characters and dots, a protocol can reference an MCP Tool only when both the entry name and the server's tool name use those characters.

## Kind: decision

A `decision` Tool evaluates a decision table. The model supplies the inputs. The table decides. The Tool name is `<anything>.<table>`. The part after the last dot names the table in `decisions/<table>.yaml`. The file name (without `.yaml`) must equal the table's `name`.

```yaml
# tools.yaml
tools:
  - name: decide.approval_routing
    kind: decision
    scope: read
    description: Approval tier from duration, leave type and role.
    args_schema:
      type: object
      properties: {duration_days: {type: integer}, leave_type: {type: string}, requester_role: {type: string}}
      required: [duration_days, leave_type, requester_role]
```

```yaml
# decisions/approval_routing.yaml
name: approval_routing
version: 1
hit_policy: first
inputs:
  duration_days: {type: integer}
  leave_type: {type: string}
  requester_role: {type: string}
outputs:
  approver_tier: {type: string}
  requires_hr: {type: boolean}
rules:
  - id: r1
    when: {leave_type: unpaid}
    then: {approver_tier: hr_ops, requires_hr: true}
  - id: r2
    when: {duration_days: ">10"}
    then: {approver_tier: skip_level, requires_hr: true}
  - id: r3
    when: {requester_role: manager}
    then: {approver_tier: director, requires_hr: false}
  - id: default
    when: {}
    then: {approver_tier: manager, requires_hr: false}
```

### The Table Format

- `name` and `version` identify the table. Rule ids must be unique.
- `hit_policy` is `first` or `collect`. Under `first`, the first rule in table order whose conditions all hold decides. Under `collect`, every matching rule is returned, in table order.
- `inputs` and `outputs` map a name to a type. The types are `string`, `integer`, `number` and `boolean`.
- Each rule has an `id`, a `when` and a `then`.

### Conditions

A `when` maps an input to a condition. An input the rule leaves out matches anything. A condition is one of these:

- A value. The input must equal it.
- A list. The input must equal one of the values.
- A comparison string, such as `">10"`, `"<=5"` or `"=3"`. This works for `integer` and `number` inputs only.

Every rule's `then` must set every output, each with its declared type.

### Inputs and Results

After the `args_schema` check, the table checks the inputs itself. All declared inputs are required. Unknown inputs are refused. Each input must have its declared type, except that an `integer` input accepts `3.0` as `3`. A bad input raises `DecisionError`.

Under `first`, if no rule matches, the call raises `DecisionError`. Under `collect`, zero matches is fine.

The model sees this result under `first`:

```json
{"table": "approval_routing", "version": 1, "rule": "r2", "outputs": {"approver_tier": "skip_level", "requires_hr": true}}
```

Under `collect` it sees every match:

```json
{"table": "approval_routing", "version": 1, "matches": [{"rule": "r2", "outputs": {"approver_tier": "skip_level", "requires_hr": true}}, {"rule": "default", "outputs": {"approver_tier": "manager", "requires_hr": false}}]}
```

### Audit

Every evaluation writes a `decision.evaluated` AuditEvent. It records the `tool`, the `step`, the `table`, its `version`, the `hit_policy` and `rules`, the ids of the rules that fired. The inputs stay in the `ToolCall`.

### Checks at Load Time

`cograil validate` rejects an invalid table. It also rejects a `decision` Tool whose table is missing.

### Trying a Table by Hand

```bash
cograil decide approval_routing --workspace workspaces/example-smb \
  --input duration_days=12 --input leave_type=annual --input requester_role=staff
```

```
approval_routing v1 (first)
rule r2: {"approver_tier": "skip_level", "requires_hr": true}
```

This starts no Run and calls no model. It writes no AuditEvent. Each value is read as its input's declared type, and a boolean is `true` or `false`. The command exits with 1 on a bad input or when no rule matches.
