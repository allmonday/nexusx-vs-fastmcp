# 五分钟搭建 MCP 服务：NexusX vs 手写 FastMCP

> 面向已经用 SQLModel 写过实物的开发者。目标：把数据库暴露给 Claude / Cursor 等 AI Agent。
>
> 本 repo 是可运行的对比 demo——三条路径的代码都在，每个 server 都能独立启动。

**English**: [README.en.md](./README.en.md)

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

FastMCP 是把 Python 函数变成 MCP 工具的事实标准。每个工具一个 `@mcp.tool` 装饰器。

公平起见，这里用 FastMCP 的**最佳实践**：参数和返回值用 pydantic `BaseModel`。FastMCP 会自动从类型注解生成 `inputSchema` / `outputSchema`（含 `Field(description=...)` 等元数据）——这一块 FastMCP 已经做得很好，不是 NexusX 的差异点。

```python
from fastmcp import FastMCP
from pydantic import BaseModel, Field
from sqlalchemy.orm import selectinload
from sqlmodel import select


# ─── pydantic I/O 模型 ───
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


# get_post / list_posts / create_post 同构 ...


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

**实际产出**：7 个工具（`get_user`、`list_users`、`create_user`、`get_post`、`list_posts`、`create_post`、`list_posts_with_author`）。FastMCP 给每个工具生成 `inputSchema` + `outputSchema`，docstring 成为工具描述——这一部分免费。

### 仍然存在的痛点

1. **每个实体 N 倍工作量**：User 3 个工具、Post 3 个工具……工具数量随实体线性增长。schema 自动化省掉了字段定义，但工具本身仍然要逐个手写。
2. **嵌套数据要单独设计工具**：想要"带 author 的 post 列表"，必须手写 `list_posts_with_author` + 一个 `PostWithAuthor` 模型 + 显式 `selectinload`。再多一层关系（`posts { author { comments } }`）就要再加一个工具和一个模型。
3. **工具墙**：Agent 启动时得拿到全部 7 个工具的 schema 才能用——即便这次任务只用得上 `list_posts`。schema 自动化反而让每个工具的描述变得更详细，token 占用更大。FastMCP 支持 `tools/list` 分页（cursor）可以分批发现，但只是省初次发现的延迟，agent 真用工具时仍然要加载完整 schema，token 总量没省。
4. **Over-fetch / 没法按需选字段**：MCP 协议的 `tools/call` 没有"只要某些字段"的参数。Agent 调 `get_user` 必须接受整个 `UserOut`（id + name + email 全返回），就算它只关心 `name`。模型字段越多浪费越严重——pydantic 模型字段一多，单次响应 token 就线性涨。
5. **没有组合查询能力**：Agent 想"同时拿 User 列表和 Post 列表"必须两次往返。
6. **REST 同源要重写**：同一个业务逻辑想给前端用，得再写一份 FastAPI 路由（虽然 pydantic 模型可以复用）。

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
| 每个实体一份 pydantic I/O 模型（schema 自动，但模型本身要写） | GraphQL SDL 从 SQLModel 元数据自动生成 |
| `list_posts_with_author` 这种嵌套工具 + 嵌套 pydantic 模型 | 直接 GraphQL 嵌套查询 |
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
| inputSchema / outputSchema 自动生成 | ✅（从 pydantic 模型） | ✅（从 SQLModel 元数据 → GraphQL SDL） | ✅（从 service 方法签名 → GraphQL SDL） |
| 每实体要写的样板 | N 个工具函数 + N 个 pydantic 模型 | 0 行（一行 `create_simple_mcp_server`） | 0 行（一行 `create_use_case_graphql_mcp_server`） |
| 嵌套关系查询 | 手写工具 + 嵌套模型 + `selectinload` | GraphQL 字段嵌套，DataLoader 自动 | 业务方法层组合 |
| 工具数量增长 | 线性（每实体 +N 工具） | 常数（固定 3 个） | 常数（固定 4 个） |
| Agent 启动时 schema 加载 | 全部工具描述（schema 越细，token 越多） | 1 个 `get_schema` 工具，按需下钻 | 3 层下钻（apps → schema → method） |
| 返回字段过滤（防 over-fetch） | ❌ 工具返回整个 pydantic 模型，agent 无法按需选字段 | ✅ GraphQL 字段选择 | ✅ GraphQL 字段选择 |
| 组合查询（多实体一次往返） | 不支持 | 原生 GraphQL | 原生 GraphQL |
| REST API 同源 | pydantic 模型可复用，但端点要重写 | 另写 FastAPI | `UseCaseService` 直接挂 |
| N+1 防护 | 手动 `selectinload` | DataLoader 自动批量 | DataLoader 自动批量 |
| Tool 安全标注（annotations） | ✅ `readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint` 帮 agent 决策调用是否安全 | ❌ GraphQL schema 无等价机制 | ❌ 同左 |
| MCP Resources（URI 寻址资源） | ✅ `@mcp.resource("config://...")` 暴露配置 / 文件 / 文档 | ❌ 仅 tools | ❌ 仅 tools |
| MCP Prompts（预定义提示模板） | ✅ `@mcp.prompt` 给 agent 提供结构化 prompt | ❌ 仅 tools | ❌ 仅 tools |

---

## 设计哲学：把 AI agent 当成又一个前端

回头看量化对比表里 FastMCP 的几个痛点——over-fetch、没法字段选择、没法组合查询、启动时 schema token 占用随工具数线性增长——前端工程师会很眼熟。这正是 2015 年 REST + BFF 时代天天吵架的事。GraphQL 当年就是为了解决这些而生的：字段选择、嵌套查询、强类型契约、按需组合。

agent 现在撞上同一堵墙，只是换了个名字叫 "tools/list 的 token 预算"。

NexusX 做的事不是"发明一个 MCP 框架"，而是**把 SQLModel 实体当作 GraphQL schema 的单一真相源，让 agent 通过 GraphQL-over-MCP 拿到所有能力**。一行 `create_simple_mcp_server` 背后真正起作用的是这套继承关系——agent 拿到的不是"为 AI 设计的新协议"，而是"为前端设计、已经被亿级生产流量验证过的协议"。

这个抽象的价值有三层：

1. **Agent 的能力上限被抬高了**——组合查询、字段投影、嵌套关系一次往返，这些是 GraphQL 的红利，agent 现在白捡。
2. **存量 SQLModel 项目几乎免费接入**——本来就有 entity，加几个 `@query` 装饰器就有 MCP。FastMCP 路径要重写一遍工具层。
3. **`UseCaseService` 让 REST 跟着 MCP 一起免费**——业务逻辑写一遍，agent 和前端两面交付。

但前提是：**数据有结构、关系可遍历**。如果 agent 要做的不是查数据库——而是发邮件、改图片、调外部 API——GraphQL 的抽象反而是阻碍，FastMCP 那种"工具即函数"的模型更合适。下一节展开边界。

---

## opinionated GraphQL：用约束换可预测性

但 GraphQL 协议本身有个长期被诟病的痛点：**灵活度过高，约束力度不够**。

社区里推 schema 风格的声音从来不统一——Apollo 推 schema-first + business-aligned types，Facebook 早期按 UI 组件组织，绝大多数团队最后却把 ORM 模型直接映射成 GraphQL type。三种风格混用，schema 就开始烂。问题本质是 GraphQL 只规定了**语法**，没规定**风格**——团队"找不到最佳实践"不是没努力，是协议没给约束。

NexusX 在这一层加了个 opinionated 的框架约束，两条 API 路径泾渭分明：

- **B1（simple 模式）= 强制面向 model**——从 SQLModel 元数据自动生成 schema，你不能乱加字段。本质是"自动化的 ORM → GraphQL 映射"，但因为是框架生成的，没有"团队要不要这样"的争论空间。
- **B2（UseCase 模式）= 强制面向 business**——schema 必须挂在 `UseCaseService` 方法上，按业务用例组织，不写业务方法就没有字段。这一条把 Facebook / Apollo 推崇的 business-aligned 风格变成了**唯一的代码路径**。

**用户没机会走偏**——不是"找最佳实践"，是"框架替你选好了"。GraphQL 协议层仍然保留查询能力（字段选择、嵌套、组合），但 schema 设计自由度被剥夺了。这跟 Strawberry / Ariadne 那种"给你一个 GraphQL endpoint、schema 自己设计"是相反方向。

一句话：**NexusX 不是更好的 GraphQL 框架，是更不自由的 GraphQL 框架——而不自由恰恰是它解决"找不到最佳实践"的方式。**

这也设定了它的适用边界：如果你需要 schema 设计自由度（多团队协作、复杂联邦 schema、跨域类型协调），NexusX 反而是阻碍。下一节展开。

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
- **需要 MCP 协议的完整三面**（tools + resources + prompts）：FastMCP 是完整的 MCP server 框架，NexusX 专注于把 SQLModel / 业务方法暴露成 tools（GraphQL-over-MCP），不覆盖 resources 和 prompts

### FastMCP 在本对比之外的优势

为了让对比公平，下面这些是 FastMCP 真实存在、但跟"暴露 SQLModel 实体"这个核心场景无关或弱相关的优势：

- **Tool annotations**：`readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint` 等 hint，agent 用来判断调用是否安全（比如"只读工具可以放心试")。GraphQL schema 里没有等价概念。
- **MCP Resources**：`@mcp.resource("config://...")` 暴露 URI 寻址的资源（配置、文档、文件等），跟 tools 是不同的访问模式。
- **MCP Prompts**：`@mcp.prompt` 定义可复用的提示模板，agent 可以拿到结构化 prompt。
- **Context 注入 + Lifespan**：FastMCP 工具可以注入 `Context`（progress、logging、state），server 启停有 lifespan hook。
- **Server composition**：`mcp.import_server("prefix", other_mcp)` 把多个独立 server 组合起来。
- **In-process / direct call**：FastMCP 工具可以直接当 Python 函数调用（用 `Client` 跳过协议层），方便写集成测试。
- **Auth 集成**：FastMCP 内置 OAuth / bearer token 处理。

如果你的需求超出"把数据库暴露给 agent"，比如要混合 tools + resources + prompts，FastMCP 才是更合适的基础。

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
├── README.md                    # 中文版（主）
├── README.en.md                 # 英文版
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
