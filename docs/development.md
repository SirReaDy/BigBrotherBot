| [Overview](../README.md) | [CLI](cli.md) | [Plugins](plugins.md) | [Deployment](deployment.md) | [Commands](commands.md) | [Configuration](configuration.md) | [Games](games.md) | **Development** | [Migrating](migrating.md) |
|---|---|---|---|---|---|---|---|---|

# Development

Two things a contributor needs that are not obvious from the code: which parts of the data model are
fixed by the classic bot rather than chosen here, and how to run the checks.

## Domain rules

Preserved from the classic bot for data compatibility:

**Groups** — `id` is a power-of-two membership *bit*; a client's `group_bits` is the bitwise OR of
them. `level` (0–100) is the permission ordinal that commands compare against.

| bit | keyword | name | level |
|---|---|---|---|
| 128 | superadmin | Super Admin | 100 |
| 64 | senioradmin | Senior Admin | 80 |
| 32 | fulladmin | Full Admin | 60 |
| 16 | admin | Admin | 40 |
| 8 | mod | Moderator | 20 |
| 2 | reg | Regular | 2 |
| 1 | user | User | 1 |
| 0 | guest | Guest | 0 |

**Penalties** — active means `inactive=0 AND (time_expire=-1 OR time_expire>now)`; `-1` is permanent.
`duration` is in minutes. Lifting a penalty sets `inactive=1` — never a physical delete, so the audit
trail survives.

**Identity** — `guid` is the natural key; `id` is the surrogate. Alias and IP-alias tables keep
history with `num_used` counters. Timestamps are Unix epoch integers.

**Resilience** — the bot keeps running when the database is down.

## Working on the code

```bash
python -m pip install -e ".[dev]"
pytest
ruff check src tests
```
