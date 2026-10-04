"""Knowledge search tests, one per acceptance criterion of issue #23, on an in-memory store
(and Postgres when DATABASE_URL is set): the acl_groups filter comes before ranking, results
cite their source_uri and passage, and a principal never retrieves a Chunk outside its groups."""

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine

from cograil.domain import Chunk, Colleague, KnowledgeSource, Tool, Workspace
from cograil.knowledge import InMemoryKnowledgeStore, KnowledgeStore, PostgresKnowledgeStore
from cograil.knowledge.search import search_statement
from cograil.knowledge.sync import sync_source
from cograil.knowledge.tool import add_knowledge
from cograil.parser import parse_protocol
from cograil.providers import FakeProvider, PlannedToolCall, scripted
from cograil.registry import CallContext, ToolRegistry
from cograil.runner import Runner
from cograil.store import InMemoryRunStore

SEARCH = Tool(
    name="knowledge.search",
    kind="knowledge",
    scope="read",
    args_schema={"type": "object", "properties": {"query": {"type": "string"}}},
)
SALARY = "Salary bands for every grade are reviewed each April by the pay committee."
HOLIDAY = "Annual leave is 25 days; carry over up to 5 days into the next year."


@pytest.fixture
async def kstore(request: pytest.FixtureRequest) -> AsyncIterator[KnowledgeStore]:
    if request.param == "memory":
        yield InMemoryKnowledgeStore()
        return
    pg = PostgresKnowledgeStore.from_url(request.getfixturevalue("migrated_url"))
    yield pg
    await pg.dispose()


every_store = pytest.mark.parametrize(
    "kstore", ["memory", pytest.param("postgres", marks=pytest.mark.integration)], indirect=True
)


async def held(kstore: KnowledgeStore, *chunks: tuple[str, str, list[str]]) -> str:
    """Sync (uri, text, groups) Chunks into a fresh source and return its name."""
    name = f"src-{id(kstore)}-{len(chunks)}"
    await kstore.sync_chunks(name, [
        Chunk(id=f"{name}:{uri}#0", source=name, source_uri=uri, text=body, acl_groups=groups)
        for uri, body, groups in chunks
    ])  # fmt: skip
    return name


def registry_for(run_store: InMemoryRunStore, kstore: KnowledgeStore, source: str) -> ToolRegistry:
    """A registry whose knowledge.search covers `source`, as add_knowledge builds it."""
    workspace = Workspace(name="w", colleagues=[], protocols=[], tools=[SEARCH], knowledge=[
        KnowledgeSource(name=source, path="kb", acl_groups=["staff"])
    ])  # fmt: skip
    registry = ToolRegistry(run_store)
    add_knowledge(registry, workspace, kstore)
    return registry


Search = Callable[[KnowledgeStore, str, list[str], dict[str, Any]], Awaitable[list[Any]]]


@pytest.fixture
def search_as(store: InMemoryRunStore) -> Search:
    """Call the knowledge Tool through the registry, as the runner does, with `groups` on the
    CallContext and `args` from the model; the results are returned."""

    async def search(
        kstore: KnowledgeStore, source: str, groups: list[str], args: dict[str, Any]
    ) -> list[Any]:
        ctx = CallContext("r1", 1, "alice@example.com", tuple(groups))
        result = await registry_for(store, kstore, source).invoke("knowledge.search", args, ctx)
        return list(result["results"])

    return search


# --- The SQL filter on acl_groups is applied before similarity search, never after ---


@every_store
async def test_the_best_match_outside_the_groups_does_not_use_up_the_limit(
    kstore: KnowledgeStore, search_as: Search
) -> None:
    source = await held(
        kstore,
        ("pay.md", SALARY, ["hr"]),
        ("leave.md", HOLIDAY + " Salary is not leave.", ["staff"]),
    )
    found = await search_as(
        kstore, source, ["staff"], {"query": "salary bands pay grade", "limit": 1}
    )
    assert [r["source_uri"] for r in found] == ["leave.md"]  # a post-filter would return []


def test_the_sql_filters_inside_a_materialized_cte_and_ranks_outside_it() -> None:
    sql = str(search_statement([1.0], ["staff"], ["docs"], 3).compile(dialect=postgresql.dialect()))
    inner, outer = sql.split(")\n SELECT")
    assert inner.startswith("WITH allowed AS MATERIALIZED")
    assert "chunks.acl_groups && " in inner and "<=>" not in inner and "LIMIT" not in inner
    assert "FROM allowed ORDER BY distance" in outer and "chunks" not in outer


