# 五分钟搭建 MCP 服务：NexusX vs 手写 FastMCP

> 面向已经用 SQLModel 写过实物的开发者。目标：把数据库暴露给 Claude / Cursor 等 AI Agent。
>
> 本 repo 是可运行的对比 demo——三条路径的代码都在，每个 server 都能独立启动。

## 快速运行

```bash
git clone https://github.com/allmonday/nexusx-vs-fastmcp.git
cd nexusx-vs-fastmcp

uv sync                              # 或：pip install -e .

python init_db.py                    # 一次性建表 + 种子数据（三个 db）

# 任选一条路径启动：
python fastmcp_handwritten.py        # 路径 A：手写 FastMCP（stdio）
python nexusx_simple.py              # 路径 B1：NexusX simple（stdio）
python nexusx_usecase.py             # 路径 B2：NexusX UseCase（stdio）

# 加 --http 切换到 HTTP 模式，端口 9001 / 9002 / 9003：
python nexusx_usecase.py --http
```

stdio 模式接入 Claude Desktop / Cursor；HTTP 模式可用 `curl` 或浏览器端 MCP 客户端调试。

---

## TL;DR

同一个需求——"让 AI Agent 能查询和创建 User 与 Post"——三条路径：

| | 手写 FastMCP | NexusX simple | NexusX UseCase |
|---|---|---|---|
| 实际产出 | 7 个扁平工具（每实体 3 个 + 1 个关系补偿工具） | 1 个 GraphQL endpoint + 3 个 MCP 工具 | 4 层渐进披露 MCP 工具 |
| 嵌套查询（`posts { author { name } }`） | 必须单独写一个 `list_posts_with_author` 工具 | 直接 GraphQL 嵌套，DataLoader 自动防 N+1 | 业务方法层组合，DTO 决定形状 |
| Agent 端 token 占用 | 一上来读全部 7 个工具描述 | 3 个工具，schema 按需下钻 | 4 个工具，三层下钻 |
| REST API 同源 | 再写一份 FastAPI 路由 | 同样需要再写 | 同一个 `UseCaseService` 直接挂 FastAPI |

下面把三条路径完整写一遍。所有代码片段都对应 repo 里的真实文件。

---

## 共同的起点

三条路径共用同一份实体定义（`User` + `Post` 带一对多关系）和同样的种子数据。差异只在"如何变成 MCP 服务"。

实体字段：

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

种子数据：2 个 user（Alice、Bob）、3 个 post。

---

## 路径 A：手写 FastMCP（10+ 分钟）

> 完整代码：[`fastmcp_handwritten.py`](./fastmcp_handwritten.py)

FastMCP 是把 Python 函数变成 MCP 工具的事实标准。每个工具一个 `@mcp.tool` 装饰器，FastMCP 从类型注解和 docstring 推断 schema。

```python
from fastmcp import FastMCP
from sqlalchemy.orm import selectinload
from sqlmodel import select

mcp = FastMCP("Blog")


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


# ... create_user、get_post、list_posts、create_post 同构 ...


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
            {"id": p.id, "title": p.title,
             "author": {"id": p.author.id, "name": p.author.name}}
            for p in rows
        ]
```

**实际产出**：7 个工具（`get_user`、`list_users`、`create_user`、`get_post`、`list_posts`、`create_post`、`list_posts_with_author`）。

### 痛点

1. **每个实体 N 倍工作量**：User 3 个工具、Post 3 个工具……线性增长。
2. **嵌套数据要单独设计工具**：想要"带 author 的 post 列表"，必须手写 `list_posts_with_author` 并显式 `selectinload`。再多一层关系（`posts { author { comments } }`）就要再加一个工具。
3. **工具墙**：Agent 一启动就读到全部 7 个工具的 schema 描述——即便这次任务只用得上 `list_posts`。
4. **没有组合查询能力**：Agent 想"同时拿 User 列表和 Post 列表"必须两次往返。
5. **REST 同源要重写**：同一个业务逻辑想给前端用，得再写一份 FastAPI 路由。

---

## 路径 B1：NexusX Simple（30 秒）

> 完整代码：[`nexusx_simple.py`](./nexusx_simple.py)

NexusX 的逻辑：**实体的 `@query` / `@mutation` 方法就是 GraphQL 字段，MCP 自动包住整个 GraphQL endpoint**。

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


