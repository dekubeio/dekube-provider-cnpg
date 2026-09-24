# dekube-provider-cnpg

![vibe coded](https://img.shields.io/badge/vibe-coded-ff69b4)
![python 3](https://img.shields.io/badge/python-3-3776AB)
![heresy: 7/10](https://img.shields.io/badge/heresy-7%2F10-orange)
![stdlib only](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen)
![public domain](https://img.shields.io/badge/license-public%20domain-brightgreen)

CloudNativePG provider for [dekube](https://dekube.io).

## Handled kinds

- `Cluster` -- converts CNPG PostgreSQL clusters into compose services
- `Pooler` -- registers PgBouncer pooler names as DNS aliases (no container generated)

## What it does

Emulates the CNPG operator's behavior for a compose environment. The CNPG Docker image (`ghcr.io/cloudnative-pg/postgresql:*`) is not usable outside K8s (requires the CNPG controller binary + API access), so this provider maps to the standard `postgres:*` Docker Hub image.

**Cluster CR:**
- Maps `spec.imageName` version tag to standard postgres image (`ghcr.io/cloudnative-pg/postgresql:18.3` -> `postgres:18.3`)
- Resolves the application owner credentials from `bootstrap.initdb.secret` or auto-generates them into `<cluster>-app` (idempotent across runs; keys `username`, `user`, `password`, `dbname`, `host`, `port`, `uri`, like CNPG)
- Runs the image as CNPG does: `POSTGRES_USER=postgres` with the superuser password, `POSTGRES_DB=postgres`; an initdb script creates the owner role (`bootstrap.initdb.owner`, default: the database name) and the application database (`bootstrap.initdb.database`, default `app`) from `CNPG_OWNER` / `CNPG_OWNER_PASSWORD` / `CNPG_DATABASE`
- Generates superuser secret with connection URIs (`uri`, `fqdn-uri`, `jdbc-uri`, `pgpass`) emulating CNPG operator output, but only when `spec.enableSuperuserAccess: true` (disabled by default, matching CNPG) — the postgres superuser password is still generated and persisted on disk either way (needed to start the container), it just isn't published as a `<cluster>-superuser` Secret other services can reference
- Writes `postgresql.conf` from `spec.postgresql.parameters` (plus `listen_addresses = '*'` unless set)
- Runs `postInitSQL` after the bootstrap, as superuser in the `postgres` database (as CNPG does)
- Persists PGDATA as PVC `<cluster>-1` (CNPG's PVC name), mapped through `volumes:` in `dekube.yaml` like any other PVC
- Registers `-rw`, `-r`, `-ro` service aliases in compose DNS (all point to the same container -- compose is single-instance)

**TLS (requires dekube-converter-cert-manager):**
- Emits synthetic Certificate/Issuer manifests (priority 90, before cert-manager at 100)
- cert-manager generates PEM certs; provider mounts them at startup
- `pg_hba.conf` enforces `hostssl` with `scram-sha-256` for all remote connections
- Key file permissions handled via `install -m 600 -o postgres` at container startup (PG refuses world-readable keys, and bind mounts inherit host umask)
- Without cert-manager: PG runs without SSL (graceful degradation)

**Pooler CR:**
- Pooler name registered as DNS alias to the cluster service
- No PgBouncer container -- apps connect directly to PostgreSQL

## Priority

Two classes:
- `CnpgIndexer`: `90` (before cert-manager at 100)
- `CnpgProvider`: `500` (after cert-manager)

## Dependencies

None (stdlib only). Optional: `dekube-converter-cert-manager` for TLS.

## Known limitations

**No PgBouncer.** Pooler CRDs are converted to DNS aliases, not PgBouncer instances. Apps connect directly to PostgreSQL. If you need connection pooling in compose... actions, consequences.

**Namespace collisions.** The dekube engine indexes manifests in a flat namespace (no per-K8s-namespace separation). Two CNPG Clusters with the same `metadata.name` in different namespaces will collide -- last-parsed wins. Don't do that. If you have `my-cluster` in both `ns-a` and `ns-b`, rename one of them.

**Rootless Docker.** The TLS command uses `install -o postgres -g postgres -m 600` which requires the container to start as root (default for the standard `postgres` image). Rootless Docker environments where the entrypoint doesn't run as root may need adjustment.

**No replication.** Compose is single-instance. The `-rw`, `-r`, and `-ro` services all resolve to the same container. If you're running `instances: 3` in K8s and expect HA in compose, see above re: actions and consequences.

## Upgrading from ≤ v0.1.x

Before this version, PGDATA lived in the container's anonymous volume, not under `./data/<cluster>-1`. Upgrading in place will make postgres initdb a fresh, empty PGDATA at the new path — your old data is still in the anonymous volume, but the new compose file no longer mounts it. Copying the old data dir across does not work either: the new bootstrap step won't run against an already-initialized PGDATA, and the superuser changed (pre-fix, the container's superuser was the app user, e.g. `app`; this version bootstraps a real `postgres` superuser and creates the app owner role separately).

Dump before regenerating, restore after:

```bash
# 1. With the OLD compose stack still running, dump everything (roles + data)
#    as the old superuser (the bootstrap secret's username, "app" by default):
docker compose exec <cluster> pg_dumpall -U app > dump.sql

# 2. Regenerate with the new version and bring the stack up (fresh PGDATA,
#    new "postgres" superuser + app owner role get bootstrapped).
python3 helmfile2compose.py ... && docker compose up -d

# 3. Restore into the new cluster as the postgres superuser:
cat dump.sql | docker compose exec -T <cluster> psql -U postgres -d postgres
```

Step 3 may print "role already exists" for the app role — harmless, the new bootstrap already created it; the data restore still applies.

## Upgrading from v0.2.0

v0.2.0 mounted the TLS key at `/var/lib/postgresql/server.key`, which on PostgreSQL 18+ is inside the PGDATA PVC; this version moves TLS files to `/etc/postgresql/tls/` instead — a stale `server.key` (and `server.crt`/`ca.crt`) left behind under `./data/<cluster>-1/` by v0.2.0 is no longer read and should be deleted. `server.key` there may actually be the current private key, since cert-manager ≥ v0.5.0 reuses existing keys rather than regenerating them — leaving it in place is misleading, not harmless.

Deleting needs `sudo`: `server.crt` and `ca.crt` are empty mountpoint files Docker created as root when the old bind mounts were set up, and `server.key` is owned by uid 999 (the postgres user in the container), mode 0600.

## Upgrading to this version: enableSuperuserAccess now defaults to false

Older versions always published a `<cluster>-superuser` Secret. This provider now matches CNPG's own default (`spec.enableSuperuserAccess` is disabled unless set): a superuser Secret is only generated when your Cluster manifest sets `enableSuperuserAccess: true`. If any app in your `dekube.yaml` references `<cluster>-superuser`, add `enableSuperuserAccess: true` to the Cluster spec before regenerating — exactly what you'd need to do against a real CNPG cluster.

## Usage

Via dekube-manager:

```bash
python3 dekube-manager.py cnpg
```

Manual:

```bash
python3 helmfile2compose.py --extensions-dir ./dekube-provider-cnpg --helmfile-dir ~/my-platform -e local --output-dir .
```

## Code quality

| Metric | Value |
|--------|-------|
| Pylint | 9.84/10 |
| Pyflakes | clean |
| Radon MI | 43.99 (A) |
| Radon avg CC | 4.125 (A) |

Worst CC: `_index_cluster` (11, C) — 8 YAML field extractions, not logic complexity.

## License

Public domain.
