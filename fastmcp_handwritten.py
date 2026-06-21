"""Path A: hand-written FastMCP server.

Each entity needs N tools (list / get / create / ...). Each tool requires its own
decorator, docstring, session handling, and serialization. Nested data needs a
separate tool with explicit eager loading.

FastMCP *does* auto-generate ``inputSchema`` / ``outputSchema`` from pydantic
types — so we lean into that best practice here. The point of comparison is
about *tool-count scaling* and *progressive disclosure*, not schema automation
(NexusX and FastMCP both solve that part).

Tools exposed (7 total):
    get_user, list_users, create_user,
    get_post, list_posts, list_posts_with_author, create_post

Run:
    python fastmcp_handwritten.py             # stdio (for Claude Desktop / Cursor)
    python fastmcp_handwritten.py --http      # streamable-http on :9001
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from typing import Optional

from fastmcp import FastMCP
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlmodel import Field as SQLField, Relationship, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

DATABASE_URL = "sqlite+aiosqlite:///./blog_fastmcp.db"
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class User(SQLModel, table=True):
    id: Optional[int] = SQLField(default=None, primary_key=True)
    name: str
    email: str
    posts: list["Post"] = Relationship(back_populates="author")


class Post(SQLModel, table=True):
    id: Optional[int] = SQLField(default=None, primary_key=True)
    title: str
    content: str
    author_id: int = SQLField(foreign_key="user.id")
    author: Optional["User"] = Relationship(back_populates="posts")


# ──────────────────────────── Pydantic I/O models ────────────────────────────
# FastMCP derives inputSchema + outputSchema from these. Field() metadata flows
# straight into the JSON Schema the agent sees.


class UserCreate(BaseModel):
    """Payload to create a user."""

    name: str = Field(..., description="User's display name")
    email: str = Field(..., description="Reachable email")


class UserOut(BaseModel):
    """A user record."""

    id: int
    name: str
    email: str


class PostCreate(BaseModel):
    """Payload to create a post."""

    title: str = Field(..., description="Post title")
    content: str = Field(..., description="Post body content (markdown allowed)")
    author_id: int = Field(..., description="ID of the authoring user")


class PostOut(BaseModel):
    """A post record."""

    id: int
    title: str
    author_id: int


class PostWithAuthor(BaseModel):
    """A post with nested author info.

    This shape exists *only* because the flat-tool model can't express nested
    field selection. Every new nesting needs another tool + another Pydantic
    model.
    """

    id: int
    title: str
    author: UserOut


@asynccontextmanager
async def session():
    async with async_session() as s:
        yield s


mcp = FastMCP("Blog")


# ──────────────────────────── User tools ────────────────────────────


@mcp.tool
async def get_user(user_id: int) -> UserOut | None:
    """Get a user by ID.

    Returns ``null`` if the user does not exist.
    """
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
    async with session() as s:
        user = User(name=payload.name, email=payload.email)
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return UserOut(id=user.id, name=user.name, email=user.email)


# ──────────────────────────── Post tools ────────────────────────────


@mcp.tool
async def get_post(post_id: int) -> PostOut | None:
    """Get a post by ID.

    Returns ``null`` if the post does not exist.
    """
    async with session() as s:
        post = await s.get(Post, post_id)
        if not post:
            return None
        return PostOut(id=post.id, title=post.title, author_id=post.author_id)


@mcp.tool
async def list_posts(limit: int = 20) -> list[PostOut]:
    """List posts."""
    async with session() as s:
        rows = (await s.exec(select(Post).limit(limit))).all()
        return [PostOut(id=p.id, title=p.title, author_id=p.author_id) for p in rows]


@mcp.tool
async def list_posts_with_author(limit: int = 20) -> list[PostWithAuthor]:
    """List posts with their author (avoids N+1 via selectinload).

    This tool exists *only* because the flat-tool model can't express nested
    field selection. Every new shape needs another tool + another Pydantic model.
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


@mcp.tool
async def create_post(payload: PostCreate) -> PostOut:
    """Create a post from a pydantic payload."""
    async with session() as s:
        post = Post(
            title=payload.title,
            content=payload.content,
            author_id=payload.author_id,
        )
        s.add(post)
        await s.commit()
        await s.refresh(post)
        return PostOut(id=post.id, title=post.title, author_id=post.author_id)


# ──────────────────────────── Bootstrap ────────────────────────────


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
    uvicorn.run(app, host="0.0.0.0", port=9001)


if __name__ == "__main__":
    if "--http" in sys.argv:
        main_http()
    else:
        asyncio.run(main_stdio())
