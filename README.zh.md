# GraphQL 风格的 MCP：把数据库和业务接口开放给 AI

最近一年，MCP（Model Context Protocol）悄悄变成了事实标准。它最早是 Anthropic 内部的协议，现在 Claude Desktop、Cursor、Claude Code、Cline、Codex 都支持了。社区里已经有一千多个 server，把数据库、文件系统、各种 SaaS API、内部工具，一股脑都接给 agent 用。

能大规模地把后端能力暴露给大模型，这是 agent 生态往前走的必要一步。

但是，MCP 默认的工具模型，在处理结构化数据时开始捉襟见肘了。它把每一个能力做成一个独立的函数，有固定的入参和出参。这种"工具 = 函数"的设定，一旦遇到彼此关联的实体，就有点别扭。

举个例子。User 下面有 Post，Post 又有 Author，Author 又有自己的 Post。就这么两个实体的小模型，agent 可能想问的查询形状，已经有几十种。flat-tool MCP 要求你事先把每一种形状都想到，预先写一个工具，再加一套 pydantic 模型。这不是实现的 bug，是范式本身决定的代价。

说白了，工具是固定形状的函数，数据是可变形状的图。这种错位不是新问题。

前端工程师在 2015 年前后撞过同一堵墙。那时大家都在 REST 加 BFF 的模式里挣扎，最后的答案就是 GraphQL。字段投影解决 over-fetch，嵌套解析解决 N+1，强类型 schema 解决契约漂移。十几年沉淀下来的工程实践——DataLoader、深度限制、cost analysis、persisted query——都是现成的。

GraphQL-based MCP 就是把这套基础设施借给 agent 用。这个想法其实不新——社区里早有人提过——但一直没真正流行起来，根因之一就是再单独维护一套 GraphQL 层太贵：得起 server、写 resolver、扛 Apollo 或 Strawberry 的运维。NexusX 改变的就是这一点。开发者只要把数据库模型或者业务方法声明成 GraphQL schema，框架会自动把这个 schema 包成 MCP 工具交给 agent。agent 拿到的是 GraphQL 的全部查询能力；开发者拿到的是“一条查询、一次往返拿完”的简洁，而不是“再多维护一个 GraphQL 服务”的负担。

先退一步说 NexusX 本身是什么。它是个数据定义和组装框架：你定义 ER（实体和关系），它在此基础上渐进地、声明式地衍生出各种数据组合能力——GraphQL schema、REST 路由、MCP 工具，这些都是 ER 定义之上的衍生品，不是各自要单独维护的表面。前一段那一句"框架自动把 schema 包成 MCP 工具"之所以做得到，根因就在这里——MCP 这一层不是 NexusX 又写了一遍 schema，而是从 ER 出发自动长出来的。

这里要补一句。NexusX 用的 GraphQL 不是完整规范。alias（字段别名）、fragment（查询片段）这些为人类开发者设计的特性，agent 写查询用不上，都被砍掉了。schema 因此更小、更省 token。换句话说，agent 拿到的是为它裁剪过的 GraphQL，不是前端团队那一整套。

我想用一个最具体的任务来说明。假设 agent 要回答："列出 Alice 的 post 及其作者"。在 flat-tool MCP（FastMCP 推广的那种风格）里，这件事要三次工具调用，外加一个事先手写好的胶水工具。在 GraphQL-based MCP 里，这就是一条 agent 自己写出来的查询。

这个仓库把两种范式并排跑给你看。同一份 User 加 Post 实体，同一份种子数据，三个完整的 server 实现。

- 路径 A：手写 FastMCP，每个动作一个 `@mcp.tool`。
- 路径 B1：NexusX Simple，实体上的 `@query` / `@mutation` 方法，加一行启动 MCP。
- 路径 B2：NexusX UseCase，业务方法形状的 MCP，分 4 层渐进披露。

下面不是一份功能清单，而是一次走读。我会讲每种范式写起来什么手感，agent 在协议层看到的又是什么。

（英文版见 [README.md](./README.md)。）

## 快速运行

