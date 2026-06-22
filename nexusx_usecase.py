"""Path B2: NexusX UseCase MCP server with 4-layer progressive disclosure.

Business methods live on a ``UseCaseService`` subclass — ``@query`` / ``@mutation``
decorated async functions. ``create_use_case_graphql_mcp_server`` derives a GraphQL
schema from the service signatures and exposes it via 4 MCP tools:

    - list_apps()                              → discover apps
    - describe_compose_schema(app)             → service + method names (compact)
    - describe_compose_method(app, svc, method)→ params, return type, SDL fragment
    - compose_query(app, query)                → execute GraphQL

The agent picks up a tiny schema (4 tools) and drills down on demand — instead of
seeing a flat wall of N tools per entity.

Run:
    python nexusx_usecase.py             # stdio
    python nexusx_usecase.py --http      # streamable-http on :9003
"""

import asyncio
import sys
from typing import Optional

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Field, Relationship, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from nexusx import (
    DefineSubset,
    ErManager,
    SubsetConfig,
    UseCaseAppConfig,
    UseCaseService,
    build_dto_select,
    create_use_case_graphql_mcp_server,
    query,
)

DATABASE_URL = "sqlite+aiosqlite:///./blog_nexusx_usecase.db"
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ──────────────────────────── Entities ────────────────────────────


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    email: str
    posts: list["Post"] = Relationship(back_populates="author")


class Post(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    content: str
    author_id: int = Field(foreign_key="user.id")
    author: Optional["User"] = Relationship(back_populates="posts")


# ──────────────────────────── DTOs ────────────────────────────


class PostSummary(DefineSubset):
    __subset__ = SubsetConfig(kls=Post, fields=["id", "title", "author_id"])


class UserSummary(DefineSubset):
    __subset__ = SubsetConfig(kls=User, fields=["id", "name", "email"])


class UserWithPostCount(DefineSubset):
    """Derived view — business-level shape, not a 1:1 row mapping."""

    __subset__ = SubsetConfig(kls=User, fields=["id", "name"])

    posts: list[PostSummary] = []
    post_count: int = 0

    def post_post_count(self):
        return len(self.posts)


class PostWithAuthor(DefineSubset):
    __subset__ = SubsetConfig(kls=Post, fields=["id", "title", "author_id"])

    author: Optional[UserSummary] = None


# ──────────────────────────── ErManager ────────────────────────────


er = ErManager(entities=[User, Post], session_factory=async_session)
Resolver = er.create_resolver()


# ──────────────────────────── Services ────────────────────────────


class UserService(UseCaseService):
    """User operations."""

    @query
    async def list_users(cls) -> list[UserSummary]:
        """List all users."""
        stmt = build_dto_select(UserSummary)
        async with async_session() as s:
            rows = (await s.exec(stmt)).all()
        dtos = [UserSummary(**dict(r._mapping)) for r in rows]
        return await Resolver().resolve(dtos)

    @query
    async def get_user(cls, user_id: int) -> Optional[UserSummary]:
        """Get a user by ID."""
        stmt = build_dto_select(UserSummary, where=User.id == user_id)
        async with async_session() as s:
            rows = (await s.exec(stmt)).all()
        if not rows:
            return None
        return await Resolver().resolve(UserSummary(**dict(rows[0]._mapping)))

    @query
    async def list_users_with_post_counts(cls) -> list[UserWithPostCount]:
        """List users with their post counts (derived)."""
        stmt = build_dto_select(UserWithPostCount)
        async with async_session() as s:
            rows = (await s.exec(stmt)).all()
        dtos = [UserWithPostCount(**dict(r._mapping)) for r in rows]
        return await Resolver().resolve(dtos)


class PostService(UseCaseService):
    """Post operations."""

    @query
    async def list_posts(cls) -> list[PostSummary]:
        """List all posts."""
        stmt = build_dto_select(PostSummary)
        async with async_session() as s:
            rows = (await s.exec(stmt)).all()
        dtos = [PostSummary(**dict(r._mapping)) for r in rows]
        return await Resolver().resolve(dtos)

    @query
    async def list_posts_with_author(cls) -> list[PostWithAuthor]:
        """List posts with their author — auto-loaded by the Resolver."""
        stmt = build_dto_select(PostWithAuthor)
        async with async_session() as s:
            rows = (await s.exec(stmt)).all()
        dtos = [PostWithAuthor(**dict(r._mapping)) for r in rows]
        return await Resolver().resolve(dtos)


# ──────────────────────────── MCP server ────────────────────────────


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


# ──────────────────────────── Bootstrap ────────────────────────────


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    async with async_session() as s:
        if (await s.exec(select(User))).first():
            return
        alice = User(name="Alice", email="alice@example.com")
        bob = User(name="Bob", email="bob@example.com")
        s.add(alice)
        s.add(bob)
        await s.flush()
        s.add(Post(title="Hello World", content="First post", author_id=alice.id))
        s.add(Post(title="GraphQL Tips", content="Use DataLoader", author_id=alice.id))
        s.add(Post(title="MCP Notes", content="Progressive disclosure", author_id=bob.id))
        await s.commit()


def main_stdio() -> None:
    asyncio.run(init_db())
    mcp.run()


def main_http() -> None:
    import asyncio

    from starlette.middleware.cors import CORSMiddleware

    asyncio.run(init_db())

    app = mcp.http_app(transport="streamable-http", stateless_http=True)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9003)


if __name__ == "__main__":
    if "--http" in sys.argv:
        main_http()
    else:
        main_stdio()