# Post 类同构：get_posts / get_post / create_post


# ─── 一行变成 MCP 服务 ───
mcp = create_simple_mcp_server(
    base=SQLModel,
    name="Blog (NexusX Simple)",
    session_factory=async_session,
    allow_mutation=True,
)
```

**业务代码量跟路径 A 几乎一样**——你本来就得写"怎么查 user、怎么创建 post"。NexusX 没有魔法消除这些。

差异在最后一行之前：

| 路径 A 你要操心的事 | 路径 B1 你不用操心的事 |
|---|---|
| `@mcp.tool` 装饰器 × N | 自动 |
| 每个工具的 input schema | 从类型注解自动推 |
| 每个工具的返回序列化 | GraphQL SDL 自动 |
| `list_posts_with_author` 这种嵌套工具 | 直接 GraphQL 嵌套查询 |
| `selectinload` 防止 N+1 | DataLoader 自动批量 |
| 工具数量随实体爆炸 | 3 个工具固定不变 |

### Agent 端看到的工具差异

路径 A 的 Agent 启动时读到 7 个工具描述。

路径 B1 的 Agent 启动时读到：

```
- get_schema()           → { sdl }
- graphql_query(query)   → { data }
- graphql_mutation(...)  → { data }
```

然后 Agent 按需调 `get_schema` 了解能力，再发一次 GraphQL 查询。组合查询、嵌套字段一次往返搞定：

```graphql
{
  userGetUser(id: 1) {
    id
    name
    posts {
      id
      title
      author { name }   /* 跨关系回引，DataLoader 自动批量 */
    }
  }
}
```

路径 A 要做同样的事，Agent 得调 3 次工具（`get_user` → `list_posts_with_author` → 拼装），或者你预先写好一个 `get_user_with_posts_and_author_comments` 工具——然后下一个稍微不同的查询需求又得再加一个工具。

---

## 路径 B2：NexusX UseCase + 4 层渐进披露（再花 2 分钟）

> 完整代码：[`nexusx_usecase.py`](./nexusx_usecase.py)

如果你要暴露的不是裸 CRUD，而是**业务方法**（比如 `list_users_with_post_counts` 这种带派生字段的），NexusX 的 UseCase 模式更有优势。

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

此时 MCP 提供 4 层工具：

| 工具 | 用途 | 响应信封 |
|---|---|---|
| `list_apps()` | 发现可用应用 | 极小 |
| `describe_compose_schema(app)` | 列出 service + 方法名（不含参数） | 小 |
| `describe_compose_method(app, svc, method)` | 看具体方法的参数 + 返回类型 + SDL 片段 | 中 |
| `compose_query(app, query)` | 执行 GraphQL 字符串 | 按查询大小 |

### 这是 NexusX 相对 FastMCP 最大的结构差异

- **FastMCP 是扁平工具墙**——Agent 启动就把所有工具描述塞进上下文
- **NexusX 是渐进式发现树**——Agent 先看 `list_apps` 决定要不要继续，再 `describe_compose_schema` 看方法名，最后 `describe_compose_method` 拿到精确 schema

实体越多、业务方法越多，这个差异越明显。FastMCP 项目里 30+ 工具是常态，Agent 上下文被工具描述吃掉一大块；NexusX 始终是 4 个工具。

而且**同一个 `UserService` 子类还能直接挂到 FastAPI**：

```python
from nexusx import create_use_case_router

