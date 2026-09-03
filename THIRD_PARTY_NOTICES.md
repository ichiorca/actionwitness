# Third-party notices

ActionWitness is licensed under Apache-2.0. It depends on the following open-source
projects. Their copyrights and license terms remain with their respective owners.

| Component | License | Use in ActionWitness |
|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | HTTP service boundary |
| [Starlette](https://github.com/encode/starlette) | BSD-3-Clause | ASGI foundation |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI server |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT | Boundary validation and serialization |
| [HTTPX](https://github.com/encode/httpx) | BSD-3-Clause | Adapter and proxy transport |
| [aiosqlite](https://github.com/omnilib/aiosqlite) | MIT | Async SQLite access |
| [React](https://github.com/facebook/react) | MIT | Workspace and storefront UI |
| [Vite](https://github.com/vitejs/vite) | MIT | Frontend build |
| [Vitest](https://github.com/vitest-dev/vitest) | MIT | Frontend tests |
| [TypeScript](https://github.com/microsoft/TypeScript) | Apache-2.0 | Strict frontend types |
| [ESLint](https://github.com/eslint/eslint) | MIT | Frontend linting |
| [typescript-eslint](https://github.com/typescript-eslint/typescript-eslint) | MIT / BSD-2-Clause | Type-aware linting |
| [use-webmcp-tool](https://www.npmjs.com/package/use-webmcp-tool) | MIT | WebMCP registration lifecycle |
| [webmcp-types](https://www.npmjs.com/package/webmcp-types) | MIT | WebMCP TypeScript declarations |
| [webmcp-evals](https://github.com/webmachinelearning/webmcp) | Apache-2.0 | Imported call-level evaluator reports |
| [pytest](https://github.com/pytest-dev/pytest) | MIT | Python tests |
| [Ruff](https://github.com/astral-sh/ruff) | MIT | Python formatting and linting |
| [uv](https://github.com/astral-sh/uv) | Apache-2.0 / MIT | Python workspace and dependency management |

ActionWitness implements JSON canonicalization from
[RFC 8785](https://www.rfc-editor.org/rfc/rfc8785). The specification is not a
bundled software dependency.

Exact versions and transitive packages are recorded in `uv.lock` and the two
committed `package-lock.json` files. Those lockfiles are the source of truth for a
particular checkout.

The generated presentation background in
`docs/assets/actionwitness-signal-background.png` was created for this repository
with OpenAI's built-in image generation tool. The remaining presentation graphics
are original SVG compositions stored beside their rendered PNG versions.
