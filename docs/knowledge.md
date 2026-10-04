# Loading Knowledge

A KnowledgeSource is a folder of documents in a Workspace. Cograil cuts each document into Chunks and stores them, each with the `acl_groups` that may see it. ACL stands for access control list. This page covers loading and then searching (see "Searching").

## Declaring a KnowledgeSource

Declare sources in the Workspace file `knowledge.yaml`.

```yaml
# knowledge.yaml
knowledge:
  - name: policy-library
    path: knowledge/policies
    acl_groups: [all-employees]
```

| Field | Meaning |
| --- | --- |
| `name` | The name of the source. It is part of every Chunk id. |
| `path` | The folder, relative to the Workspace folder. It must stay inside the Workspace. |
| `acl_groups` | The default groups for files that have no ACL file of their own. It is also the most that any ACL file can name. It must list at least one group. |
| `chunk_size` | The most characters in one Chunk. The default is 800. |
| `chunk_overlap` | The characters shared by neighbouring Chunks. The default is 120. |

`chunk_size` and `chunk_overlap` count characters, not words. `chunk_overlap` must be smaller than `chunk_size`, or the Workspace is invalid. A KnowledgeSource with `acl_groups: []` is also invalid. `cograil validate` reports it.

A Chunk ends at a space when one falls in the second half of its window, so words are not cut when they need not be. A document with no text gives no Chunks. A PDF with only scanned images is such a document, because Cograil does not read images.

## What Is Loaded

Cograil reads every `.md`, `.markdown` and `.pdf` file in the folder and its subfolders. Other files are ignored. The check on the file ending does not care about upper or lower case.

Two rules keep a source inside its folder:

- The `path` must resolve to a folder inside the Workspace. A path such as `../shared` is an error.
- A file that resolves outside the source folder, for example a symlink to `/etc`, is refused. The whole sync fails. The file is not skipped.

A file that cannot be read, or a PDF that cannot be parsed, is also an error.

## Who May See a Document

Each document gets its `acl_groups` from the first of these that exists:

1. A sidecar file beside it, named `<file>.acl.yaml`. For `leave.md` this is `leave.md.acl.yaml`.
2. The file `.acl.yaml` in the nearest folder above it, up to and including the source folder.
3. The `acl_groups` of the KnowledgeSource.

The nearest one still wins over the ones farther away. But an ACL file can only narrow. A document gets the groups its ACL file names that the KnowledgeSource's `acl_groups` also names. A file in the content folder cannot grant a group that the source never declared.

An ACL file looks like this.

```yaml
# policies/hr/.acl.yaml
acl_groups: [hr, managers]
```

If the source declares `acl_groups: [all-employees, hr]`, a document under this file gets `[hr]`. `managers` is dropped, because the source does not name it.

An ACL file that is unreadable, is not valid YAML, has no `acl_groups`, or has an empty list is an error. So is a list with a blank or non-text name. So is an ACL file that shares no group with the source. The sync stops and names the file. An ACL file mistake never turns into "everyone" or "no one". Fix the file and sync again.

## Chunk Ids

A Chunk has an `id`, a `source`, a `source_uri`, its `text` and its `acl_groups`.

- `source_uri` is the file path relative to the Workspace folder, such as `knowledge/policies/hr/leave.md`.
- `id` is `<source>:<uri>#<n>`, where `n` counts from 0 in that file. The first Chunk of that file in `policy-library` is `policy-library:knowledge/policies/hr/leave.md#0`.

The same file always gives the same ids. This is how a sync tells a new Chunk from a changed one.

## Syncing

```bash
cograil knowledge sync workspaces/example-smb
```

The command needs `DATABASE_URL`, in the form `postgresql+asyncpg://user:password@host/db`. The Chunks live in the `chunks` table. Alembic migration `0003` creates it. Migration `0004` adds the `embedding` column and enables the pgvector `vector` extension. The column holds 256 numbers for each Chunk. The sync does not run the migrations, so apply them first on a new database or after an upgrade.

```bash
alembic upgrade head
```

