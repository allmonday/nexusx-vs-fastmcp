# GraphQL-based MCP: Expose Database and Business API to AI Agents

The past year turned MCP (Model Context Protocol) from an Anthropic-internal
spec into a de facto standard. Claude Desktop, Cursor, Claude Code, Cline, and
Codex all support it; a thousand-plus community servers now hand agents
databases, file systems, SaaS APIs, and internal tools. Being able to expose
backends to LLMs at this scale is a real step forward for the agent ecosystem.

But MCP's default tool model — each capability is a standalone function with
fixed arguments and a fixed return shape — starts to strain when the thing
being exposed is **structured data**. A `User` has `Post`s, a `Post` has an
`Author`, an `Author` has `Post`s again: a two-entity toy model already has
dozens of query shapes an agent might ask for. flat-tool MCP asks you to
anticipate each one and pre-write a tool function plus a pydantic I/O model.
That's structural — a property of the *tool = function* contract meeting
*data = graph* — not an implementation bug.

Tools are fixed-shape functions; data is a many-shape graph — this mismatch
isn't new. Frontend engineers hit the same wall in the REST + BFF era around
2015; the answer then was GraphQL: field projection solves over-fetch,
nested resolution solves N+1, strongly-typed schemas solve contract drift.
A decade of accumulated practice — DataLoader, depth limiting, cost
analysis, persisted queries — is already mature and ready to use.

GraphQL-based MCP hands this infrastructure to the agent — but **without
making you stand up your own GraphQL server, hand-write a pile of resolvers,
or take on Apollo / Strawberry-grade operational burden**. The developer
just declares database models (or business methods) as a GraphQL schema; the
framework wraps that schema into MCP tools automatically. The agent inherits
GraphQL's full query power; the developer gets the simplicity of *agent
writes one query, fetches everything in one round trip* — not the burden of
*yet another GraphQL service to maintain*.

One caveat worth flagging. NexusX's GraphQL is not the full spec. Features
designed for human developers — aliases, fragments — are stripped out. The
agent never writes them, and dropping them keeps the schema smaller and the
token cost lower. In other words, the agent gets a GraphQL trimmed for its
needs, not the full stack a frontend team would use.

Picture a concrete task: an AI agent needs to answer *"show me Alice's posts
and who wrote them."* Under flat-tool MCP — the style [FastMCP](https://github.com/jlowin/fastmcp)
popularized — that's three tool calls plus a hand-written glue function the
author had to anticipate. Under GraphQL-based MCP, it's one query the agent
writes itself.

This repo is a runnable side-by-side of both paradigms. The same `User` + `Post`
entities, the same seed data, three full server implementations:

