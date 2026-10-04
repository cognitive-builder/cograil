"""Knowledge loader tests, one per acceptance criterion of issue #22, on an in-memory store
(and Postgres for the store contract when DATABASE_URL is set)."""

import asyncio
import shutil
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from itertools import pairwise
from pathlib import Path

import pytest
from typer.testing import CliRunner

import cograil.knowledge.cli
from cograil.cli import app
from cograil.domain import Chunk, KnowledgeSource
from cograil.errors import KnowledgeSourceError
from cograil.knowledge import (
    InMemoryKnowledgeStore,
    KnowledgeStore,
    PostgresKnowledgeStore,
    SyncCounts,
    build_chunks,
    chunk_text,
    sync_source,
)

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "workspaces/example-smb"
runner = CliRunner()


def make_pdf(text: str) -> bytes:
    """A one-page PDF whose only content is `text` (no parentheses or backslashes)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )


def source(**overrides: object) -> KnowledgeSource:
    fields: dict[str, object] = {"name": "docs", "path": "kb", "acl_groups": ["staff"]}
    return KnowledgeSource.model_validate(fields | overrides)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A workspace folder with kb/ holding one Markdown file, one PDF and a subfolder."""
    kb = tmp_path / "kb"
    (kb / "hr").mkdir(parents=True)
    (kb / "intro.md").write_text("# Intro\n\nWelcome to the company handbook.\n")
    (kb / "hr" / "pay.md").write_text("# Pay\n\nSalaries are paid monthly.\n")
    (kb / "faq.pdf").write_bytes(make_pdf("Ask HR about leave"))
    (kb / "notes.txt").write_text("not a knowledge file")
    return tmp_path


# --- Loads a folder of Markdown and PDF; chunk size and overlap configurable ---


def test_loads_markdown_and_pdf_and_nothing_else(workspace: Path) -> None:
    files, chunks = build_chunks(workspace, source())
    assert files == 3
    assert {c.source_uri for c in chunks} == {"kb/intro.md", "kb/hr/pay.md", "kb/faq.pdf"}
    assert any("Ask HR about leave" in c.text for c in chunks if c.source_uri == "kb/faq.pdf")
    assert {c.source for c in chunks} == {"docs"}


@pytest.mark.parametrize(("size", "overlap"), [(20, 0), (20, 5), (50, 10), (800, 120)])
def test_chunk_size_and_overlap_are_configurable(size: int, overlap: int) -> None:
    text = " ".join(f"word{n:03d}" for n in range(60))
    chunks = chunk_text(text, size, overlap)
    assert all(len(c) <= size for c in chunks)
    assert " ".join(chunks).count("word059") >= 1
    for before, after in pairwise(chunks):
        shared = next((n for n in range(min(len(before), len(after)), 0, -1)
                       if before.endswith(after[:n])), 0)  # fmt: skip
        assert shared >= (overlap > 0) and shared <= overlap


def test_the_source_settings_reach_the_chunks(workspace: Path) -> None:
    (workspace / "kb" / "long.md").write_text("alpha beta gamma delta " * 40)
    small = [c for c in build_chunks(workspace, source(chunk_size=60, chunk_overlap=10))[1]
             if c.source_uri == "kb/long.md"]  # fmt: skip
    large = [c for c in build_chunks(workspace, source(chunk_size=400, chunk_overlap=0))[1]
             if c.source_uri == "kb/long.md"]  # fmt: skip
    assert len(small) > len(large) > 1
    assert all(len(c.text) <= 60 for c in small)


@pytest.mark.parametrize(("size", "overlap"), [(0, 0), (10, 10), (10, 20), (10, -1)])
def test_a_bad_chunk_setting_is_refused(size: int, overlap: int) -> None:
    with pytest.raises(KnowledgeSourceError):
        chunk_text("some text", size, overlap)
    with pytest.raises(ValueError, match="chunk_"):
        source(chunk_size=size, chunk_overlap=overlap)


@pytest.mark.parametrize("path", ["../outside", "missing", "kb/intro.md"])
def test_a_source_path_must_be_a_folder_inside_the_workspace(workspace: Path, path: str) -> None:
    (workspace.parent / "outside").mkdir(exist_ok=True)
    with pytest.raises(KnowledgeSourceError):
        build_chunks(workspace, source(path=path))


def test_a_symlink_out_of_the_folder_is_refused(workspace: Path) -> None:
    secret = workspace.parent / "secret.md"
    secret.write_text("private")
    (workspace / "kb" / "link.md").symlink_to(secret)
    with pytest.raises(KnowledgeSourceError, match="outside"):
        build_chunks(workspace, source())


