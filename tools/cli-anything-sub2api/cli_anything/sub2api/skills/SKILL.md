---
name: >-
  cli-anything-sub2api
description: >-
  Command-line interface for Sub2Api - Stateful CLI harness for operating a **Sub2API** deployment through its native administrator HTTP AP...
---

# cli-anything-sub2api

Stateful CLI harness for operating a **Sub2API** deployment through its native administrator HTTP API. The real backend is the deployed Go service at `SUB2API_BASE_URL`; this harness never reimplements account or gateway behavior.

## Installation

This CLI is installed as part of the cli-anything-sub2api package:

```bash
pip install cli-anything-sub2api
```

**Prerequisites:**
- Python 3.10+
- sub2api must be installed on your system


## Usage

### Basic Commands

```bash
# Show help
cli-anything-sub2api --help

# Start interactive REPL mode
cli-anything-sub2api

# Create a new project
cli-anything-sub2api project new -o project.json

# Run with JSON output (for agent consumption)
cli-anything-sub2api --json project info -p project.json
```

### REPL Mode

When invoked without a subcommand, the CLI enters an interactive REPL session:

```bash
cli-anything-sub2api
# Enter commands interactively with tab-completion and history
```


## Command Groups


### Cli

Operate a deployed Sub2API instance through its native administrator API.

| Command | Description |
|---------|-------------|

| `status` | Verify native health and, when authenticated, report the deployed version. |

| `api` | Call a supported admin endpoint not yet given a dedicated command. |

| `repl` | Interactive shell; one-shot API semantics remain unchanged. |



### Profile

Manage the non-secret local connection profile.

| Command | Description |
|---------|-------------|

| `show` | Show the saved base URL without reading or displaying credentials. |

| `set` | Save a base URL locally; API keys and JWTs are deliberately excluded. |



### Accounts

Inspect and operate upstream provider accounts through admin account routes.

| Command | Description |
|---------|-------------|

| `list` | List native account records with the same filters as the admin console. |

| `get` | Fetch one upstream account by ID. |

| `create` | Create an account using the native account payload schema. |

| `update` | Apply a partial update to one account with the backend's native semantics. |

| `delete` | Delete one account after explicit terminal confirmation. |

| `command` | Read a native account diagnostic resource. |

| `set-schedulable` | Enable or disable an account for scheduler selection. |

| `set-status` | Set the backend account status field. |

| `action` | Run a supported native account action without inventing business behavior. |

| `bulk-update` | Update many accounts through the native bulk-update endpoint. |

| `export` | Export sensitive credentials only to an explicit local file. |



### Grok

Operate Grok/xAI OAuth administration routes.

| Command | Description |
|---------|-------------|

| `import-password` | Convert email/password to Grok OAuth and create one account.

    The password is accepted only from stdin, used for the one authorization
    request, and never included in output or the persisted account payload.
    The server-side password authorization feature must already be enabled. |



### Grok Oauth

Inspect Grok OAuth capabilities and perform one-shot authorization.

| Command | Description |
|---------|-------------|

| `capabilities` | Read whether optional Grok password authorization is enabled. |



### Users

Manage Sub2API panel users through documented user administration routes.

| Command | Description |
|---------|-------------|

| `list` | List panel users with common native query filters. |

| `get` | Fetch one user by ID. |

| `create` | Create a panel user using the server's native schema. |

| `update` | Apply a partial user update. |

| `delete` | Delete one user after explicit confirmation. |

| `balance` | Change the internal ledger balance through the dedicated balance route.

    Writing `balance` via `users update` is not the audited path; this route
    records balance history and enforces set/add/subtract semantics. |



### Audit Logs

Read native admin audit logs (who called which admin route, and when).

| Command | Description |
|---------|-------------|

| `list` | List audit-log rows, newest first. |



### Groups

Manage API consumer groups through native group endpoints.

| Command | Description |
|---------|-------------|



### Proxies

Manage upstream proxy records through native proxy endpoints.

| Command | Description |
|---------|-------------|

| `export` | Export sensitive proxy credentials only to an explicit local file. |



### Redeem Codes

Inspect and issue native Sub2API redemption codes.

| Command | Description |
|---------|-------------|

| `list` | List redemption codes using the native pagination endpoint. |

| `get` | Fetch one redemption code by ID. |

| `stats` | Fetch redemption code totals from the native stats endpoint. |

| `generate` | Generate codes; pass a stable idempotency key for payment-related flows. |

| `expire` | Expire an existing redemption code. |



### Dashboard

Read native Sub2API dashboard metrics without screen-scraping the UI.

| Command | Description |
|---------|-------------|

| `stats` | Fetch global dashboard statistics. |

| `snapshot` | Fetch the v2 dashboard snapshot with optional native date filters. |



### Settings

Inspect or update native administrator settings.

| Command | Description |
|---------|-------------|

| `get` | Read the current administrator settings. |

| `update` | Apply a partial native administrator-settings update. |



### Ops

Read operational signals directly from the native administration API.

| Command | Description |
|---------|-------------|

| `concurrency` | Fetch real-time concurrency statistics. |

| `realtime-traffic` | Fetch the real-time traffic summary. |

| `ingress-rejections` | Fetch bounded ingress-admission rejection aggregates. |




## Examples


### Create a New Project

Create a new sub2api project file.

```bash
cli-anything-sub2api project new -o myproject.json
# Or with JSON output for programmatic use
cli-anything-sub2api --json project new -o myproject.json
```


### Interactive REPL Session

Start an interactive session with undo/redo support.

```bash
cli-anything-sub2api
# Enter commands interactively
# Use 'help' to see available commands
# Use 'undo' and 'redo' for history navigation
```


## State Management

The CLI maintains session state with:

- **Undo/Redo**: Up to 50 levels of history
- **Project persistence**: Save/load project state as JSON
- **Session tracking**: Track modifications and changes

## Output Formats

All commands support dual output modes:

- **Human-readable** (default): Tables, colors, formatted text
- **Machine-readable** (`--json` flag): Structured JSON for agent consumption

```bash
# Human output
cli-anything-sub2api project info -p project.json

# JSON output for agents
cli-anything-sub2api --json project info -p project.json
```

## For AI Agents

When using this CLI programmatically:

1. **Always use `--json` flag** for parseable output
2. **Check return codes** - 0 for success, non-zero for errors
3. **Parse stderr** for error messages on failure
4. **Use absolute paths** for all file operations
5. **Verify outputs exist** after export operations

## More Information

- Full documentation: See README.md in the package
- Test coverage: See TEST.md in the package
- Methodology: See HARNESS.md in the cli-anything-plugin

## Version

1.0.0