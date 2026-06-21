# Five Minutes to an MCP Service: NexusX vs Hand-Written FastMCP

> For developers who already have SQLModel entities lying around. Goal: expose
> the database to AI agents like Claude / Cursor.
>
> This repo is a runnable side-by-side demo — all three paths have real code,
> each server starts independently.

**中文版**: [README.md](./README.md)

## Quick start

```bash
git clone https://github.com/allmonday/nexusx-vs-fastmcp.git
cd nexusx-vs-fastmcp

uv sync                              # or: pip install -e .

python init_db.py                    # one-shot: create schema + seed (all three DBs)

# Pick a path and start:
python fastmcp_handwritten.py        # Path A: hand-written FastMCP (stdio)
python nexusx_simple.py              # Path B1: NexusX simple (stdio)
python nexusx_usecase.py             # Path B2: NexusX UseCase (stdio)

# Append --http to switch to HTTP mode on ports 9001 / 9002 / 9003:
python nexusx_usecase.py --http
```

stdio mode plugs into Claude Desktop / Cursor. HTTP mode is handy for `curl`
probes or browser-based MCP clients during development.

---

## TL;DR

Same requirement — "let an AI agent query and create Users and Posts" — three paths:

| | Hand-written FastMCP | NexusX simple | NexusX UseCase |
|---|---|---|---|
| What you produce | 7 flat tools (3 per entity + 1 relationship-compensation tool) | 1 GraphQL endpoint + 3 MCP tools | 4-layer progressive-disclosure MCP tools |
| Nested query (`posts { author { name } }`) | Must write a separate `list_posts_with_author` tool | Direct GraphQL nesting, DataLoader prevents N+1 | Business-method composition, DTOs decide shape |
| Agent-side token cost | Reads all 7 tool descriptions upfront | 3 tools, schema drilled into on demand | 4 tools, three layers of drill-down |
| REST API co-existence | Write a separate set of FastAPI routes | Same — write it again | The same `UseCaseService` mounts to FastAPI |

The three paths are written out in full below. Every code snippet maps to a real file in this repo.

---

## Shared starting point

All three paths share the same entity definitions (`User` + `Post` in a one-to-many
relationship) and the same seed data. The only difference is *how* they become an
MCP service.

Entity fields:

```python
class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str
    posts: list["Post"] = Relationship(back_populates="author")


class Post(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    content: str
    author_id: int = Field(foreign_key="user.id")
    author: Optional["User"] = Relationship(back_populates="posts")
```

Seed data: 2 users (Alice, Bob) and 3 posts.

---

## Path A: hand-written FastMCP (10+ minutes)

> Full code: [`fastmcp_handwritten.py`](./fastmcp_handwritten.py)

FastMCP is the de facto standard for turning Python functions into MCP tools. One
`@mcp.tool` decorator per tool.

For fairness, this path uses FastMCP's **best practice**: arguments and return
values are pydantic `BaseModel`s. FastMCP auto-generates `inputSchema` /
`outputSchema` from the type annotations (including `Field(description=...)`
metadata). FastMCP is genuinely good at this — it's not where NexusX differs.

```python
from fastmcp import FastMCP
from pydantic import BaseModel, Field
from sqlalchemy.orm import selectinload
from sqlmodel import select


# ─── pydantic I/O models ───
class UserCreate(BaseModel):
    """Payload to create a user."""
    name: str = Field(..., description="User's display name")
    email: str = Field(..., description="Reachable email")


class UserOut(BaseModel):
    """A user record."""
    id: int
    name: str
    email: str


class PostWithAuthor(BaseModel):
    """A post with nested author info.

    This shape exists *only* because the flat-tool model can't express nested
    field selection. Every new nesting needs another tool + another Pydantic model.
    """
    id: int
    title: str
    author: UserOut


mcp = FastMCP("Blog")


@mcp.tool
async def get_user(user_id: int) -> UserOut | None:
    """Get a user by ID."""
    async with session() as s:
        user = await s.get(User, user_id)
        if not user:
            return None
        return UserOut(id=user.id, name=user.name, email=user.email)


@mcp.tool
async def list_users(limit: int = 20) -> list[UserOut]:
    """List users."""
    async with session() as s:
        rows = (await s.exec(select(User).limit(limit))).all()
        return [UserOut(id=u.id, name=u.name, email=u.email) for u in rows]


@mcp.tool
async def create_user(payload: UserCreate) -> UserOut:
    """Create a user from a pydantic payload."""
    # ... insert + return UserOut


# get_post / list_posts / create_post follow the same shape ...


@mcp.tool
async def list_posts_with_author(limit: int = 20) -> list[PostWithAuthor]:
    """List posts with their author (avoids N+1 via selectinload).

    Every new nesting shape needs another tool + another Pydantic model.
    """
    async with session() as s:
        stmt = select(Post).options(selectinload(Post.author)).limit(limit)
        rows = (await s.exec(stmt)).all()
        return [
            PostWithAuthor(
                id=p.id,
                title=p.title,
                author=UserOut(id=p.author.id, name=p.author.name, email=p.author.email),
            )
            for p in rows
        ]
```