router = create_use_case_router(
    apps=[UseCaseAppConfig(name="blog", services=[UserService])],
)
# 挂到 FastAPI app 即可，OpenAPI 文档自动生成
```

业务逻辑写一遍，MCP 和 REST 两面交付。FastMCP 路径要做同样的事，得手动把每个工具函数重新包成 FastAPI 端点。

---

## 量化对比

| 维度 | 手写 FastMCP | NexusX simple | NexusX UseCase |
|---|---|---|---|
| 实体 → MCP 暴露的额外代码 | ~30 行 × 实体数 | 0 行（一行 `create_simple_mcp_server`） | 0 行（一行 `create_use_case_graphql_mcp_server`） |
| 嵌套关系查询 | 手写工具 + `selectinload` | GraphQL 字段嵌套，DataLoader 自动 | 业务方法层组合 |
| 工具数量增长 | 线性（每实体 +N） | 常数（固定 3 个） | 常数（固定 4 个） |
| Agent 启动时 schema 加载 | 全部工具描述 | 1 个 `get_schema` 工具，按需下钻 | 3 层下钻（apps → schema → method） |
| 组合查询（多实体一次往返） | 不支持 | 原生 GraphQL | 原生 GraphQL |
| REST API 同源 | 另写 FastAPI | 另写 FastAPI | `UseCaseService` 直接挂 |
| N+1 防护 | 手动 `selectinload` | DataLoader 自动批量 | DataLoader 自动批量 |

---

## 诚实的边界

NexusX 不是所有场景都更合适。

**适合用 NexusX**：

- 项目本来就用 SQLModel / SQLAlchemy
- 需要把数据库实体或业务方法暴露给 AI Agent
- 同时需要 REST 和 MCP 双交付（→ UseCase 模式）
- 实体多、关系复杂、嵌套查询频繁

**适合继续用 FastMCP**：

- MCP 工具是独立的"无状态函数"——比如 `send_email(to, subject)`、`resize_image(url, width)`，跟数据库无关
- 工具数量很少（< 5 个）且不会增长
- 非 Python 生态（FastMCP 还有 TypeScript / Go 版本）
- 你需要非常精确地控制每个工具的 description / 参数顺序，GraphQL 抽象反而是阻碍

---

## 五分钟清单

```bash
# Step 1: 安装（30 秒）
git clone https://github.com/allmonday/nexusx-vs-fastmcp.git
cd nexusx-vs-fastmcp
uv sync
```

```python
# Step 2: 复用你已有的 SQLModel 实体
# 给每个实体加 @query / @mutation 方法（业务代码，不是 MCP 样板）
# 或者把它们组织成 UseCaseService 子类
```

```python
# Step 3: 创建 MCP 服务（5 秒）
from nexusx.mcp import create_simple_mcp_server

mcp = create_simple_mcp_server(
    base=SQLModel,
    name="My API",
    session_factory=async_session,
)
```

```python
# Step 4: 运行
mcp.run()                                       # stdio，给 Claude Desktop / Cursor 用
# mcp.http_app(transport="streamable-http")     # HTTP，给 Web 端 Agent 用（见各 server 的 --http 分支）
```

```json
// Step 5: 接入 Claude Desktop（~/.config/claude/claude_desktop_config.json）
{
  "mcpServers": {
    "blog": {
      "command": "python",
      "args": ["/path/to/nexusx_simple.py"]
    }
  }
}
```

重启 Claude Desktop，对话里直接问"列出所有用户及其最近一篇 post"——Agent 会自动发现 schema、构造 GraphQL 查询、一次拿到结果。

---

## 项目结构

```
nexusx-vs-fastmcp/
├── README.md                    # 本文
├── pyproject.toml               # uv-managed（nexusx 从 git 拉）
├── init_db.py                   # 一次性为三个 db 建表 + 种子
├── fastmcp_handwritten.py       # 路径 A：7 个 @mcp.tool
├── nexusx_simple.py             # 路径 B1：@query/@mutation + create_simple_mcp_server
└── nexusx_usecase.py            # 路径 B2：UseCaseService + 4 层渐进披露
```

三个 server 用独立的 sqlite 文件（`blog_fastmcp.db` / `blog_nexusx_simple.db` / `blog_nexusx_usecase.db`），互不干扰，可以同时跑。

---

## 进一步阅读

- [NexusX MCP 服务文档](https://github.com/allmonday/nexusx/blob/main/docs/advanced/mcp_service.zh.md) — `create_simple_mcp_server` 与 `create_mcp_server`（多应用）的完整参数
- [NexusX UseCase 服务文档](https://github.com/allmonday/nexusx/blob/main/docs/advanced/use_case_service.zh.md) — 4 层渐进披露 MCP + FastAPI 同源
- [NexusX GraphQL 模式](https://github.com/allmonday/nexusx/blob/main/docs/guide/graphql_mode.zh.md) — MCP 底层使用的 GraphQL API 细节
- [NexusX 主项目](https://github.com/allmonday/nexusx) — 从 SQLModel 类自动生成 GraphQL + Core API + MCP