def test_an_acl_symlink_out_of_the_folder_is_refused(workspace: Path) -> None:
    outside = workspace.parent / "outside.acl.yaml"
    outside.write_text("acl_groups: [everyone]\n")
    (workspace / "kb" / "intro.md.acl.yaml").symlink_to(outside)
    with pytest.raises(KnowledgeSourceError, match="outside"):
        build_chunks(workspace, source())


@pytest.mark.parametrize(
    "content", [b"not a pdf", b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 9 0 R >>\nendobj\n"]
)
def test_a_damaged_pdf_is_a_knowledge_source_error(workspace: Path, content: bytes) -> None:
    (workspace / "kb" / "broken.pdf").write_bytes(content)
    with pytest.raises(KnowledgeSourceError, match=r"broken\.pdf"):
        build_chunks(workspace, source())


def test_an_unreadable_pdf_names_the_file(workspace: Path) -> None:
    (workspace / "kb" / "broken.pdf").write_bytes(b"not a pdf")
    with pytest.raises(KnowledgeSourceError, match=r"broken\.pdf"):
        build_chunks(workspace, source())


# --- acl_groups from a sidecar file or folder convention, stored on every Chunk ---


def acl(workspace: Path, relative: str, *groups: str) -> None:
    listed = ", ".join(groups)
    (workspace / "kb" / relative).write_text(f"acl_groups: [{listed}]\n")


def groups_by_file(workspace: Path) -> dict[str, list[str]]:
    chunks = build_chunks(workspace, source())[1]
    assert chunks
    return {c.source_uri: c.acl_groups for c in chunks}


def test_without_acl_files_every_chunk_gets_the_source_groups(workspace: Path) -> None:
    assert set(map(tuple, groups_by_file(workspace).values())) == {("staff",)}


def test_a_sidecar_file_sets_the_groups_of_its_document(workspace: Path) -> None:
    acl(workspace, "intro.md.acl.yaml", "everyone")
    assert groups_by_file(workspace) == {
        "kb/intro.md": ["everyone"],
        "kb/hr/pay.md": ["staff"],
        "kb/faq.pdf": ["staff"],
    }


def test_a_folder_acl_file_covers_its_folder_and_subfolders(workspace: Path) -> None:
    (workspace / "kb" / "hr" / "deep").mkdir()
    (workspace / "kb" / "hr" / "deep" / "x.md").write_text("deep file")
    acl(workspace, "hr/.acl.yaml", "hr", "hr-leads")
    found = groups_by_file(workspace)
    assert found["kb/hr/pay.md"] == found["kb/hr/deep/x.md"] == ["hr", "hr-leads"]
    assert found["kb/intro.md"] == ["staff"]


def test_the_sidecar_beats_the_folder_file(workspace: Path) -> None:
    acl(workspace, "hr/.acl.yaml", "hr")
    acl(workspace, "hr/pay.md.acl.yaml", "payroll")
    assert groups_by_file(workspace)["kb/hr/pay.md"] == ["payroll"]


@pytest.mark.parametrize(
    "content", ["acl_groups: []\n", "acl_groups: [1, 2]\n", "groups: [a]\n", "- a\n", "a: [\n"]
)
def test_a_bad_acl_file_fails_instead_of_widening_or_hiding(workspace: Path, content: str) -> None:
    (workspace / "kb" / "hr" / ".acl.yaml").write_text(content)
    with pytest.raises(KnowledgeSourceError, match=r"\.acl\.yaml"):
        build_chunks(workspace, source())


def test_acl_files_are_not_loaded_as_documents(workspace: Path) -> None:
    acl(workspace, "intro.md.acl.yaml", "everyone")
    assert {c.source_uri for c in build_chunks(workspace, source())[1]} == {
        "kb/intro.md",
        "kb/hr/pay.md",
        "kb/faq.pdf",
    }


# --- `cograil knowledge sync <workspace>` is idempotent ---


@pytest.fixture
async def store(request: pytest.FixtureRequest) -> AsyncIterator[KnowledgeStore]:
    if request.param == "memory":
        yield InMemoryKnowledgeStore()
        return
    # An async fixture cannot request another async fixture by name, so this one owns its engine.
    pg = PostgresKnowledgeStore.from_url(request.getfixturevalue("migrated_url"))
    yield pg
    await pg.dispose()


every_store = pytest.mark.parametrize(
    "store", ["memory", pytest.param("postgres", marks=pytest.mark.integration)], indirect=True
)


def chunk(chunk_id: str, text: str = "t", groups: tuple[str, ...] = ("staff",)) -> Chunk:
    return Chunk(id=chunk_id, source="s", source_uri="u", text=text, acl_groups=list(groups))


@every_store
async def test_sync_chunks_adds_updates_removes_and_leaves_equals(store: KnowledgeStore) -> None:
    name = f"s-{id(store)}"

    def make(*items: Chunk) -> list[Chunk]:
        return [c.model_copy(update={"source": name, "id": f"{name}:{c.id}"}) for c in items]

    first = make(chunk("a"), chunk("b"), chunk("c"))
    assert await store.sync_chunks(name, first) == SyncCounts(added=3)
    assert await store.sync_chunks(name, first) == SyncCounts(unchanged=3)
    second = make(chunk("a"), chunk("b", "new text"), chunk("c", groups=("hr",)), chunk("d"))
    assert await store.sync_chunks(name, second) == SyncCounts(1, 2, 0, 1)
    assert await store.sync_chunks(name, second[:1]) == SyncCounts(removed=3, unchanged=1)
    assert await store.list_chunks(name) == second[:1]


@every_store
async def test_sync_chunks_of_one_source_leaves_the_others(store: KnowledgeStore) -> None:
    one, other = f"one-{id(store)}", f"other-{id(store)}"
    keep = Chunk(id=f"{other}:x", source=other, source_uri="u", text="t", acl_groups=["a"])
    await store.sync_chunks(other, [keep])
    await store.sync_chunks(one, [])
    assert await store.list_chunks(other) == [keep]


@pytest.fixture
def shared_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemoryKnowledgeStore]:
    """One store for every CLI call of a test, as Postgres would be across processes."""
    shared = InMemoryKnowledgeStore()

    @asynccontextmanager
    async def open_knowledge_store() -> AsyncIterator[KnowledgeStore]:
        yield shared

    monkeypatch.setattr(cograil.knowledge.cli, "open_knowledge_store", open_knowledge_store)
    yield shared