**What you actually produce**: 7 tools (`get_user`, `list_users`, `create_user`,
`get_post`, `list_posts`, `create_post`, `list_posts_with_author`). FastMCP
generates `inputSchema` + `outputSchema` for each; the docstring becomes the tool
description. That part is free.

### Pain points that remain

1. **N× work per entity**: 3 tools for User, 3 for Post... tool count grows
   linearly with entity count. Schema automation removed the field definitions
   but you still hand-write each tool.
2. **Nested data needs its own tool**: "posts with their author" requires a
   hand-written `list_posts_with_author` + a `PostWithAuthor` model + an explicit
   `selectinload`. One more level of nesting (`posts { author { comments } }`)
   means another tool and another model.
3. **Tool wall**: The agent has to load all 7 tool schemas before it can do
   anything — even if this task only needs `list_posts`. Schema automation makes
   each description *more* detailed, so token cost goes up, not down. FastMCP
   supports `tools/list` pagination (cursor-based), which helps discovery latency
   but doesn't reduce total schema tokens when the agent actually invokes a tool.
4. **Over-fetch / no field projection**: MCP's `tools/call` has no
   "just-these-fields" parameter. An agent calling `get_user` must accept the
   entire `UserOut` (id + name + email returned in full), even if it only cares
   about `name`. The more fields on the pydantic model, the worse the waste —
   single-call response tokens scale linearly with model width.
5. **No composed queries**: "Give me the user list AND the post list" means two
   round trips.