```bash
git clone https://github.com/allmonday/nexusx-vs-fastmcp.git
cd nexusx-vs-fastmcp

uv sync                              # 或者：pip install -e .

python init_db.py                    # 一次性建表 + 种子数据（三个 db）

# 任选一条路径启动：
python fastmcp_handwritten.py        # 路径 A：手写 FastMCP（stdio）
python nexusx_simple.py              # 路径 B1：NexusX simple（stdio）
python nexusx_usecase.py             # 路径 B2：NexusX UseCase（stdio）

# 加 --http 切换到 HTTP 模式，端口 9001 / 9002 / 9003：
python nexusx_usecase.py --http
```

stdio 模式可以接入 Claude Desktop 或者 Cursor。HTTP 模式可以用 curl 或者浏览器端 MCP 客户端来调试。

## 我们要解决的问题

两个 SQLModel 实体，一对多关系，一个需要在这两张表之间来回走的 agent。

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

种子数据很简单：2 个 user（Alice、Bob），3 个 post。下面所有的内容都围绕一个问题展开——每种范式如何让 agent 在这张小图上来回走，你又要为此写多少代码。

## 路径 A：手写 FastMCP

> 完整代码见 [`fastmcp_handwritten.py`](./fastmcp_handwritten.py)

大多数 Python MCP 项目都是从这里开始的。一个 `@mcp.tool` 装饰器对应一个动作，pydantic 模型作 I/O，让 `inputSchema` / `outputSchema` 自动生成得干净。FastMCP 这部分确实做得好，schema 自动化不是 NexusX 跟它差异化的点。

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

两个实体，七个工具。痛点就是从这里开始的，而且来得很快。

第一，工具墙早早就出现了。每个实体 3 个工具是下限。等你有了 10 个实体，你就在 ship 30 多个工具描述。Eager-load 的客户端——现在主要就是 Cursor——启动时把它们全读进上下文，不管这次任务用不用得上。新版 Claude Code 和新版 Claude Desktop 通过官方的 Tool Search 绕开了大部分问题：只有工具的 name 进上下文，schema 按调用拉取。但 MCP 协议层本身没有渐进披露的内建机制，只能靠客户端自觉。即便这样，Claude Code 和 Codex 都还有不跟随 `tools/list` 分页的 bug 没修。每个工具描述还比想象中更冗长，因为 schema 自动化会把所有 `Field(description=...)` 和嵌套类型都拉进来。token 成本是涨的，不是降的。

第二，嵌套数据要单独设计工具。"带 author 的 post 列表"这个需求，必须手写一个 `list_posts_with_author`，加一个 `PostWithAuthor` 的 pydantic 模型，再加一个显式的 `selectinload`。再多一层关系，比如 `posts { author { comments } }`，就再加一个工具、一个模型。agent 可能用到的每一种查询形状，都得事先设想好、预先写好。

第三，over-fetch 是结构性的。MCP 的 `tools/call` 没有"只要某些字段"这种参数。agent 调 `get_user`，必须接受整个 `UserOut`，不管它想要 2 个字段还是 12 个。pydantic 模型越宽，单次响应浪费的 token 就越多。

第四，组合查询吃往返。"同时拿 User 列表和 Post 列表"要调两次工具。MCP 协议在 2025-06-18 版本里正式移除了 JSON-RPC batch，理由是太脆弱，复杂度盖过了收益。所以协议层也不再有线合并多次调用的办法。任何嵌套组合，比如 `users { posts { author } }`，仍然要专门写工具。

最后说一下 REST 共存这一项。FastMCP 3.0 加了 `FastMCP.from_fastapi(app)`，能把现有 FastAPI 路由自动提升成 MCP 工具；反向也能把 MCP server mount 到 FastAPI ASGI app 里。如果你从 FastAPI 起步，REST 加 MCP 共存基本就解决了。这里有一个结构性 caveat：路径 A 这种模式——直接写 `@mcp.tool` 函数、底下没有 FastAPI app——拿不到这座桥，得先重构成"FastAPI 在先，再暴露 MCP"才能继承。NexusX 的 `UseCaseService`（路径 B2）从另一头解决：业务方法写一遍，同时挂到 MCP 和 FastAPI。

这些都不是 FastMCP 的 bug。是"工具 = 函数"这个契约形状，撞上了"数据 = 图"这个问题。问题是结构性的，痛也是结构性的。

