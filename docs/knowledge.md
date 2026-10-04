# Loading Knowledge

A KnowledgeSource is a folder of documents in a Workspace. Cograil cuts each document into Chunks and stores them, each with the `acl_groups` that may see it. ACL stands for access control list. This page covers loading. Searching the Chunks is a separate feature (see "What Is Not Here Yet").

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
| `acl_groups` | The default groups for files that have no ACL file of their own. |
| `chunk_size` | The most characters in one Chunk. The default is 800. |
| `chunk_overlap` | The characters shared by neighbouring Chunks. The default is 120. |

`chunk_size` and `chunk_overlap` count characters, not words. `chunk_overlap` must be smaller than `chunk_size`, or the Workspace is invalid.

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

The nearest one wins. It replaces the others. It does not add to them.

An ACL file looks like this.

```yaml
# policies/hr/.acl.yaml
acl_groups: [hr, managers]
```

An ACL file that is unreadable, is not valid YAML, has no `acl_groups`, or has an empty list is an error. So is a list with a blank or non-text name. The sync stops and names the file. An ACL file mistake never turns into "everyone" or "no one". Fix the file and sync again.

## Chunk Ids

A Chunk has an `id`, a `source`, a `source_uri`, its `text` and its `acl_groups`.

- `source_uri` is the file path relative to the Workspace folder, such as `knowledge/policies/hr/leave.md`.
- `id` is `<source>:<uri>#<n>`, where `n` counts from 0 in that file. The first Chunk of that file in `policy-library` is `policy-library:knowledge/policies/hr/leave.md#0`.

The same file always gives the same ids. This is how a sync tells a new Chunk from a changed one.

## Syncing

```bash
cograil knowledge sync workspaces/example-smb
```

The command needs `DATABASE_URL`, in the form `postgresql+asyncpg://user:password@host/db`. The Chunks live in the `chunks` table. Alembic migration `0003` creates it. The sync does not run the migration, so apply it first on a new database.

```bash
alembic upgrade head
```

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

## What Is Not Here Yet

This page covers loading only. Searching the Chunks, with the `acl_groups` pre-filter that limits results to the principal's groups before anything is ranked, is issue #23. Until then, nothing reads the Chunks back into a Run.
