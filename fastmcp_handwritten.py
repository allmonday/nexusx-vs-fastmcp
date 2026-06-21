"""Path A: hand-written FastMCP server.

Each entity needs N tools (list / get / create / ...). Each tool requires its own
decorator, docstring, session handling, and serialization. Nested data needs a
separate tool with explicit eager loading.

Tools exposed (8 total):
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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlmodel import Field, Relationship, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

DATABASE_URL = "sqlite+aiosqlite:///./blog_fastmcp.db"
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


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


@asynccontextmanager
async def session():
    async with async_session() as s:
        yield s


mcp = FastMCP("Blog")


# ──────────────────────────── User tools ────────────────────────────


@mcp.tool
async def get_user(user_id: int) -> dict:
    """Get a user by ID."""
    async with session() as s:
        user = await s.get(User, user_id)
        if not user:
            return {"error": "not found"}
        return {"id": user.id, "name": user.name, "email": user.email}


@mcp.tool
async def list_users(limit: int = 20) -> list[dict]:
    """List users."""
    async with session() as s:
        rows = (await s.exec(select(User).limit(limit))).all()
        return [{"id": u.id, "name": u.name, "email": u.email} for u in rows]


@mcp.tool
async def create_user(name: str, email: str) -> dict:
    """Create a user."""
    async with session() as s:
        user = User(name=name, email=email)
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return {"id": user.id, "name": user.name, "email": user.email}


# ──────────────────────────── Post tools ────────────────────────────


@mcp.tool
async def get_post(post_id: int) -> dict:
    """Get a post by ID."""
    async with session() as s:
        post = await s.get(Post, post_id)
        if not post:
            return {"error": "not found"}
        return {"id": post.id, "title": post.title, "author_id": post.author_id}


@mcp.tool
async def list_posts(limit: int = 20) -> list[dict]:
    """List posts."""
    async with session() as s:
        rows = (await s.exec(select(Post).limit(limit))).all()
        return [{"id": p.id, "title": p.title, "author_id": p.author_id} for p in rows]


@mcp.tool
async def list_posts_with_author(limit: int = 20) -> list[dict]:
    """List posts with their author (avoids N+1 via selectinload).

    This tool exists *only* because the flat-tool model can't express
    nested field selection. Every new shape needs another tool.
    """
    async with session() as s:
        stmt = select(Post).options(selectinload(Post.author)).limit(limit)
        rows = (await s.exec(stmt)).all()
        return [
            {
                "id": p.id,
                "title": p.title,
                "author": {"id": p.author.id, "name": p.author.name},
            }
            for p in rows
        ]


@mcp.tool
async def create_post(title: str, content: str, author_id: int) -> dict:
    """Create a post."""
    async with session() as s:
        post = Post(title=title, content=content, author_id=author_id)
        s.add(post)
        await s.commit()
        await s.refresh(post)
        return {"id": post.id, "title": post.title, "author_id": post.author_id}


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