def sync_cli(path: Path) -> str:
    result = runner.invoke(app, ["knowledge", "sync", str(path)])
    assert result.exit_code == 0, result.output
    return result.output


def chunks_of(store: InMemoryKnowledgeStore, source: str) -> list[Chunk]:
    return asyncio.run(store.list_chunks(source))


@pytest.fixture
def example(tmp_path: Path) -> Path:
    copy = tmp_path / "example-smb"
    shutil.copytree(EXAMPLE, copy)
    return copy


def test_cli_sync_twice_changes_nothing_the_second_time(
    example: Path, shared_store: InMemoryKnowledgeStore
) -> None:
    first = sync_cli(example)
    stored = chunks_of(shared_store, "policy-library")
    assert stored
    assert {tuple(c.acl_groups) for c in stored} == {("all-employees",)}
    assert "added 0" not in first
    second = sync_cli(example)
    assert "added 0, updated 0, removed 0, unchanged" in second
    assert chunks_of(shared_store, "policy-library") == stored


def test_cli_sync_follows_edits_and_deletes(
    example: Path, shared_store: InMemoryKnowledgeStore
) -> None:
    folder = example / "knowledge" / "policies"
    sync_cli(example)
    (folder / "extra.md").write_text("Extra policy.")
    assert "added 1, updated 0, removed 0" in sync_cli(example)
    (folder / "extra.md").write_text("Extra policy, revised.")
    assert "added 0, updated 1, removed 0" in sync_cli(example)
    (folder / "extra.md").unlink()
    assert "added 0, updated 0, removed 1" in sync_cli(example)
    assert {c.source_uri for c in chunks_of(shared_store, "policy-library")} == {
        "knowledge/policies/leave-policy.md"
    }


async def test_sync_source_reports_files_and_chunks(workspace: Path) -> None:
    report = await sync_source(workspace, source(), InMemoryKnowledgeStore())
    assert (report.source, report.files) == ("docs", 3)
    assert report.chunks == report.counts.added > 0


def test_cli_sync_reports_a_bad_source_and_exits_1(
    example: Path, shared_store: InMemoryKnowledgeStore
) -> None:
    (example / "knowledge" / "policies" / ".acl.yaml").write_text("acl_groups: []\n")
    result = runner.invoke(app, ["knowledge", "sync", str(example)])
    assert result.exit_code == 1
    assert ".acl.yaml" in result.output


def test_cli_sync_needs_database_url(example: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = runner.invoke(app, ["knowledge", "sync", str(example)])
    assert result.exit_code == 1
    assert "DATABASE_URL" in result.output
