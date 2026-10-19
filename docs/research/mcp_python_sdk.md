# MCP Python SDK (`mcp` 2.3.0) and protocol revision 2026-07-28: verified reference

Researched 2026-10-06. Method: installed `mcp==2.3.0` into a throwaway venv (Python 3.13, plus `fastapi`),
read the installed source, and ran three scripts: in-process `Client`, FastAPI mount over `httpx2.ASGITransport`
with auth, and the low-level server over HTTP. Each statement below is marked:

- **[src]**: read in the installed source.
- **[run]**: the scripts executed it and showed the behaviour.
- **[spec]**: taken from modelcontextprotocol.io/specification/2026-07-28.
- **[docs]**: taken from py.sdk.modelcontextprotocol.io.
- **UNVERIFIED**: not confirmed.

## 0. Decisions for Lokal69

- Use `mcp==2.3.0`. `FastMCP` no longer exists: `import mcp.server.fastmcp` raises `ModuleNotFoundError` and points to `MCPServer`. [src]
- **Use the low-level `mcp.server.Server`** for the tool surface. It is the only way to publish an exact
  `inputSchema` (for example `additionalProperties: false`). `MCPServer` builds schemas from function
  signatures and **silently ignores extra arguments**. [run]
- Build the ASGI app with `server.streamable_http_app(...)`. Mount it at `/` as the **last** route of the FastAPI app,
  and run `server.session_manager.run()` inside the FastAPI lifespan. [run]
- For 2026-07-28 clients the HTTP path is always stateless. For older clients, `stateless_http=True` removes the
  need for sticky sessions. Set `json_response=True` unless streaming progress is needed. [src][run]
- Auth: implement `TokenVerifier.verify_token()`, then pass `AuthSettings(...)` with `validate_token_resource=True`.
  The SDK then serves RFC 9728 metadata, returns 401/403 with `WWW-Authenticate`, and sets a contextvar that
  `get_access_token()` reads inside handlers. [run]

## 1. Package facts [src]

- **PyPI:** `mcp` 2.3.0, uploaded 2026-10-02.
  - 2.0.0 shipped 2026-07-28, the same day as the spec revision. 2.1.0 shipped 08-24 and 2.2.0 on 09-07.
  - The v1 line still gets releases (1.30.0 on 2026-09-07).
- **Python:** `Requires-Python >=3.10`. Classifiers cover 3.10–3.14. Python 3.13 is fine.
- **Wire types:** a separate package, `mcp-types==2.3.0` (deps: `pydantic>=2.12`, `typing-extensions>=4.13`).
  - `mcp.types` mirrors `mcp_types` exactly.
  - Models are snake_case with camelCase aliases (`input_schema`, `structured_content`, `is_error`, `_meta`→`meta`).