6. **REST isn't free**: The same business logic exposed to a web frontend needs a
   separate set of FastAPI routes (pydantic models can be reused, endpoints can't).

---

## Path B1: NexusX Simple (30 seconds)

> Full code: [`nexusx_simple.py`](./nexusx_simple.py)

NexusX's premise: **`@query` / `@mutation` methods on the entity *are* GraphQL
fields; MCP wraps the entire GraphQL endpoint automatically.**

```python
from nexusx import mutation, query
from nexusx.mcp import create_simple_mcp_server


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str
    posts: list["Post"] = Relationship(back_populates="author")

    @query
    async def get_users(cls, limit: int = 20) -> list["User"]:
        """List users."""
        async with async_session() as s:
            return list((await s.exec(select(cls).limit(limit))).all())

    @query
    async def get_user(cls, id: int) -> Optional["User"]:
        """Get a user by ID."""
        async with async_session() as s:
            return await s.get(cls, id)

    @mutation
    async def create_user(cls, name: str, email: str) -> "User":
        """Create a user."""
        async with async_session() as s:
            user = cls(name=name, email=email)
            s.add(user)
            await s.commit()
            await s.refresh(user)
            return user


# Post class is parallel: get_posts / get_post / create_post


# ─── one line to become an MCP service ───
mcp = create_simple_mcp_server(
    base=SQLModel,
    name="Blog (NexusX Simple)",
    session_factory=async_session,
    allow_mutation=True,
)
```

**Business code is roughly the same as Path A** — you'd be writing "how to query
users, how to create posts" anyway. NexusX doesn't magically remove that.

The difference is what comes *before* the last line:

| What Path A makes you handle | What Path B1 doesn't |
|---|---|
| `@mcp.tool` decorator × N | automatic |
| One pydantic I/O model per entity (schema is free, the model isn't) | GraphQL SDL generated from SQLModel metadata |
| `list_posts_with_author` style nested tool + nested pydantic model | Plain GraphQL nested query |
| `selectinload` to avoid N+1 | DataLoader batches automatically |
| Tool count explosion as entities grow | Fixed at 3 tools |

### What the agent sees

Path A: the agent reads 7 tool descriptions on startup.

Path B1: the agent reads:

```
- get_schema()           → { sdl }
- graphql_query(query)   → { data }
- graphql_mutation(...)  → { data }
```

The agent calls `get_schema` on demand to learn capabilities, then issues one
GraphQL query. Composed queries and nested fields resolve in a single round trip:

```graphql
{
  userGetUser(id: 1) {
    id
    name
    posts {
      id
      title
      author { name }   /* cross-relation back-reference, DataLoader batches */
    }
  }
}
```

To do the same in Path A, the agent makes 3 tool calls (`get_user` →
`list_posts_with_author` → manual assembly), or you pre-write a
`get_user_with_posts_and_author_comments` tool — and then the next slightly
different query needs yet another tool.

---

## Path B2: NexusX UseCase + 4-layer progressive disclosure (2 more minutes)

> Full code: [`nexusx_usecase.py`](./nexusx_usecase.py)

When you want to expose **business methods** (not raw CRUD) — things like
`list_users_with_post_counts` that carry derived fields — the UseCase pattern
shines.

```python
from pydantic import BaseModel
from nexusx import UseCaseAppConfig, UseCaseService, create_use_case_graphql_mcp_server


class UserSummary(BaseModel):
    id: int
    name: str
    email: str


class UserWithPostCount(BaseModel):
    """Derived view — business-level shape, not a 1:1 row mapping."""
    id: int
    name: str
    post_count: int


class UserService(UseCaseService):
    """User operations."""

    @classmethod
    async def list_users(cls) -> list[UserSummary]:
        """List all users."""
        async with async_session() as s:
            rows = (await s.exec(select(User))).all()
        return [UserSummary(id=u.id, name=u.name, email=u.email) for u in rows]

    @classmethod
    async def list_users_with_post_counts(cls) -> list[UserWithPostCount]:
        """List users with their post counts (derived)."""
        from sqlalchemy import func

        async with async_session() as s:
            stmt = (
                select(User.id, User.name, func.count(Post.id).label("post_count"))
                .join(Post, Post.author_id == User.id, isouter=True)
                .group_by(User.id)
            )
            rows = (await s.exec(stmt)).all()
        return [
            UserWithPostCount(id=r.id, name=r.name, post_count=r.post_count)
            for r in rows
        ]


mcp = create_use_case_graphql_mcp_server(
    apps=[
        UseCaseAppConfig(
            name="blog",
            services=[UserService, PostService],
            description="Blog with business-level methods",
        ),
    ],
    name="Blog (NexusX UseCase)",
)
```

The MCP service now exposes 4 layered tools:

| Tool | Purpose | Response envelope |
|---|---|---|
| `list_apps()` | Discover available apps | tiny |
| `describe_compose_schema(app)` | List service + method names (no params) | small |
| `describe_compose_method(app, svc, method)` | Params + return type + SDL fragment for one method | medium |
| `compose_query(app, query)` | Execute a GraphQL string | sized by the query |

### This is the biggest structural difference vs FastMCP

- **FastMCP is a flat tool wall** — the agent loads every tool description into
  context at startup.
- **NexusX is a progressive disclosure tree** — the agent checks `list_apps` to
  decide whether to continue, then `describe_compose_schema` for method names,
  then `describe_compose_method` for the exact schema it needs.

The gap widens as entities and business methods multiply. 30+ tools is normal in
a FastMCP project; the agent's context gets eaten by tool descriptions. NexusX
stays at 4 tools regardless.

And **the same `UserService` subclass mounts directly onto FastAPI**:

```python
from nexusx import create_use_case_router

router = create_use_case_router(
    apps=[UseCaseAppConfig(name="blog", services=[UserService])],
)
# Attach to a FastAPI app — OpenAPI docs come for free.
```

Write the business logic once; deliver to both MCP and REST. Path A would need to
re-wrap every tool function as a FastAPI endpoint by hand.

---

## Quantitative comparison

| Dimension | Hand-written FastMCP | NexusX simple | NexusX UseCase |
|---|---|---|---|
| inputSchema / outputSchema auto-generation | ✅ (from pydantic models) | ✅ (from SQLModel metadata → GraphQL SDL) | ✅ (from service method signatures → GraphQL SDL) |
| Boilerplate per entity | N tool functions + N pydantic models | 0 lines (one `create_simple_mcp_server`) | 0 lines (one `create_use_case_graphql_mcp_server`) |
| Nested relationship queries | Hand-written tool + nested model + `selectinload` | GraphQL field nesting, DataLoader auto | Business-method composition |
| Tool-count growth | Linear (+N per entity) | Constant (fixed 3) | Constant (fixed 4) |
| Schema loading at agent startup | All tool descriptions (more detail = more tokens) | 1 `get_schema` tool, drill-down on demand | 3-layer drill-down (apps → schema → method) |
| Return-field projection (anti over-fetch) | ❌ tool returns the full pydantic model; agent can't pick fields | ✅ GraphQL field selection | ✅ GraphQL field selection |
| Composed queries (multi-entity, one round trip) | Not supported | Native GraphQL | Native GraphQL |
| REST API co-existence | pydantic models reusable, endpoints must be rewritten | Write FastAPI separately | `UseCaseService` mounts directly |
| N+1 protection | Manual `selectinload` | DataLoader auto-batching | DataLoader auto-batching |
| Tool safety annotations | ✅ `readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint` help the agent decide whether a call is safe | ❌ GraphQL schema has no equivalent | ❌ same |
| MCP Resources (URI-addressable resources) | ✅ `@mcp.resource("config://...")` exposes config / files / docs | ❌ tools only | ❌ tools only |
| MCP Prompts (predefined prompt templates) | ✅ `@mcp.prompt` gives the agent structured prompts | ❌ tools only | ❌ tools only |

---

## Design philosophy: AI agent as yet another frontend client

Look back at FastMCP's pain points in the comparison table — over-fetch, no field
selection, no composed queries, schema tokens growing linearly with tool count.
Frontend engineers will find these very familiar. This is exactly what REST + BFF
teams argued about daily around 2015. GraphQL was invented to solve precisely
these: field selection, nested queries, strongly-typed contracts, composition on
demand.

Agents are now hitting the same wall, just renamed to "the tools/list token budget."

NexusX isn't "inventing a new MCP framework." It treats **SQLModel entities as the
single source of truth for a GraphQL schema, and lets the agent consume every
capability via GraphQL-over-MCP.** What makes one line of `create_simple_mcp_server`
work is this inheritance: the agent doesn't get "a new protocol designed for AI,"
it gets "a protocol designed for frontends, validated by billions of production
requests."

This abstraction pays out at three levels:

1. **The agent's capability ceiling goes up.** Composed queries, field projection,
   nested relationships in one round trip — all GraphQL dividends the agent now
   inherits for free.
2. **Existing SQLModel projects adopt it almost for free.** You already have
   entities; a handful of `@query` decorators gets you MCP. Path A would require
   rewriting the entire tool layer.
3. **`UseCaseService` makes REST free alongside MCP.** Write the business logic
   once; deliver to both agent and frontend.

The load-bearing assumption: **data has structure and relationships are
traversable.** If the agent's job isn't querying a database — it's sending emails,
resizing images, calling external APIs — GraphQL abstraction is a hindrance and
FastMCP's "tool = function" model fits better. The next section expands on the
boundaries.

---

## Opinionated GraphQL: trading freedom for predictability

GraphQL itself has a long-standing critique: **too flexible, not enough constraint.**

The community has never agreed on schema style. Apollo pushes schema-first +
business-aligned types. Early Facebook organized schemas around UI components.
Most teams end up mapping ORM models directly to GraphQL types. Three styles in
the same schema, and it starts to rot. The root cause: GraphQL specifies
**syntax**, not **style** — "can't find the best practice" isn't lack of effort,
it's the protocol giving no constraints.

NexusX adds an opinionated framework constraint at this layer. Two API paths,
sharply separated:

- **B1 (simple mode) = strictly model-oriented.** Schema is auto-generated from
  SQLModel metadata; you can't sneak in ad-hoc fields. It's "automated ORM →
  GraphQL mapping," but because the framework generates it, there's no "should
  the team do this?" debate.
- **B2 (UseCase mode) = strictly business-oriented.** Schema must hang off
  `UseCaseService` methods, organized by business use case. No business method,
  no field. This turns the business-aligned style championed by Facebook / Apollo
  into **the only code path.**

**Users can't drift** — it's not "find the best practice," it's "the framework
already picked one." GraphQL's query capabilities (field selection, nesting,
composition) are preserved at the protocol layer, but schema-design freedom is
removed. This is the opposite direction from Strawberry / Ariadne, which hand
you a GraphQL endpoint and let you design the schema yourself.

One sentence: **NexusX isn't a better GraphQL framework, it's a more restrictive
one — and the restriction is exactly how it solves "can't find best practices."**

This also draws its applicability boundary: if you need schema-design freedom
(multi-team collaboration, complex federated schemas, cross-domain type
coordination), NexusX is a hindrance. The next section expands.

---

## Honest boundaries

NexusX isn't the right fit for every scenario.

**Use NexusX when**:

- The project is already on SQLModel / SQLAlchemy.
- You need to expose database entities or business methods to an AI agent.
- REST and MCP both need to be delivered (→ UseCase mode).
- Many entities, complex relationships, frequent nested queries.

**Keep using FastMCP when**:

- MCP tools are stateless functions — `send_email(to, subject)`,
  `resize_image(url, width)`, unrelated to a database.
- Tool count is small (< 5) and won't grow.
- You're outside the Python ecosystem (FastMCP has TypeScript / Go versions).
- You need precise control over each tool's description / argument order, and
  GraphQL abstraction is in the way.
- **You need the full MCP triangle** (tools + resources + prompts). FastMCP is a
  complete MCP server framework; NexusX focuses on exposing SQLModel / business
  methods as tools (GraphQL-over-MCP) and doesn't cover resources or prompts.

### FastMCP advantages outside this comparison's scope

For fairness, these are real FastMCP capabilities that are unrelated (or weakly
related) to the core "expose SQLModel entities" scenario:

- **Tool annotations**: `readOnlyHint` / `destructiveHint` / `idempotentHint` /
  `openWorldHint` — hints the agent uses to judge whether a call is safe ("read-only
  tool, safe to try"). GraphQL schemas have no equivalent.
- **MCP Resources**: `@mcp.resource("config://...")` exposes URI-addressable
  resources (configs, docs, files) — a different access pattern from tools.
- **MCP Prompts**: `@mcp.prompt` defines reusable prompt templates the agent can
  consume as structured prompts.
- **Context injection + Lifespan**: FastMCP tools can inject `Context` (progress,
  logging, state); the server has lifespan hooks for startup/shutdown.
- **Server composition**: `mcp.import_server("prefix", other_mcp)` combines
  multiple independent servers.
- **In-process / direct call**: FastMCP tools can be invoked as plain Python
  functions (via `Client`, skipping the protocol layer) — handy for integration
  tests.
- **Auth integration**: FastMCP has built-in OAuth / bearer-token handling.

If your needs go beyond "expose the database to an agent" — say, mixing tools +
resources + prompts — FastMCP is the better foundation.

---

## Five-minute checklist

```bash
# Step 1: install (30 seconds)
git clone https://github.com/allmonday/nexusx-vs-fastmcp.git
cd nexusx-vs-fastmcp
uv sync
```

```python
# Step 2: reuse your existing SQLModel entities
# Add @query / @mutation methods to each entity (business code, not MCP boilerplate)
# Or organize them as UseCaseService subclasses
```

```python
# Step 3: create the MCP service (5 seconds)
from nexusx.mcp import create_simple_mcp_server

mcp = create_simple_mcp_server(
    base=SQLModel,
    name="My API",
    session_factory=async_session,
)
```

```python
# Step 4: run
mcp.run()                                       # stdio, for Claude Desktop / Cursor
# mcp.http_app(transport="streamable-http")     # HTTP, for web agents (see each server's --http branch)
```

```json
// Step 5: hook into Claude Desktop (~/.config/claude/claude_desktop_config.json)
{
  "mcpServers": {
    "blog": {
      "command": "python",
      "args": ["/path/to/nexusx_simple.py"]
    }
  }
}
```

Restart Claude Desktop and ask in chat: "list all users with their most recent
post." The agent will discover the schema, construct the GraphQL query, and fetch
the result in one round trip.

---

## Project layout

```
nexusx-vs-fastmcp/
├── README.md                    # Chinese version (primary)
├── README.en.md                 # English version (this file)
├── pyproject.toml               # uv-managed (nexusx pulled from git)
├── init_db.py                   # one-shot schema + seed for all three DBs
├── fastmcp_handwritten.py       # Path A: 7 @mcp.tool functions
├── nexusx_simple.py             # Path B1: @query/@mutation + create_simple_mcp_server
└── nexusx_usecase.py            # Path B2: UseCaseService + 4-layer progressive disclosure
```

The three servers use independent sqlite files (`blog_fastmcp.db` /
`blog_nexusx_simple.db` / `blog_nexusx_usecase.db`). They don't interfere and
can run concurrently.

---

## Further reading

- [NexusX MCP service docs](https://github.com/allmonday/nexusx/blob/main/docs/advanced/mcp_service.zh.md) — full parameters for `create_simple_mcp_server` and `create_mcp_server` (multi-app)
- [NexusX UseCase service docs](https://github.com/allmonday/nexusx/blob/main/docs/advanced/use_case_service.zh.md) — 4-layer progressive-disclosure MCP + FastAPI co-existence
- [NexusX GraphQL mode](https://github.com/allmonday/nexusx/blob/main/docs/guide/graphql_mode.zh.md) — the GraphQL API that MCP uses under the hood
- [NexusX main project](https://github.com/allmonday/nexusx) — auto-generate GraphQL + Core API + MCP from SQLModel classes
