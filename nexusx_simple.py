"""Path B1: NexusX simple MCP server.

The same entities and business logic as Path A, but @query / @mutation methods on
the SQLModel classes become GraphQL fields. ``create_simple_mcp_server`` wraps the
entire GraphQL endpoint in 3 MCP tools:

    - get_schema()           → { sdl }
    - graphql_query(query)   → { data }
    - graphql_mutation(...)  → { data }

Nested data (posts with author, user with posts) is expressed in the GraphQL
query itself, batched via DataLoader. No extra tools needed.

Run:
    python nexusx_simple.py             # stdio
    python nexusx_simple.py --http      # streamable-http on :9002
"""

import asyncio
import sys
from typing import Optional

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Field, Relationship, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from nexusx import mutation, query
from nexusx.mcp import create_simple_mcp_server

DATABASE_URL = "sqlite+aiosqlite:///./blog_nexusx_simple.db"
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
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


class Post(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    content: str
    author_id: int = Field(foreign_key="user.id")
    author: Optional["User"] = Relationship(back_populates="posts")

    @query
    async def get_posts(cls, limit: int = 20) -> list["Post"]:
        """List posts."""
        async with async_session() as s:
            return list((await s.exec(select(cls).limit(limit))).all())

    @query
    async def get_post(cls, id: int) -> Optional["Post"]:
        """Get a post by ID."""
        async with async_session() as s:
            return await s.get(cls, id)

    @mutation
    async def create_post(cls, title: str, content: str, author_id: int) -> "Post":
        """Create a post."""
        async with async_session() as s:
            post = cls(title=title, content=content, author_id=author_id)
            s.add(post)
            await s.commit()
            await s.refresh(post)
            return post


# One line to expose the entire thing as an MCP service.
mcp = create_simple_mcp_server(
    base=SQLModel,
    name="Blog (NexusX Simple)",
    session_factory=async_session,
    allow_mutation=True,
)


async def init_db() -> None:
    """Create tables and seed sample data."""
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


async def main_stdio() -> None:
    await init_db()
    mcp.run()


def main_http() -> None:
    import uvicorn
    from starlette.middleware.cors import CORSMiddleware

    asyncio.run(init_db())

    app = mcp.http_app(transport="streamable-http", stateless_http=True)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    uvicorn.run(app, host="0.0.0.0", port=9002)


if __name__ == "__main__":
    if "--http" in sys.argv:
        main_http()
    else:
        asyncio.run(main_stdio())
