# Declaring Tools

The tool registry turns a Tool name into something the runner can call. Before a Tool runs, the registry checks the arguments against the Tool's `args_schema`. This is a JSON Schema. The draft comes from its `$schema` field and defaults to 2020-12. Formats such as `date` are checked. A failed check raises `ToolArgumentError` and the Tool does not run. Every call, successful or not, records a `ToolCall` and a `tool.called` AuditEvent through the RunStore.

The registry does not decide who may call a Tool. Whitelists and gates stay with the runner. This page covers the `python`, `rest` and `mcp` kinds. The `knowledge`, `directory` and `decision` kinds register from their own modules.

## Kind: python

The Tool name is `module.function`. Every part must be a valid Python identifier. The registry looks for the module in two places, in this order:

1. The workspace file `tools/<module>.py`.
2. The package module `cograil.tools.<module>`.

Tools that share a module share one loaded copy of it, so module-level state is shared. A sync function runs in a worker thread. An async function is awaited directly.

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

- Placeholders such as `{employee}` are filled from the arguments and URL-encoded. Other arguments go to the query string for GET and DELETE, and to the JSON body for the other methods.
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

One entry names one Model Context Protocol (MCP) server. The server's tools register as `<entry name>.<server tool name>`. Each one takes the server's input schema, plus the entry's `scope` and `confirm_before_write`. An entry named `github` whose server lists `create_issue` gives the Tool `github.create_issue`.

```yaml
# tools.yaml
tools:
  - name: files
    kind: mcp
    scope: read
    mcp: {transport: stdio, command: npx, args: ["-y", "@modelcontextprotocol/server-filesystem", "/data"]}
  - name: github
    kind: mcp
    scope: write
    mcp: {transport: http, url: "https://mcp.example.com/mcp"}
```

A `stdio` server needs `command`. An `http` server needs `url` and uses streamable HTTP. This kind needs the `mcp` extra: `pip install cograil[mcp]`.

The server's output is returned as data, never as instructions. Because Protocol `@` references can only name Tools whose names are word characters and dots, a protocol can reference an MCP Tool only when both the entry name and the server's tool name use those characters.