## 路径 B1：NexusX Simple

> 完整代码见 [`nexusx_simple.py`](./nexusx_simple.py)

NexusX 把前提翻转了：不要再写工具，写查询。实体上的 `@query` / `@mutation` 方法本身就是 GraphQL 字段；MCP 用一行把整个 GraphQL endpoint 包起来。

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

业务代码量跟路径 A 差不多——你本来就得写"怎么查 user、怎么创建 post"。NexusX 也没省掉这些。

差异在最后一行之前的所有事情。没有 N 个 `@mcp.tool` 装饰器；没有每个实体一份 pydantic I/O 模型（SDL 从 SQLModel 元数据自动生成）；没有 `list_posts_with_author`（agent 直接要这个形状就行）；没有 `selectinload`（DataLoader 自动批量）。

agent 现在看到的是 3 个工具，不是 7 个：

```
- get_schema()           → { sdl }
- graphql_query(query)   → { data }
- graphql_mutation(...)  → { data }
```

它调一次 `get_schema` 了解形状，然后写 GraphQL。前面那个场景——"带 author 的 post"——折叠成一条查询：

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

一次协议往返。底层 SQL 次数跟 FastMCP 的 `selectinload` 差不多，两者都用批量或者 join 解 N+1。差异在于，agent 用一次工具调用、一段自己写的查询字符串就到了那儿，而不是三次工具调用，或者预先写好的 `list_posts_with_posts_and_author_comments` 胶水工具。

这里我要诚实说一下 token 成本。`get_schema` 返回的是完整 SDL，大小随实体和字段数线性增长，跟 FastMCP 的工具描述增长同阶。Simple 模式并没有打破"schema token 线性增长"这条曲线，它只是把成本从 MCP 工具描述挪到了 GraphQL SDL。B1 真正的结构性收益是字段投影和嵌套解析，不是工具数减少。路径 B2 才是真正打破曲线的方案。

## 路径 B2：NexusX UseCase——要暴露业务方法时

> 完整代码见 [`nexusx_usecase.py`](./nexusx_usecase.py)

裸 CRUD 不总是你想暴露的东西。有时候正确的形状是派生视图，比如"列出 user 和 posts，并提供 post 数量"。它跟一行不是 1:1 映射。这就是 UseCase 模式派上用处的地方。

```python
from nexusx import (
    DefineSubset, ErManager, SubsetConfig,
    UseCaseAppConfig, UseCaseService,
    build_dto_select, create_use_case_graphql_mcp_server, query,
)


class PostSummary(DefineSubset):
    __subset__ = SubsetConfig(kls=Post, fields=["id", "title", "author_id"])


class UserSummary(DefineSubset):
    __subset__ = SubsetConfig(kls=User, fields=["id", "name", "email"])


class UserWithPostCount(DefineSubset):
    """Derived view — business-level shape, not a 1:1 row mapping."""
    __subset__ = SubsetConfig(kls=User, fields=["id", "name"])

    posts: list[PostSummary] = []
    post_count: int = 0

    def post_post_count(self):  # posts 加载完后由 Resolver 自动跑
        return len(self.posts)


# ErManager 把实体挂到 DataLoader，产出 Resolver，
# 由它填充关系字段并跑 post_* 派生方法。
er = ErManager(entities=[User, Post], session_factory=async_session)
Resolver = er.create_resolver()


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
    async def list_users_with_post_counts(cls) -> list[UserWithPostCount]:
        """List users with their post counts (derived)."""
        stmt = build_dto_select(UserWithPostCount)
        async with async_session() as s:
            rows = (await s.exec(stmt)).all()
        dtos = [UserWithPostCount(**dict(r._mapping)) for r in rows]
        return await Resolver().resolve(dtos)


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

这个时候 MCP 提供的是 4 个工具——结构性差异在这里才真正显出来。agent 从 `list_apps` 开始，这是一个极小的信封，只说"有什么 app"。如果在意，调 `describe_compose_schema(app)` 拿方法名（不含参数，仍然小）。看到有用的，再调 `describe_compose_method(app, svc, method)` 拿到这个方法的参数、返回类型、SDL 片段。最后才调 `compose_query(app, query)` 真正执行。

这里有个反直觉的细节。MCP 在外面套了一层，看起来是把 GraphQL 翻译成 MCP 工具调用，其实不是——`compose_query` 收的就是一段 GraphQL query 字符串，服务端用 graphql-core 把它 parse 成 AST，再按 selection 一层层调对应的 service 方法。换句话说，agent 写的 GraphQL 不被翻译，它本身就是 payload，原样穿过 MCP 信道。

这就是渐进披露被固化在协议层的样子。FastMCP 没有等价物——eager-load 客户端（现在主要是 Cursor）把所有工具描述一次性塞进上下文。新版 Claude Code 通过 Tool Search（按调用 lazy 拉 schema）绕开这个问题，但这是客户端的优化，不是协议保证。NexusX 让 agent 从"有什么"一层层钻到"这个方法要哪些参数"，只为它当前所在的那一层付 token。实体越多、业务方法越多，差距越大。FastMCP 项目里 30 多个工具是常态；NexusX 始终是 4 个。

而且，同一个 `UserService` 子类还能直接挂到 FastAPI：

```python
from nexusx import create_use_case_router