- **Runtime deps:**

  | Package | Required | Resolved here |
  |---|---|---|
  | `anyio` | `>=4.9` (`>=4.10` on 3.14) | 4.15.1 |
  | `starlette` | `>=0.27` (`>=0.48` on 3.14) | 1.7.0 |
  | `pydantic` | `>=2.12` | 2.13.5 |
  | `sse-starlette` | `>=3.0` | 3.5.0 |
  | `uvicorn` | `>=0.31.1` | 0.54.0 |
  | **`httpx2`** | `>=2.10` | 2.13.1 (import name `httpx2`; Pydantic's httpx fork, not `httpx`) |
  | `jsonschema` | `>=4.20` | 4.26.0 |
  | `pyjwt[crypto]` | `>=2.10.1` | 2.15.1 |
  | `python-multipart` | `>=0.0.9` | not recorded |
  | `opentelemetry-api` | `>=1.28` | 1.45.1 |
  | `typing-inspection` | `>=0.4.1` | not recorded |

  Extras: `cli` (typer, python-dotenv) and `rich`. `fastapi` 0.142.2 installed cleanly next to starlette 1.7.0.
- **Protocol versions** (`mcp_types.version`):
  - `HANDSHAKE_PROTOCOL_VERSIONS` = 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25.
  - `MODERN_PROTOCOL_VERSIONS` = (`"2026-07-28"`,).
  - `LATEST_PROTOCOL_VERSION` = `"2026-07-28"`.
- **Public server imports:**
  - `from mcp.server import Server, MCPServer, ServerRequestContext, NotificationOptions, CacheHint`
  - `from mcp.server.mcpserver import MCPServer, Context, Extension, MethodBinding, ...`
  - `from mcp.shared.exceptions import MCPError`
  - `from mcp.server.mcpserver.exceptions import ToolError`

## 2. Protocol eras and how the SDK routes them [src][run]

Every request goes through `StreamableHTTPSessionManager._handle_request`, which reads the `MCP-Protocol-Version` header.

- **Header present and not a handshake version** (for example `2026-07-28`, or any unknown value): the request goes to
  `_streamable_http_modern.handle_modern_request`.
  - This path handles one JSON-RPC request in and one response out.
  - It never sets `Mcp-Session-Id` and ignores the `stateless_http` flag.
  - GET and DELETE return `405` with `Allow: POST`.
  - A notification POST returns `202` with no body.
  - A batch or a posted response returns `400` with `-32600`.
  - A wrong `Accept` header returns `406`. A non-JSON `Content-Type` returns `400`.
- **Header absent or a handshake version:** the request takes the legacy Streamable HTTP path (`initialize` handshake).
  - `stateless_http=False` (default): the server mints `Mcp-Session-Id` (seen: `mcp-session-id` on `initialize`) and uses a GET SSE stream.
    Sessions are bound to the credential that created them; another token gets `404`.
  - `stateless_http=True`: the server creates a fresh transport per request and mints no session id.
- **Modern envelope:** every request must carry
  `params._meta["io.modelcontextprotocol/protocolVersion"]` and
  `["io.modelcontextprotocol/clientCapabilities"]`. `.../clientInfo` is optional.
  If either required key is missing, the server returns `400` with `-32602`. [run]
- **Header checks:**
  - The headers `MCP-Protocol-Version`, `Mcp-Method`, and `Mcp-Name` (for `tools/call`, `prompts/get`, `resources/read`)
    must equal the body values. Otherwise the server returns `400` with `-32020` HeaderMismatch. [run]
  - The protocol-version header is compared with the envelope **before** the supported-version check.
  - An unsupported body version returns `-32022` with `data={"supported": [...], "requested": ...}`.
  - An unknown method returns **`404`** with `-32601`. [run]
- **Error-to-HTTP mapping** (`mcp.shared.inbound.ERROR_CODE_HTTP_STATUS`):
  - `-32700`, `-32600`, `-32602`, `-32020`, `-32021`, `-32022` → 400.
  - `-32601` → 404.
  - Everything else, including `-32603` and tool `isError` results, → 200.
- **Response shaping on 2026-07-28:**
  - Every result gets `resultType` (default `"complete"`) and `_meta["io.modelcontextprotocol/serverInfo"]`.
  - `server/discover` is built in. It returns `supportedVersions`, `capabilities`, `ttlMs`, and `cacheScope`.
- **SSE mode** (`json_response=False`): the server answers with JSON unless the handler emits a notification or runs
  longer than 15 s. In those cases it commits to `text/event-stream`, sends `: ping` keepalives every 15 s, and sets
  `X-Accel-Buffering: no`. A client disconnect cancels the handler.
- **Removed or deprecated in 2026-07-28** (warnings come from `MCPDeprecationWarning`):
  - Server-initiated requests: `ctx.can_send_request` is False. Sampling and elicitation use MRTR (`InputRequiredResult`) instead.
  - Logging (`ctx.log`), roots, and client-to-server progress.
  - Change notifications now go out only on `subscriptions/listen` streams.

## 3. Building and mounting the HTTP server

`Server.streamable_http_app()` is the method that `MCPServer.streamable_http_app` delegates to. Signature [src]:

```python
streamable_http_app(*, streamable_http_path="/mcp", json_response=False, stateless_http=False,
    event_store=None, retry_interval=None, max_request_body_size=4*1024*1024,
    session_idle_timeout=1800, max_sessions=10_000, transport_security=None, host="127.0.0.1",
    auth: AuthSettings | None = None, token_verifier: TokenVerifier | None = None,
    auth_server_provider=None, custom_starlette_routes=None, debug=False) -> Starlette
```

The returned `Starlette` app contains:

- `Route(streamable_http_path, StreamableHTTPASGIApp)`. When a `token_verifier` is set, this route is wrapped in
  `RequireAuthMiddleware(required_scopes, resource_metadata_url)`.
- The PRM route, when `auth.resource_server_url` is set.
- App-level middleware `AuthenticationMiddleware(BearerAuthBackend)` and `AuthContextMiddleware`.
- `lifespan=lambda app: session_manager.run()`.

When `transport_security is None` **and** `host` is localhost, DNS-rebinding protection is enabled with **localhost-only
hosts and origins**. That is the default, so production must pass explicit `TransportSecuritySettings`.
An invalid Host returns 421. An invalid Origin returns 403. A missing Origin is allowed.

Mounting rules, verified with FastAPI 0.142.2 [run]:

- The PRM route is registered at an absolute path inside the sub-app (`/.well-known/oauth-protected-resource/mcp`).
- Mounting at **`/`** with the default `/mcp` path gives these results:
  - `POST /mcp` works, and so does `GET /.well-known/oauth-protected-resource/mcp`.
  - `/mcp/` answers 307 to `/mcp`.
  - FastAPI routes declared before the mount, such as `/health`, take precedence.
- `api.mount("/mcp", app_built_with(streamable_http_path="/"))` behaves differently:
  - `/mcp` answers 307 to `/mcp/`. The SDK client follows a same-origin 307, but other clients might not.
  - The PRM ends up at `/mcp/.well-known/...` and the root one returns **404**.
  - **Avoid this layout.**
- Mounted sub-app lifespans do **not** run. Without the parent lifespan below, requests fail with
  `RuntimeError: Task group is not initialized`. [docs][src]

```python
import contextlib
from fastapi import FastAPI
from mcp.server import Server
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

server = Server("lokal69", version="0.1.0", lifespan=app_lifespan,      # lifespan(server) -> AsyncCM[State]
                on_list_tools=list_tools, on_call_tool=call_tool,
                get_tool_input_schema=lambda n: SCHEMAS.get(n))          # skips a tools/list walk per call
mcp_app = server.streamable_http_app(
    json_response=True, stateless_http=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=["api.lokal69.example", "api.lokal69.example:*"],
        allowed_origins=["https://dash.lokal69.example"]),
    auth=AuthSettings(issuer_url="https://<project>.supabase.co/auth/v1",        # AS that issues tokens
                      resource_server_url="https://api.lokal69.example/mcp",
                      required_scopes=["mcp"], validate_token_resource=True),
    token_verifier=SupabaseJwtVerifier())

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    async with server.session_manager.run():     # REQUIRED when mounted; single-use per manager instance
        yield

api = FastAPI(lifespan=lifespan)
# ... normal REST routes first ...
api.mount("/", mcp_app)                           # last: catch-all
```

- `session_manager.run()` enters the server `lifespan` once for the whole process, and `ctx.lifespan_context` is what it yields. [run]
- `StreamableHTTPSessionManager.run()` can be entered **once per instance**, so tests must build a fresh app or server.
- CORS for a browser client [docs]:
  - `allow_headers`: `Authorization`, `Content-Type`, `Mcp-Method`, `Mcp-Name`, `Mcp-Protocol-Version`, and `Mcp-Param-*`.
  - `allow_methods`: POST (plus GET and DELETE for legacy).
  - `expose_headers=["Mcp-Session-Id"]`, needed for legacy only.
- `max_request_body_size` (4 MiB) produces a 413.
- Multiple replicas [docs][src]:
  - Modern clients do not need sticky sessions.
  - `MCPServer` keeps the `subscriptions/listen` bus in memory by default. Pass `subscriptions=False` or a custom
    `SubscriptionBus`. With the default, discover advertises `tools.listChanged: true` because listen is served. [run]
  - MRTR `requestState` needs `RequestStateSecurity(keys=[shared])` plus identical server names.
- Behind a TLS proxy, run `uvicorn --proxy-headers --forwarded-allow-ips=...`. [docs]

## 4. Tools: explicit schema, structured output, annotations, errors

The low-level server never validates `arguments` against `inputSchema` and never validates `structured_content`
against `outputSchema`; both are the handler's job. There is no `jsonschema` use on the server side. [src][docs]
Pydantic models with `extra="forbid"` produce `"additionalProperties": false` and also do the validation [run]:

```python
import mcp.types as types
from mcp.server import ServerRequestContext
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    make: str = Field(min_length=1)
    max_price_eur: int | None = Field(default=None, ge=0)

class SearchOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int
    ids: list[str]

SEARCH = types.Tool(name="search_listings", description="...",
    input_schema=SearchArgs.model_json_schema(),          # -> {"additionalProperties": false, "type": "object", ...}
    output_schema=SearchOut.model_json_schema(),
    annotations=types.ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))

async def list_tools(ctx: ServerRequestContext, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[SEARCH])

async def call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    if params.name != "search_listings":
        raise MCPError(code=types.INVALID_PARAMS, message=f"Unknown tool: {params.name}")   # protocol error
    try:
        args = SearchArgs.model_validate(params.arguments or {})
    except ValidationError as e:                                                          # tool execution error
        return types.CallToolResult(content=[types.TextContent(type="text", text=str(e))], is_error=True)
    out = SearchOut(count=1, ids=["x"])
    return types.CallToolResult(content=[types.TextContent(type="text", text=out.model_dump_json())],
                                structured_content=out.model_dump(mode="json"))
```

**Schema output.** Pydantic emits `title` keys and `anyOf [.., {"type": "null"}]` for optionals, which is valid 2020-12.
Pydantic also emits `$defs`/`$ref` for nested models. The spec forbids `x-mcp-header` behind `$ref`, so inline such
schemas or keep header-mirrored params flat.

**`ToolAnnotations` fields:** `title`, `read_only_hint`, `destructive_hint`, `idempotent_hint`, `open_world_hint`.
**`Tool` fields:** `name`, `title`, `description`, `input_schema`, `output_schema`, `icons`, `annotations`, `meta`
(`_meta`), and `execution` (2025-11-25 only).

`MCPServer` behaviour [run]:

- `@mcp.tool(name=, title=, description=, annotations=, icons=, meta=, structured_output=)` builds `inputSchema`
  from the signature, with no `additionalProperties`. Extra arguments are dropped silently.
- Return-type rules:
  - A pydantic model or TypedDict gives an `outputSchema` plus `structuredContent`.
  - `list[...]` or a primitive is wrapped as `{"result": ...}`.
  - `dict` gives **no** `outputSchema` (unstructured).
  - Returning `CallToolResult` directly bypasses conversion.
- A `Context` parameter is injected by type annotation.
- Sync tools run in a worker thread.

Errors [src][run]:

| Situation | Result on the wire |
|---|---|
| Handler (low-level) returns `CallToolResult(is_error=True)` | result, `isError: true`, HTTP 200 |
| `MCPServer`: `raise ToolError("msg")` | `isError: true`, text `"Error executing tool <name>: msg"` |
| `MCPServer`: argument validation failure | `isError: true` with the pydantic message |
| `MCPServer`: any other exception | `isError: true`, text only `"Error executing tool <name>"`; traceback logged server-side |
| `MCPServer`: unknown tool | `isError: true` `"Unknown tool: x"`. Note: the spec example uses a protocol error `-32602`. |
| `raise MCPError(code=..., message=..., data=...)` anywhere | JSON-RPC error. In the low-level server, uncaught exceptions become `-32603` with a generic message. |

## 5. Request context and the authenticated principal [src][run]

- Low-level handlers receive `ctx: ServerRequestContext[LifespanT, Request]`, which provides:
  `session`, `lifespan_context`, `protocol_version`, `method`, `params` (raw), `request_id`, `meta`, and `request`.
  - Over HTTP, `request` is the Starlette `Request` (`ctx.request.headers`, `ctx.request.user`).
  - On stdio, `request` is None.
  - Client capabilities are available at `ctx.session.client_capabilities`.
- `MCPServer` tools receive `ctx: Context`, which provides:
  `ctx.request_context` (the object above), `ctx.headers` (verified that `X-Trace` arrived), `ctx.protocol_version`,
  `ctx.request_id`, `ctx.client_capabilities`, `ctx.report_progress()`, `ctx.read_resource()`, and `ctx.notify_*_changed()`.
- To get the principal, call `from mcp.server.auth.middleware.auth_context import get_access_token`, which returns
  `AccessToken | None`.
  - The value is set per HTTP request by `AuthContextMiddleware` and propagated into handler tasks.
    This works for both modern and legacy, stateless and stateful. [run]
  - Alternative: `ctx.request.user`, an `AuthenticatedUser` with `.access_token` and `.scopes`.
- `AccessToken` fields: `token`, `client_id`, `scopes`, `expires_at`, `resource`, `subject`, `claims`.
- `principal_components(token)` returns `(client_id, claims["iss"], subject)`.
  `mcp.server.mcpserver.authenticated_principal(ctx)` gives the JSON form of that tuple.
- Headers are client input. Never treat one as identity.

## 6. Built-in auth for a protected resource server [src][run]

- **`TokenVerifier`** is a `Protocol` with one method: `async def verify_token(self, token: str) -> AccessToken | None`.
  For a Supabase JWT, verify signature, issuer, audience, and expiry yourself (pyjwt is already a dependency),
  then set `resource`, `subject`, `scopes`, and `claims`.
- **`AuthSettings`** (`mcp.server.auth.settings`):
  - `issuer_url` (required).
  - `resource_server_url` (required field; may be None).
  - `required_scopes`.
  - `validate_token_resource`. Leaving it unset while setting `resource_server_url` raises a deprecation warning;
    the default becomes True in 3.0.
  - `service_documentation_url`, `client_registration_options`, `revocation_options`, `identity_assertion_enabled`.
    These are only used with an `auth_server_provider`.
- `MCPServer(token_verifier=..., auth=...)`: providing both `auth_server_provider` and `token_verifier` is an error,
  and so is either one without `auth`.
- **`BearerAuthBackend`** ignores a non-`Bearer` header. It rejects these tokens, treating them as unauthenticated:
  - `verify_token` returned None;
  - `expires_at` is in the past;
  - with `validate_token_resource=True`, `AccessToken.resource` differs from `resource_server_url` (URL compare, trailing slash ignored).
- **`RequireAuthMiddleware`** sends these responses (seen in the runs):
  - No or invalid token → `401`, `WWW-Authenticate: Bearer error="invalid_token", error_description="Authentication required", resource_metadata="https://api.example.com/.well-known/oauth-protected-resource/mcp"`, JSON body `{"error","error_description"}`.
  - Missing a `required_scopes` entry → `403`, `error="insufficient_scope", error_description="Required scope: mcp:read", resource_metadata=...`.
  - **Gap vs. spec:** there is no `scope="..."` parameter in either challenge, though the spec says SHOULD.
    To add it, wrap the endpoint with your own ASGI middleware.
- **PRM (RFC 9728):** `create_protected_resource_routes()` serves GET/OPTIONS at
  `build_resource_metadata_url(resource_server_url)`. That is the well-known prefix inserted before the path:
  `https://host/mcp` → `/.well-known/oauth-protected-resource/mcp`.
  - CORS is enabled and `Cache-Control: public, max-age=3600` is set.
  - Body (seen):
    `{"resource","authorization_servers":[issuer_url],"scopes_supported":required_scopes,"bearer_methods_supported":["header"]}`.
  - There is no root `/.well-known/oauth-protected-resource`. That is acceptable because the spec requires
    `resource_metadata` in the header **or** one well-known location.
- **Scopes:** only global `required_scopes` are enforced. Per-tool checks are up to you:
  - Inside the handler: check `get_access_token().scopes`, then return `isError` or raise `MCPError`. Either way the HTTP status is 200.
  - To get a spec-style 403 per tool: on the modern path an outer ASGI middleware can trust `Mcp-Name`,
    because the SDK rejects a header/body mismatch afterwards. This is an inference from [src], not tested.
- **Routes:** `MCPServer.custom_route(...)` routes are **never** authenticated. [docs]

## 7. Custom JSON-RPC methods and the Events extension [src][run]

- `server.add_request_handler(method, ParamsModel, handler)` registers any method name.
  - The handler signature is `async (ctx, params) -> BaseModel | dict | None`.
  - `ParamsModel` should subclass `types.RequestParams`.
  - Registering a spec method replaces the built-in handler; `"initialize"` cannot be overridden.
  - `add_notification_handler` exists for notifications.
  - Method existence is decided by dispatch, so custom methods route on both eras and unknown ones return 404/`-32601`.
  - Verified: `events/list` was registered and called in auto and legacy modes. On 2026-07-28 the result gets
    `resultType:"complete"` and `_meta.serverInfo` added.
- `MCPServer(extensions=[MyExt()])`: subclass `Extension` with `identifier="com.example/x"` (reverse-DNS, validated).
  - It can contribute `tools()`, `resources()`, `methods()` → `MethodBinding(method, params_type, handler, protocol_versions=None)`,
    and `intercept_tool_call`.
  - It is advertised under `capabilities.extensions[identifier]`.
  - `MethodBinding` refuses spec method names.
  - `require_client_extension(ctx, id)` raises `-32021`.
- `server.middleware.append(async (ctx, call_next) -> result)` wraps every inbound message. It is provisional API.
- **Events extension (`events/list`, `events/poll`, `events/stream`, `events/subscribe`, `events/unsubscribe`):**
  - **Not in the Python SDK** (no `events/` anywhere in `mcp` or `mcp_types`).
  - It is a draft from the `modelcontextprotocol/experimental-ext-triggers-events` working group; the design sketch is dated 2026-02-19.
  - There is an open TS-SDK proposal (#2945).
  - The sketch advertises a **top-level** `capabilities.events = {listChanged}`, but `mcp_types.ServerCapabilities`
    **drops unknown keys**. To advertise it you would need to:
    - use `capabilities.extensions[...]` or `experimental`; or
    - rewrite the `server/discover` / `initialize` result dict in a `Server.middleware`. This is possible because the
      middleware receives the wire dict, but it was not run (UNVERIFIED).
  - Implement the methods as custom handlers. See `docs/research/mcp_events_and_webhooks.md` for the wire shapes.
  - `events/stream` would need SSE notifications. On the modern path, `ctx.session` notifications go to the request's
    SSE sink only when `json_response=False` (UNVERIFIED for custom methods).

## 8. Testing [src][run][docs]

- **In-process:** `from mcp import Client`, then `async with Client(server_or_mcpserver, raise_exceptions=True) as c:`
  followed by `await c.list_tools()`, `await c.call_tool(name, args)` → `CallToolResult(content, structured_content, is_error, meta)`.
  - `mode="auto"` (default) dispatches directly at 2026-07-28 with no JSON-RPC framing.
  - `mode="legacy"` runs the `initialize` handshake over in-memory streams (`mcp.client._memory.InMemoryTransport`).
  - Use pytest with `anyio` (`@pytest.mark.anyio` plus an `anyio_backend` fixture returning `"asyncio"`).
- Custom methods: `await c.session.send_request(MyRequest(params=...), TypeAdapter(dict[str, Any]) | ResultModel)`.
  Here `MyRequest(types.Request[MyParams, str])` declares `method: str = "events/list"`. A bare `dict` result type fails.
- **The auth principal is NOT available in-process.** Setting `auth_context_var` before `Client(...)` did not reach
  the handler in either mode. [run] Test principal-dependent logic over HTTP, or behind your own injectable seam.
- **HTTP in-process**, which covers the auth, header, and status-code paths:

```python
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
async with api.router.lifespan_context(api):                     # ASGITransport does not run lifespan
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=api), base_url="https://api.example.com",
                                  headers={"Authorization": "Bearer good"}) as h:
        r = await h.post("/mcp", json=body, headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call",
                         "Mcp-Name": "whoami", "Accept": "application/json, text/event-stream"})   # raw wire assertions
        async with Client(streamable_http_client("https://api.example.com/mcp", http_client=h)) as c:  # or mode="legacy"
            res = await c.call_tool("whoami", {})
```

  - `streamable_http_client(url, *, http_client: httpx2.AsyncClient | None, terminate_on_close=True, max_sse_event_size=...)`
    must receive an **`httpx2`** client, not an `httpx` one.
  - The base URL host must be in `allowed_hosts`.

## 9. Spec 2026-07-28: normative points for a server [spec]

Streamable HTTP (`basic/transports/streamable-http`):

- **Endpoint:** a single MCP endpoint that supports POST. Each client message is its own POST with
  `Accept: application/json, text/event-stream`. The body is one request or notification; no batches and no responses.
  - A notification gets `202` with no body.
  - A request gets either `application/json` or `text/event-stream`.
  - SSE may carry notifications related to that request and must not carry server requests. The final response SHOULD close the stream.
  - Send `X-Accel-Buffering: no`. Closing the stream **MUST** be treated as cancellation.
  - `Last-Event-ID` is not supported.
- **Security:** servers MUST validate `Origin` and answer 403 when it is present and invalid. They SHOULD bind to
  localhost when local and SHOULD authenticate every connection.
- **Request headers:**
  - `MCP-Protocol-Version` is required. It must equal `_meta.io.modelcontextprotocol/protocolVersion`; otherwise 400 with `-32020`.
  - An unimplemented version gets 400 with `UnsupportedProtocolVersionError` (`-32022`, listing the supported versions).
  - An unimplemented method gets **404** with `-32601`.
  - A missing header MAY be treated as 2025-03-26, to support pre-2025-06-18 clients.
  - `Mcp-Method` is required on all requests. `Mcp-Name` is required for `tools/call`, `resources/read`, `prompts/get`.
  - Values may use the `=?base64?...?=` encoding and must be decoded before comparison.
  - Any mismatch, missing required header, or invalid characters gets 400 with `-32020`.
- **`x-mcp-header`:**
  - Allowed only on string, integer, or boolean properties reachable through `properties` keys alone.
  - Header names must be RFC 9110 tokens, case-insensitively unique.
  - Each one produces an `Mcp-Param-{name}` header, which the server MUST validate against the body.
  - Do not mark sensitive params.
  - The SDK validates these: with `get_tool_input_schema`, or else by calling your `tools/list` handler.
- **Removed in this revision:** `Mcp-Session-Id`, the GET stream, and DELETE.
  A modern-only server SHOULD answer GET/DELETE with 405 and ignore `Mcp-Session-Id` and `Last-Event-ID`.
  The SDK serves both eras on one endpoint.

Base protocol (`basic/index`):

- Requests MUST carry `_meta` `protocolVersion` and `clientCapabilities`. A missing one gets `-32602` with HTTP 400.
- Servers MUST NOT rely on undeclared client capabilities; they return `-32021` with HTTP 400 instead.
- Results MUST include `resultType`.
- Servers SHOULD put `io.modelcontextprotocol/serverInfo` in every result's `_meta`.
- The protocol is stateless: no context from prior requests. Cross-request state needs explicit ids passed by the client.
- Codes `-32020..-32099` are reserved for MCP; do not invent codes there. Application codes go outside `-32768..-32000`.
- `-32002` (resource not found, the old code) and `-32042` MUST NOT be emitted.

Authorization (`basic/authorization`, `.../authorization-server-discovery`):

- **Discovery:** the server MUST implement RFC 9728 PRM, with `authorization_servers` holding at least one entry.
  It MUST expose PRM through `WWW-Authenticate: Bearer resource_metadata="..."` on 401 **or** a well-known URI,
  either path-inserted (`/.well-known/oauth-protected-resource/mcp`) or at the root.
- **Scopes:** the server SHOULD include `scope="..."` in the challenge.
- **Token validation:**
  - Tokens MUST be validated (OAuth 2.1 §5.2) **and** MUST have been issued for this server as audience (RFC 8707).
  - Invalid or expired tokens get 401.
  - The server MUST NOT accept or pass through other tokens.
  - Tokens arrive in the `Authorization: Bearer` header on every request, never in the query string.
- **Status codes:** 401 = missing or invalid token. 403 = insufficient scope, with
  `WWW-Authenticate: Bearer error="insufficient_scope", scope="...", resource_metadata="..."` (SHOULD).
  400 = malformed request.
- **Scope hierarchy:** servers MUST honour scope hierarchies. They SHOULD NOT advertise `offline_access`.

Tools (`server/tools`):

- **Capability and listing:** declare `tools` (`listChanged`).
  - `tools/list` MUST NOT vary per connection but MAY vary by the request's authorization (for example scopes).
  - The order SHOULD be deterministic. `tools/list` supports pagination plus `ttlMs` and `cacheScope`.
- **Schemas:**
  - `inputSchema` must be a non-null JSON Schema, 2020-12 by default. For no parameters,
    `{"type":"object","additionalProperties":false}` is recommended.
  - `outputSchema` may be any 2020-12 schema; arrays are allowed in 2026-07-28. When present,
    `structuredContent` (any JSON value) MUST conform to it.
  - Structured results SHOULD also be serialized into a `TextContent` block.
- **Naming:** names SHOULD be 1–128 characters from `[A-Za-z0-9_.-]`.
- **Errors:** unknown tools and malformed requests are JSON-RPC errors (example: `-32602 "Unknown tool: x"`).
  Input-validation, API, and business errors are `isError: true` results.
- **Security:** servers MUST validate all inputs, enforce access control, rate-limit calls, and sanitize outputs.
- **Annotations** are hints; clients treat them as untrusted.

## 10. Gotchas and UNVERIFIED items

- `MCPServer` tool schemas cannot express `additionalProperties:false` without private hacks such as
  `_tool_manager` or a custom `Tool`. Use the low-level `Server`.
- The default `host="127.0.0.1"` turns on localhost-only Host/Origin checks. Behind a real domain, every request
  gets 421 unless `transport_security` is passed.
- A POST without `Content-Type: application/json` gets 400 `Invalid Content-Type header`, even with protection disabled.
- On the legacy stateless path, `ctx.can_send_request` is False. On 2026-07-28 the SDK masks it off for every request.
- UNVERIFIED:
  - Notification delivery from custom-method handlers on the modern SSE path.
  - Rewriting the result dict in `Server.middleware` to add `capabilities.events`.
  - Behaviour on Python 3.14 (only 3.13 was run).
  - `event_store` resumability (legacy only).
- Sources:
  - https://pypi.org/project/mcp/2.3.0/
  - https://py.sdk.modelcontextprotocol.io/ (`run/asgi`, `run/authorization`, `run/deploy`, `get-started/testing`, `advanced/low-level-server`, `protocol-versions`)
  - https://modelcontextprotocol.io/specification/2026-07-28/ (`basic/index`, `basic/transports/streamable-http`, `basic/authorization`, `server/tools`)
  - https://github.com/modelcontextprotocol/experimental-ext-triggers-events
  - https://github.com/modelcontextprotocol/typescript-sdk/issues/2945