Migrations connect as the owner. Where `DATABASE_URL` names the `cograil_app` role, set `MIGRATIONS_DATABASE_URL` to the owner's URL first (see "Database Roles" in `docs/deploy.md`).

Each Chunk the sync writes is embedded, so search can rank it (see "Embeddings"). Chunks stored before migration `0004` have no embedding. Search skips them. The next sync rewrites them, and they count as `updated`.

The command prints one line for each source, in the order declared in `knowledge.yaml`:

```
policy-library: 3 files, 12 chunks (added 12, updated 0, removed 0, unchanged 0)
```

A Workspace with no sources prints `<workspace name>: no knowledge sources`.

### Running It Again

A sync makes the store hold exactly the Chunks that the files give now.

- `added`: Chunks that are new.
- `updated`: Chunks whose text or `acl_groups` changed.
- `removed`: Chunks of files that were edited shorter or deleted.
- `unchanged`: Chunks that already matched. They are not written again.

A second sync of unchanged files writes nothing. Each source is synced in its own database transaction. If one source fails, the sources before it stay synced and the ones after it are not tried. A source you delete from `knowledge.yaml` is no longer synced, and its old Chunks stay in the store.

### Exit Codes

- `0`: all sources synced.
- `1`: an error. This covers an invalid Workspace, a folder, document or ACL file that cannot be loaded, and a missing `DATABASE_URL`. The message goes to standard error.
- `2`: a usage error, such as a missing argument.

## Searching

A Run reads Chunks back through a Tool of kind `knowledge`. The example Workspace declares one.

```yaml
# tools.yaml
tools:
  - name: knowledge.search
    kind: knowledge
    scope: read
    description: Search the policy library with ACL pre-filter.
    args_schema: {type: object, properties: {query: {type: string}}, required: [query]}
```

The Tool takes these arguments.

| Argument | Meaning |
| --- | --- |
| `query` | The text to look for. It must not be empty. |
| `limit` | Optional. The most results to return, from 1 to 20. The default is 5. |

A bad `query` or `limit` raises `ToolArgumentError`. The Tool searches every KnowledgeSource of the Workspace.

### Who Is Searching

The principal's groups come from the Run, never from the arguments. The runner sets them on the CallContext. The model cannot ask for more groups, so it cannot see more. A principal with no groups finds nothing.

### Filter First, Then Rank

The filter on `acl_groups` runs first. It runs inside a materialized subquery, so Postgres must finish it before anything else. Only the Chunks that name at least one of the principal's groups are ranked, by cosine similarity to the query. This means the top results are always the best Chunks the principal may see. They are never the best Chunks overall with the forbidden ones removed afterwards.

There is deliberately no vector index on `embedding`. An index would rank first and filter after, which is the wrong order.

### The Result

The Tool returns the most similar Chunks first.

```json
{"results": [{"source": "policy-library", "source_uri": "knowledge/policies/hr/leave.md", "chunk_id": "policy-library:knowledge/policies/hr/leave.md#0", "passage": "Annual leave ...", "score": 0.4123}]}
```

- `source`, `source_uri` and `chunk_id` say where the passage came from.
- `passage` is the Chunk's text, word for word, so an answer can cite it.
- `score` is the cosine similarity, rounded to 4 places. Higher is closer.

The results reach the model as data, never as instructions. See the [threat model](threat-model.md) for how instruction-like text in a passage is stripped or withheld, and where that stops.

### Embeddings

An embedding is a list of numbers that stands for a piece of text. Search compares the embedding of the query with the embedding of each Chunk.

The default embedder, `HashingEmbedder`, hashes each word into one of 256 slots. It needs no model and no network, and it gives the same numbers every time. The cost is that similarity rewards shared words, not shared meaning. A query for "holiday" will not find a Chunk that only says "vacation".

A model-backed embedder can replace it behind the `Embedder` protocol, as long as its vectors are 256 wide. The store refuses an embedder of another width.

### Running It

`cograil run` registers the knowledge Tools against the Postgres store named by `DATABASE_URL`. Sync the Workspace first, so there are Chunks to find.