router = create_use_case_router(
    apps=[UseCaseAppConfig(name="blog", services=[UserService])],
)
# 挂到 FastAPI app 即可，OpenAPI 文档自动生成。
```

业务逻辑写一遍，MCP 和 REST 两面交付。（FastMCP 3.0 的 `FastMCP.from_fastapi` 桥从相反方向解决了同样的共存问题——先有 FastAPI，免费拿到 MCP。结构性差异是：B2 之所以能做到，*是因为* `UseCaseService` 这个抽象；FastMCP 则要求你底下已经有 FastAPI 路由。）

## 三条路径放在一起

三条路径都自动生成 `inputSchema` / `outputSchema`，这一块没差异。差异在每个实体的样板、查询形状、token 成本、REST 共存这几项。

路径 A 每个实体要付 N 个工具加 N 个 pydantic 模型，每种嵌套查询形状要付一个手写工具，每次 N+1 风险要付一次手动 `selectinload`，REST 要付一次完整的 FastAPI 重写。路径 B1 把大部分都折叠了：业务代码之外 0 样板，嵌套查询变成字段选择，DataLoader 自动处理 N+1。路径 B2 在此之上加了渐进披露——4 个固定工具，agent 一路钻进去，token 成本次线性。

字段投影在 B1 和 B2 里有，因为 GraphQL 原生就有。路径 A 给不了：`tools/call` 返回整个 pydantic 模型，不管 agent 想要 2 个字段还是 12 个，浪费随模型宽度线性涨。

组合查询在路径 A 里要么多次往返、要么预先写胶水工具。在 B1 和 B2 里是原生 GraphQL——一次协议往返，底层 SQL 次数相当。

REST 共存这一项上，差距已经抹平。FastMCP 3.0 的 `FastMCP.from_fastapi(app)` 从 REST 这一侧搭桥，先有 FastAPI，免费拿到 MCP 工具。NexusX 的 `UseCaseService` 从 MCP 这一侧搭桥，先有 service 方法，同时挂两边。两边都能做到一份代码、两面交付。B2 真正的结构性优势在渐进披露（上面那条），REST 不再是它发挥作用的地方。

## 为什么这样做是有效的

> 本节是作者的设计观点，不是客观对比。单独标出来，是因为类比式的论述不应被当成论证来读。

回头看路径 A 那几个痛点：over-fetch、没法选字段、没法组合查询、schema token 随工具数线性增长。前端工程师会很眼熟——这正是 2015 年 REST 加 BFF 时代天天吵架的事。GraphQL 当年就是为了解决这些而生的：字段选择、嵌套查询、强类型契约、按需组合。

agent 现在撞上类似的墙，只是换了个名字，叫"tools/list 的 token 预算"。

NexusX 不是"发明一个新 MCP 框架"。它把 SQLModel 实体当作 GraphQL schema 的单一真相源，让 agent 通过 GraphQL-over-MCP 拿到所有能力。让那一行 `create_simple_mcp_server` 真正起作用的，是这套继承关系——agent 拿到的不是"为 AI 设计的新协议"，而是"为前端设计的协议"。

这个抽象的价值有三层。第一，agent 的能力上限被抬高了——组合查询、字段投影、嵌套关系一次往返，这些是 GraphQL 的红利，agent 直接拿到。第二，存量 SQLModel 项目几乎免费接入——本来就有 entity，加几个 `@query` 装饰器就有 MCP。第三，`UseCaseService` 让 REST 跟 MCP 一起免费——同一份代码挂两面。

前提是：数据有结构、关系可遍历。如果 agent 要做的不是查数据库，而是发邮件、改图片、调外部 API，那 GraphQL 的抽象反而是阻碍，FastMCP 那种“工具 = 函数”的模型更合适。

## 少即是多：用约束换可预测

GraphQL 长期被诟病的一点是：它规定了语法，没规定风格。社区里推 schema 风格的声音从来不统一——Apollo 推 schema-first 加 business-aligned types，Facebook 早期按 UI 组件组织，绝大多数团队最后却把 ORM 模型直接映射成 GraphQL type。三种风格混用，schema 就开始失控。"找不到最佳实践"不是没努力，是协议没给约束。

NexusX 把选择拿掉了。两条 API 路径泾渭分明。

B1（Simple 模式）严格面向 model。schema 从 SQLModel 元数据自动生成，你不能乱加字段。它本质上是自动化的 ORM 到 GraphQL 映射，但因为是框架生成的，没有"团队要不要这样"的争论空间。

B2（UseCase 模式）严格面向 business。schema 必须挂在 `UseCaseService` 方法上，按业务用例组织——不写业务方法就没有字段。这一条把 Facebook 和 Apollo 推崇的 business-aligned 风格变成了唯一的代码路径。

用户没机会走偏。不是"找最佳实践"，是"框架替你选好了"。GraphQL 协议层仍然保留查询能力（字段选择、嵌套、组合），但 schema 设计自由度被剥夺了。这跟 Strawberry / Ariadne 那种"给你一个 GraphQL endpoint、schema 自己设计"是相反方向。

一句话：NexusX 不是更好的 GraphQL 框架，是更不自由的 GraphQL 框架——而不自由恰恰是它解决"找不到最佳实践"的方式。

## 代价是什么

上面的对比在结构性维度上偏向 GraphQL。为了平衡，下面这些是 agent 生成的 GraphQL 查询真实要付的代价，扁平 tool MCP 不用付。

首先是查询深度变成真实攻击面，但有成熟对策。GraphQL 服务传统上需要 cost analysis 或者深度限制（`user { posts { author { posts { ... }}}}`），来防止病态查询。Agent 在运行时生成查询，深度不可预测。对策是现成的——depth limiting、selection-set bounding、cost analysis——GraphQL Foundation 也在 2025 年 10 月专门成立了 AI Working Group 来处理 LLM 流量这类风险。扁平 tool MCP 没有等价问题，因为每个工具的形状在设计时就固定了。GraphQL 这一边的代价是，server 要主动执行这些限制，而不是从固定工具形状里被动继承安全。

接着说 B2 在这一项上风险显著更低。路径 B2 的 schema 不直接暴露 ORM 关系图，而是挂在 `UseCaseService` 方法返回的 DTO 上。DTO 是扁平的 pydantic 模型，不会像 ORM 关系那样自然形成 `User.posts` 和 `Post.author` 这种双向回引——你要嵌套就显式设计（比如 `PostSummary.author: UserSummary`），单向、不循环。换句话说，B2 给 agent 的是一棵单向树，不是循环图，结构上就没有 `posts.author.posts.author...` 这种深度爆炸的入口。代价是损失了 B1 那种"任意嵌套"的查询能力——要嵌套就得自己写组合方法。

然后是错误信息更难自纠。FastMCP 加 pydantic 给出字段级校验错误，比如"`email`：缺少必填字段"。GraphQL 的 parse / validation 错误常常是结构性的，比如"Expected Name, found `}`"，agent 更难从中恢复。

最后是 schema 变更影响半径更大。GraphQL 重命名一个字段，会断掉所有引用它的 agent 查询；扁平 tool 重命名只会断掉调那个工具的 caller。字段级耦合让 GraphQL schema 在 agent 流量下更脆弱。

这些不会抵消结构性收益（字段投影、渐进披露、嵌套组合），但这是把查询形状从设计时挪到运行时的代价。

### 什么时候用 NexusX，什么时候继续用 FastMCP

适合用 NexusX 的场景：项目本来就用 SQLModel 或者 SQLAlchemy；需要把数据库实体或业务方法暴露给 agent；REST 和 MCP 都要交付（UseCase 模式）；实体多、关系复杂、嵌套查询频繁。

适合继续用 FastMCP 的场景：MCP 工具是无状态函数（`send_email`、`resize_image`），跟数据库无关；工具数量少且稳定；非 Python 生态；需要非常精确地控制每个工具的 description 和参数顺序——以及重要的——需要 MCP 协议的完整三角（tools 加 resources 加 prompts）。

FastMCP 有一些真实存在、但 NexusX 不打算覆盖的能力。Tool annotations（`readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`）让 agent 判断调用是否安全。MCP Resources（`@mcp.resource("config://...")`）暴露 URI 寻址的配置、文档、文件。MCP Prompts（`@mcp.prompt`）定义可复用的提示模板。还有 `Context` 注入（progress、logging、state）、lifespan hook、server composition、in-process direct call（写测试方便）、内置 OAuth 加 bearer token auth。如果你的需求超出"把数据库暴露给 agent"——比如要混合 tools 加 resources 加 prompts——FastMCP 是更合适的基础。

## 其他 GraphQL-based MCP 实现

NexusX 不是这种范式的唯一实现。核心洞察——让 agent 用 GraphQL 查询数据，而不是调 N 个扁平工具——可以用多种技术栈落地。

Strawberry 加 FastMCP 是最接近的 Python 替代：类型驱动的 schema，支持 Federation 和 Subscription，但 DataLoader 要自己写，也失去了 opinionated 风格约束。Apollo Server 加轻量 MCP bridge 适合已经投入 Apollo 的 Node 项目，代价是多一层网络 hop 和部署复杂度。Ariadne 加 FastMCP 是 schema-first，适合想要 SDL 作契约的团队，但每个字段都要 resolver，样板多。裸 `graphql-core` 加 FastMCP 是最底层，控制力最强、样板也最多——适合实验或框架开发。

范式比任何单个实现都大。NexusX 是 opinionated 变体——SQLModel 单一真相源加两条强制风格路径。其他实现保留更多自由度，但失去了防止 schema 漂移的约束。

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

重启 Claude Desktop，对话里直接问"列出所有用户及其最近一篇 post"——agent 会自动发现 schema、构造 GraphQL 查询、一次往返拿到结果。

## 项目结构

```
nexusx-vs-fastmcp/
├── README.md                    # 英文版（主）
├── README.zh.md                 # 中文版（本文件）
├── pyproject.toml               # uv-managed（nexusx 从 git 拉）
├── init_db.py                   # 一次性为三个 db 建表 + 种子
├── fastmcp_handwritten.py       # 路径 A：7 个 @mcp.tool
├── nexusx_simple.py             # 路径 B1：@query/@mutation + create_simple_mcp_server
└── nexusx_usecase.py            # 路径 B2：UseCaseService + 4 层渐进披露
```

三个 server 用独立的 sqlite 文件（`blog_fastmcp.db` / `blog_nexusx_simple.db` / `blog_nexusx_usecase.db`），互不干扰，可以同时跑。

## 进一步阅读

- [NexusX MCP 服务文档](https://github.com/allmonday/nexusx/blob/main/docs/advanced/mcp_service.zh.md) — `create_simple_mcp_server` 与 `create_mcp_server`（多应用）的完整参数
- [NexusX UseCase 服务文档](https://github.com/allmonday/nexusx/blob/main/docs/advanced/use_case_service.zh.md) — 4 层渐进披露 MCP + FastAPI 同源
- [NexusX GraphQL 模式](https://github.com/allmonday/nexusx/blob/main/docs/guide/graphql_mode.zh.md) — MCP 底层使用的 GraphQL API 细节
- [NexusX 主项目](https://github.com/allmonday/nexusx) — 从 SQLModel 类自动生成 GraphQL + Core API + MCP