- **Path A** — hand-written [FastMCP](https://github.com/jlowin/fastmcp): one
  `@mcp.tool` per action.
- **Path B1** — [NexusX](https://github.com/allmonday/nexusx) Simple:
  `@query` / `@mutation` methods on the entity, one line to spin up MCP.
- **Path B2** — NexusX UseCase: business-method-shaped MCP with four-layer
  progressive disclosure.

What follows isn't a feature checklist. It's a walk through what each paradigm
*feels like to write* and what the agent *actually sees on the wire*.

**中文版**: [README.zh.md](./README.zh.md)

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

stdio plugs into Claude Desktop / Cursor. HTTP is handy for `curl` probes or
browser-based MCP clients during development.

## The task we'll keep coming back to

Two SQLModel entities, a one-to-many relationship, and an agent that needs to
navigate between them.

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

Seed data: two users (Alice, Bob) and three posts. Everything that follows is
about *how each paradigm lets the agent move across this little graph* — and how
much code you write to make that movement possible.

## Path A: hand-writing FastMCP

> Full code: [`fastmcp_handwritten.py`](./fastmcp_handwritten.py)

Most Python MCP projects start here. One `@mcp.tool` decorator per action,
pydantic models for I/O so `inputSchema` / `outputSchema` come out clean.
FastMCP is genuinely good at this part — schema generation isn't where NexusX
differs.

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

Two entities, seven tools. That's where the friction starts to show — and it
shows fast.

**The tool wall shows up early.** Three tools per entity is the floor. By the
time you have ten entities you're shipping thirty-plus tool descriptions.
Eager-load clients — Cursor is the main example today — read all of them into
context on startup, whether or not this particular task needs them. Recent
Claude Code (and the new Claude Desktop) builds sidestep most of this via
**Tool Search** — only the tool *name* goes into context, the schema is
fetched per call — but the protocol itself has no built-in notion of
progressive disclosure, only well-behaved clients. (Even then, both Claude
Code and Codex have unfixed bugs around following `tools/list` pagination.)
Each tool description is also more verbose than you'd expect, because schema
automation pulls in every `Field(description=...)` and nested type. Token cost
goes up, not down.

**Nested data needs its own tool.** *"Posts with their author"* requires
`list_posts_with_author`, plus a `PostWithAuthor` pydantic model, plus an
explicit `selectinload`. One more level of nesting — say
`posts { author { comments } }` — means another tool and another model. Every
shape the agent might want has to be anticipated and pre-written.

**Over-fetch is structural.** MCP's `tools/call` has no "just these fields"
parameter. An agent calling `get_user` accepts the whole `UserOut` whether it
wants two fields or twelve. The wider the model, the more tokens per response.

**Composed queries cost round trips.** *"Give me the user list AND the post
list"* means two tool calls. The MCP spec removed JSON-RPC batch support in
the 2025-06-18 revision (too fragile; complexity outweighed the benefit), so
there's no longer a protocol-level way to collapse them either. Any *nested*
composition (`users { posts { author } }`) requires a bespoke tool.

**REST used to mean rewriting — now less so.** FastMCP 3.0 added
`FastMCP.from_fastapi(app)`, which auto-promotes existing FastAPI routes to
MCP tools, and you can mount an MCP server into a FastAPI ASGI app for the
reverse direction. If you start from FastAPI, REST + MCP co-existence is
largely solved. The structural caveat is that Path A's pattern — writing
`@mcp.tool` functions directly, without an underlying FastAPI app — doesn't
get the bridge automatically; you'd need to refactor to "FastAPI first, then
expose via MCP" to inherit it. NexusX's `UseCaseService` (Path B2) approaches
from the other direction: write the business method once, mount it to both
MCP and FastAPI.

None of this is a FastMCP bug. It's the shape of the contract — *tool =
function* — meeting *data = graph*. The shape of the problem forces the shape of
the pain.

## Path B1: NexusX Simple

> Full code: [`nexusx_simple.py`](./nexusx_simple.py)

NexusX flips the premise. Instead of writing tools, you write queries. The
`@query` / `@mutation` methods on the entity *are* the GraphQL fields; MCP
wraps the whole GraphQL endpoint in one line.

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

The business code is roughly the same as Path A — you'd be writing *"how to
query users, how to create posts"* anyway. NexusX doesn't magically remove that.

What's different is everything before the last line. No `@mcp.tool` decorators
multiplied by N. No pydantic I/O models per entity — the SDL is generated from
SQLModel metadata. No `list_posts_with_author` — the agent asks for that shape
directly. No `selectinload` — DataLoader batches for you.

The agent now sees three tools instead of seven:

```
- get_schema()           → { sdl }
- graphql_query(query)   → { data }
- graphql_mutation(...)  → { data }
```

It calls `get_schema` once to learn the shape, then writes GraphQL. The earlier
scenario — *"posts with their author"* — collapses into one query:

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

One protocol round trip. The underlying SQL count is comparable to FastMCP's
`selectinload` — both solve N+1 via batching. The difference is the agent got
there with one tool call and a query string it wrote itself, instead of three
tool calls or a pre-written `list_posts_with_posts_and_author_comments` glue
tool.

**An honest caveat on token cost.** `get_schema` returns the full SDL — its
size scales with entity and field count, just like FastMCP's tool descriptions
do. Simple mode doesn't break the linear schema-token curve; it relocates the
cost from MCP tool descriptions to GraphQL SDL. The structural wins in B1 are
field projection and nested resolution, not tool-count reduction. Path B2 is
what actually breaks the curve.

## Path B2: NexusX UseCase — when you want business methods

> Full code: [`nexusx_usecase.py`](./nexusx_usecase.py)

Raw CRUD isn't always what you want to expose. Sometimes the right shape is a
derived view — *"list users with their post counts"* — that doesn't map 1:1 to
a row. That's where the UseCase pattern earns its keep.

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

Now MCP serves four tools — and this is where the structural difference actually
shows up. The agent starts with `list_apps`, a tiny envelope that just says
*what exists*. If it cares, it calls `describe_compose_schema(app)` for method
names (no params yet, still small). When something looks useful, it calls
`describe_compose_method(app, svc, method)` to get exactly that method's
parameters, return type, and SDL fragment. Only then does it call
`compose_query(app, query)` to actually run something.

This is what *progressive disclosure* looks like baked into the protocol itself.
FastMCP offers no equivalent — eager-load clients (Cursor today) put every tool
description into context at startup. Recent Claude Code builds sidestep this
via Tool Search (lazy per-call schema fetch), but that's a client-side
optimization, not a protocol guarantee. NexusX lets the agent drill in from
*"what's available?"* down to *"what does this method take?"*, paying token
cost only for the layer it's actually at. The gap widens as entities and
business methods multiply. Thirty-plus tools is normal in a FastMCP project;
NexusX stays at four regardless.

And the same `UserService` mounts directly onto FastAPI:

```python
from nexusx import create_use_case_router

router = create_use_case_router(
    apps=[UseCaseAppConfig(name="blog", services=[UserService])],
)
# Attach to a FastAPI app — OpenAPI docs come for free.
```

Write the business logic once; deliver to both MCP and REST. (FastMCP 3.0's
`FastMCP.from_fastapi` bridge offers a similar co-existence story from the
opposite direction — start with FastAPI, get MCP for free. The structural
difference is that B2 makes this work *because* of the `UseCaseService`
abstraction, where FastMCP relies on you already having FastAPI routes.)

## Putting them side by side

All three paths auto-generate `inputSchema` / `outputSchema` — that part isn't
different. Where they diverge is boilerplate per entity, query shape, token
cost, and REST co-existence.

Path A charges N tools and N pydantic models per entity, charges a hand-written
tool per nested-query shape, charges a manual `selectinload` per N+1 risk, and
charges a full FastAPI rewrite for REST. Path B1 collapses most of that: zero
boilerplate beyond the business code, nested queries become field selection,
DataLoader handles N+1 automatically. Path B2 adds progressive disclosure on
top — four fixed tools, sub-linear token cost as the agent drills in.

Field projection exists in B1 and B2 because GraphQL natively has it. Path A
can't offer it: `tools/call` returns the whole pydantic model whether the agent
wants two fields or twelve, and the waste scales with model width.

Composed queries in Path A need either multiple round trips or a pre-written
glue tool. In B1 and B2 they're native GraphQL — one protocol round trip, with
comparable SQL count underneath.

REST co-existence is where the field has leveled. FastMCP 3.0's
`FastMCP.from_fastapi(app)` bridges from the REST side — start with FastAPI,
inherit MCP tools for free. NexusX's `UseCaseService` bridges from the MCP
side — start with service methods, mount to both. Either gets you one
codebase, two faces. B2 still has a structural edge in *progressive
disclosure* (above), but REST isn't where it shows up anymore.

## Why this works: the agent is another frontend client

> This section is design opinion, not comparison. It's labeled as such because
> analogy-driven framing shouldn't be read as objective evidence.

Step back and look at Path A's pain points again. Over-fetch. No field
selection. No composed queries. Schema tokens growing linearly with tool count.
Frontend engineers will find these very familiar — this is exactly what REST +
BFF teams argued about daily around 2015. GraphQL was invented to solve
precisely these problems: field selection, nested queries, strongly-typed
contracts, composition on demand.

Agents are now hitting a similar wall, just renamed to *"the tools/list token
budget."*

NexusX isn't inventing a new MCP framework. It treats SQLModel entities as the
single source of truth for a GraphQL schema, and lets the agent consume every
capability via GraphQL-over-MCP. What makes one line of
`create_simple_mcp_server` work is that inheritance: the agent doesn't get *"a
new protocol designed for AI"*, it gets *"a protocol designed for frontends."*

This pays out at three levels. The agent's capability ceiling goes up —
composed queries, field projection, nested relationships in one round trip are
GraphQL dividends inherited for free. Existing SQLModel projects adopt it
almost for free — you already have entities, a handful of `@query` decorators
gets you MCP. And `UseCaseService` makes REST free alongside MCP, because the
same code mounts both places.

The load-bearing assumption is that *data has structure and relationships are
traversable*. If the agent's job isn't querying a database — it's sending
emails, resizing images, calling external APIs — GraphQL abstraction is a
hindrance, and FastMCP's *tool = function* model fits better.

**Where the analogy breaks.** Frontend developers pre-know the query shape;
agents generate query shapes at runtime. Some of GraphQL's production-grade
tooling — persisted queries, operation whitelists, frontend-team-maintained
resolvers — depends on that pre-known shape and doesn't transfer cleanly to
agent traffic. The structural wins transfer; the operational tooling doesn't.

## The opinionated layer: constraints over freedom

GraphQL's long-standing critique is that it specifies *syntax*, not *style*.
The community has never agreed on schema style: Apollo pushes schema-first +
business-aligned types, early Facebook organized schemas around UI components,
most teams end up mapping ORM models directly to GraphQL types. Three styles in
the same schema and it starts to rot. *"Can't find the best practice"* isn't
lack of effort — it's the protocol giving no constraints.

NexusX removes the choice. Two API paths, sharply separated.

B1 (Simple mode) is strictly model-oriented. The schema is auto-generated from
SQLModel metadata; you can't sneak in ad-hoc fields. It's automated ORM →
GraphQL mapping, but because the framework generates it, there's no *"should
the team do this?"* debate.

B2 (UseCase mode) is strictly business-oriented. The schema hangs off
`UseCaseService` methods, organized by business use case — no method, no field.
This turns the business-aligned style championed by Facebook and Apollo into
*the only code path*.

Users can't drift because there's nothing to drift from. GraphQL's query
capabilities — field selection, nesting, composition — are preserved at the
protocol layer, but schema-design freedom is removed. That's the opposite
direction from Strawberry or Ariadne, which hand you a GraphQL endpoint and let
you design the schema yourself.

NexusX isn't a better GraphQL framework. It's a more restrictive one — and the
restriction is exactly how it solves *"can't find best practices."*

## What GraphQL-over-MCP costs you

The comparison above leans GraphQL-favorable on structural dimensions. For
balance, these are the real costs of *agent-generated GraphQL queries* that
flat-tool MCP doesn't pay.

**Query depth becomes a real attack surface — with mature countermeasures.**
GraphQL services traditionally need cost analysis or depth limits
(`user { posts { author { posts { ... }}}}}`) to prevent pathological queries.
With an agent writing queries at runtime, depth is unpredictable. The
countermeasures are well-established (depth limiting, selection-set bounding,
cost analysis), and the GraphQL Foundation chartered an AI Working Group in
October 2025 to tackle exactly this class of risk for LLM-driven traffic.
Flat-tool MCP doesn't have an equivalent because each tool's shape is fixed
at design time — but the cost on the GraphQL side is that the server has to
actively enforce these limits rather than passively inherit safety from
fixed tool shapes.

**B2 carries materially less risk here.** Path B2's schema doesn't expose the
ORM relationship graph directly — it hangs off DTOs returned by
`UseCaseService` methods. Those DTOs are flat pydantic models; they don't
naturally form the `User.posts` ↔ `Post.author` back-reference cycles that
ORM relationships do. If you want nesting, you design it explicitly (e.g.,
`PostSummary.author: UserSummary`) — one-directional, no cycles. In other
words, B2 hands the agent **a unidirectional tree, not a cyclic graph** —
structurally, there's no entry point for `posts.author.posts.author...`
depth explosion. The trade-off is losing B1's "arbitrary nesting" query
power: if you need nesting, you write a composed method yourself.

**Error messages get harder to self-correct.** FastMCP + pydantic produces
field-level validation errors (*"`email`: missing required field"*). GraphQL
parse or validation errors are often structural (*"Expected Name, found `}`"*)
and harder for the agent to recover from.

**Persisted-query tooling needs rethinking, not abandonment.** Production
GraphQL deployments usually cache and rate-limit via persisted queries,
which depend on a fixed query set. Agents generate fresh query strings every
call, so the pure-persisted-query model doesn't transfer. The
community-recommended replacement is a hybrid: dynamic query generation
plus an operation whitelist that gates runtime queries against a known-good
set (Apollo and others document this pattern for agent traffic). The burden
shifts to the server side; it doesn't disappear.

**Schema changes have wider blast radius.** A renamed GraphQL field breaks
every agent query that referenced it. A renamed flat tool only breaks callers
of that one tool. Field-level coupling makes GraphQL schemas more fragile
under agent-driven traffic.

None of this negates the structural wins — field projection, progressive
disclosure, nested composition. But it's the price of moving the query shape
from design time to runtime.

### Where NexusX fits, where FastMCP fits

Reach for NexusX when the project is already on SQLModel / SQLAlchemy, when you
need to expose database entities or business methods to an agent, when REST and
MCP both need to be delivered (UseCase mode), or when entities are many and
relationships complex.

Keep using FastMCP when MCP tools are stateless functions (`send_email`,
`resize_image`) unrelated to a database, when tool count is small and stable,
when you're outside the Python ecosystem, when you need precise control over
each tool's description and argument order, or — importantly — when you need
the **full MCP triangle** of tools + resources + prompts.

FastMCP has real capabilities NexusX doesn't try to cover. Tool annotations
(`readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`) let
the agent judge whether a call is safe. MCP Resources
(`@mcp.resource("config://...")`) expose URI-addressable configs, docs, or
files. MCP Prompts (`@mcp.prompt`) define reusable prompt templates. There's
`Context` injection for progress and logging, lifespan hooks, server
composition, in-process direct calls for tests, and built-in OAuth /
bearer-token auth. If your needs go beyond *"expose the database to an agent"*
— say, mixing tools with resources and prompts — FastMCP is the better
foundation.

## Other GraphQL-based MCP implementations

NexusX isn't the only realization of this paradigm. The core insight — *let
the agent write GraphQL queries instead of calling N flat tools* — works with
several stacks.

**Strawberry + FastMCP** is the closest Python alternative: type-driven schema,
full Federation / Subscription support, but you write DataLoader by hand and
lose the opinionated constraint. **Apollo Server + a thin MCP bridge** suits
Node shops already invested in Apollo, at the cost of an extra network hop and
deployment complexity. **Ariadne + FastMCP** is schema-first, good for teams
that want SDL as a contract, but per-field resolver boilerplate is high.
**Raw `graphql-core` + FastMCP** gives maximum control for maximum boilerplate
— best for experiments or framework building.

The paradigm is bigger than any one implementation. NexusX is the opinionated
variant — SQLModel as single source of truth, two forced schema styles. Other
implementations keep more freedom but lose the constraint that prevents schema
drift.

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

Restart Claude Desktop and ask in chat: *"list all users with their most recent
post."* The agent will discover the schema, construct the GraphQL query, and
fetch the result in one round trip.

## Project layout

```
nexusx-vs-fastmcp/
├── README.md                    # English version (this file)
├── README.zh.md                 # Chinese version
├── pyproject.toml               # uv-managed (nexusx pulled from git)
├── init_db.py                   # one-shot schema + seed for all three DBs
├── fastmcp_handwritten.py       # Path A: 7 @mcp.tool functions
├── nexusx_simple.py             # Path B1: @query/@mutation + create_simple_mcp_server
└── nexusx_usecase.py            # Path B2: UseCaseService + 4-layer progressive discovery
```

The three servers use independent sqlite files (`blog_fastmcp.db` /
`blog_nexusx_simple.db` / `blog_nexusx_usecase.db`). They don't interfere and
can run concurrently.

## Further reading

- [NexusX MCP service docs](https://github.com/allmonday/nexusx/blob/main/docs/advanced/mcp_service.zh.md) — full parameters for `create_simple_mcp_server` and `create_mcp_server` (multi-app)
- [NexusX UseCase service docs](https://github.com/allmonday/nexusx/blob/main/docs/advanced/use_case_service.zh.md) — 4-layer progressive-disclosure MCP + FastAPI co-existence
- [NexusX GraphQL mode](https://github.com/allmonday/nexusx/blob/main/docs/guide/graphql_mode.zh.md) — the GraphQL API that MCP uses under the hood
- [NexusX main project](https://github.com/allmonday/nexusx) — auto-generate GraphQL + Core API + MCP from SQLModel classes