# --- Results include source_uri and a verbatim passage for citation ---


@every_store
async def test_results_cite_the_source_uri_and_quote_the_passage(
    kstore: KnowledgeStore, search_as: Search
) -> None:
    source = await held(kstore, ("pay.md", SALARY, ["staff"]), ("leave.md", HOLIDAY, ["staff"]))
    found = await search_as(
        kstore, source, ["staff"], {"query": "how many annual leave days are there"}
    )
    assert [r["source_uri"] for r in found] == ["leave.md", "pay.md"]
    assert found[0]["passage"] == HOLIDAY
    assert found[0] == found[0] | {"source": source, "chunk_id": f"{source}:leave.md#0"}
    assert 0 < found[1]["score"] < found[0]["score"] <= 1


# --- A principal cannot retrieve a Chunk outside their groups ---


@every_store
@pytest.mark.parametrize("groups", [["staff"], ["contractors"], []])
async def test_a_principal_cannot_retrieve_a_chunk_outside_their_groups(
    kstore: KnowledgeStore, groups: list[str], search_as: Search
) -> None:
    source = await held(kstore, ("pay.md", SALARY, ["hr"]), ("leave.md", HOLIDAY, ["staff"]))
    forged = {"query": SALARY, "groups": ["hr"], "limit": 20}  # the model cannot widen access
    found = await search_as(kstore, source, groups, forged)
    assert SALARY not in {r["passage"] for r in found}
    assert {r["source_uri"] for r in found} == ({"leave.md"} if groups == ["staff"] else set())


@every_store
async def test_a_sidecar_naming_an_outside_group_does_not_grant_it(
    kstore: KnowledgeStore, tmp_path: Path, search_as: Search
) -> None:
    (tmp_path / "kb").mkdir()
    (tmp_path / "kb" / "pay.md").write_text(SALARY)
    (tmp_path / "kb" / "pay.md.acl.yaml").write_text("acl_groups: [outsiders, hr]\n")
    name = f"docs-{id(kstore)}"
    await sync_source(tmp_path, KnowledgeSource(name=name, path="kb", acl_groups=["hr"]), kstore)
    assert await search_as(kstore, name, ["outsiders"], {"query": SALARY}) == []
    assert [r["source_uri"] for r in await search_as(kstore, name, ["hr"], {"query": SALARY})] == [
        "kb/pay.md"
    ]


async def test_the_runner_searches_as_the_run_principal(store: InMemoryRunStore) -> None:
    kstore = InMemoryKnowledgeStore()
    source = await held(kstore, ("pay.md", SALARY, ["hr"]), ("leave.md", HOLIDAY, ["staff"]))
    registry = registry_for(store, kstore, source)
    search = PlannedToolCall(id="c1", tool="knowledge.search", args={"query": "salary leave"})
    provider = FakeProvider([scripted("", search), scripted("found", done=True)])
    colleague = Colleague(name="harper", role="HR", escalation_contact="x", protocols=["demo"])
    protocol = parse_protocol('Protocol: demo\n1. Step "Search": Use @knowledge.search.')
    await Runner(provider, registry, store, colleague).run("r1", protocol)
    [call] = await store.list_tool_calls("r1")  # alice is in staff, not hr
    assert [r["source_uri"] for r in call.result["results"]] == ["leave.md"]


@pytest.mark.integration
@pytest.mark.parametrize("kstore", ["postgres"], indirect=True)
async def test_a_chunk_stored_without_an_embedding_is_rewritten_by_the_next_sync(
    kstore: KnowledgeStore, migrated_url: str, search_as: Search
) -> None:
    source = await held(kstore, ("leave.md", HOLIDAY, ["staff"]))
    chunks = await kstore.list_chunks(source)
    engine = create_async_engine(migrated_url)
    async with engine.begin() as conn:
        forget = text("UPDATE chunks SET embedding = NULL WHERE source = :s")
        await conn.execute(forget, {"s": source})
    await engine.dispose()
    assert await search_as(kstore, source, ["staff"], {"query": HOLIDAY}) == []
    assert (await kstore.sync_chunks(source, chunks)).updated == 1
    assert len(await search_as(kstore, source, ["staff"], {"query": HOLIDAY})) == 1
