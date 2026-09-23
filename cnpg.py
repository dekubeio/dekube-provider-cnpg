"""dekube provider: cnpg — Cluster, Pooler.

Converts CloudNativePG Cluster CRDs into PostgreSQL compose services.
Pooler CRDs become DNS aliases (no PgBouncer container).

TLS is delegated to cert-manager: synthetic Certificate/Issuer manifests
are injected into ctx.manifests for cert-manager to process. Without
cert-manager, TLS degrades gracefully (PG runs without SSL).
"""

import os
import re
import sys

from dekube import (  # pylint: disable=import-error  # h2c resolves at runtime
    ConverterResult, ProviderResult,
    IndexerConverter, Provider, secret_value, generate_password,
    convert_volume_mounts,
)

# Module-level state shared between CnpgIndexer and CnpgProvider
_clusters = {}


class CnpgIndexer(IndexerConverter):
    """Index CNPG Cluster manifests and emit synthetic cert-manager CRDs.

    Runs before cert-manager (priority 90 < 100) so that emitted
    Certificate/Issuer manifests are picked up by cert-manager's pass.
    """

    name = "cnpg"
    kinds = ["Cluster"]
    priority = 90

    def convert(self, kind, manifests, ctx):
        _clusters.clear()  # avoid stale data across repeated runs
        for m in manifests:
            if not m:
                continue
            self._index_cluster(m, ctx)
        return ConverterResult()

    def _index_cluster(self, m, ctx):
        meta = m.get("metadata") or {}
        name = os.path.basename(meta.get("name", ""))
        ns = os.path.basename(meta.get("namespace", ""))
        spec = m.get("spec") or {}

        bootstrap = (spec.get("bootstrap") or {}).get("initdb") or {}
        bootstrap_secret = os.path.basename(
            (bootstrap.get("secret") or {}).get("name", ""))

        _clusters[name] = {
            "name": name,
            "namespace": ns,
            "image_name": spec.get("imageName", ""),
            "bootstrap_secret": bootstrap_secret,
            "database": bootstrap.get("database") or "app",
            # CNPG: owner defaults to the database name
            "owner": bootstrap.get("owner") or bootstrap.get("database") or "app",
            "post_init_sql": bootstrap.get("postInitSQL") or [],
            "pg_parameters": (spec.get("postgresql") or {}).get("parameters") or {},
            "server_alt_dns_names": (spec.get("certificates") or {}).get("serverAltDNSNames") or [],
            "enable_superuser": spec.get("enableSuperuserAccess", True),
        }

        # PGDATA persists like any PVC (CNPG names the first instance's PVC <cluster>-1).
        # Registered here (indexer, priority 90) so config has it before the provider mounts it.
        # Also on non-first runs: compose needs the key (a bare name is an undefined named volume),
        # and the engine only saves dekube.yaml on first run — so warn instead.
        pvc = f"{name}-1"
        ctx.pvc_names.add(pvc)
        volumes = ctx.config.get("volumes") or {}
        if pvc not in volumes:
            volumes[pvc] = {"host_path": pvc}
            ctx.config["volumes"] = volumes
            if not ctx.first_run:
                ctx.warnings.append(
                    f"PVC '{pvc}' (CNPG PGDATA) not in dekube.yaml — using host_path {pvc}; "
                    f"add it under volumes: to make it explicit. If you're upgrading from a "
                    f"version that didn't persist PGDATA, that old data is NOT under "
                    f"./data/{pvc} (it lived in the container's anonymous volume) — dump it "
                    f"with pg_dumpall from the OLD container before running `up`, then restore "
                    f"after. Copying the old data dir over does not work: the new bootstrap "
                    f"won't run against an existing PGDATA, and the superuser differs.")

        self._emit_certs(name, _clusters[name], ctx)
        print(f"  cnpg: indexed cluster '{name}' (namespace: {ns})",
              file=sys.stderr)

    @staticmethod
    def _emit_certs(name, cluster_info, ctx):
        """Inject synthetic Certificate/Issuer manifests for cert-manager."""
        dns_names = [f"{name}-rw", f"{name}-r", f"{name}-ro"]
        dns_names.extend(cluster_info["server_alt_dns_names"])

        # 1. Self-signed issuer
        ctx.manifests.setdefault("Issuer", []).append({
            "apiVersion": "cert-manager.io/v1",
            "kind": "Issuer",
            "metadata": {"name": f"{name}-self-signed"},
            "spec": {"selfSigned": {}},
        })

        # 2. CA certificate
        ctx.manifests.setdefault("Certificate", []).append({
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": f"{name}-ca"},
            "spec": {
                "isCA": True,
                "commonName": f"{name}-ca",
                "secretName": f"{name}-ca",
                "issuerRef": {"name": f"{name}-self-signed", "kind": "Issuer"},
            },
        })

        # 3. CA issuer (references the CA secret)
        ctx.manifests.setdefault("Issuer", []).append({
            "apiVersion": "cert-manager.io/v1",
            "kind": "Issuer",
            "metadata": {"name": f"{name}-ca-issuer"},
            "spec": {"ca": {"secretName": f"{name}-ca"}},
        })

        # 4. Server certificate
        ctx.manifests.setdefault("Certificate", []).append({
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": f"{name}-server"},
            "spec": {
                "secretName": f"{name}-server-tls",
                "commonName": f"{name}-rw",
                "issuerRef": {"name": f"{name}-ca-issuer", "kind": "Issuer"},
                "dnsNames": dns_names,
            },
        })


class CnpgProvider(Provider):
    """Generate PostgreSQL compose services from indexed CNPG Clusters.

    Claims Pooler to register pooler names as aliases, then iterates
    _clusters to generate one PG container per cluster.
    """

    name = "cnpg"
    kinds = ["Pooler"]
    priority = 500

    def convert(self, kind, manifests, ctx):
        # Phase 1: index pooler aliases
        for m in manifests:
            if not m:
                continue
            self._index_pooler(m, ctx)

        # Phase 2: generate PG services from indexed clusters
        services = {}
        for cluster_name, info in _clusters.items():
            svc = self._build_pg_service(info, ctx)
            if svc:
                services[cluster_name] = svc
                self._register_services(cluster_name, info, ctx)

        return ProviderResult(services=services)

    # -- Pooler alias indexing -----------------------------------------------

    @staticmethod
    def _index_pooler(m, ctx):
        """Register pooler name as DNS alias to the cluster compose service."""
        meta = m.get("metadata") or {}
        pooler_name = meta.get("name", "")
        spec = m.get("spec") or {}
        cluster_ref = (spec.get("cluster") or {}).get("name", "")

        if pooler_name and cluster_ref:
            ctx.alias_map[pooler_name] = cluster_ref
            print(f"  cnpg: pooler '{pooler_name}' → alias for "
                  f"'{cluster_ref}'", file=sys.stderr)

    # -- PG service generation -----------------------------------------------

    def _build_pg_service(self, info, ctx):
        """Build a compose service dict for a PostgreSQL cluster."""
        name = info["name"]
        image = self._map_image(info["image_name"])

        # Credentials: the image's superuser is CNPG's postgres superuser;
        # the application owner role + database come from 00-bootstrap.sh.
        owner, owner_pw = self._resolve_credentials(info, ctx)
        su_password = self._superuser_password(info, ctx)
        # CBA: CNPG defaults enableSuperuserAccess to false — kept true here, P1
        if info["enable_superuser"]:
            self._generate_superuser_secret(info, ctx, su_password)
        env = {
            "POSTGRES_USER": "postgres",
            "POSTGRES_PASSWORD": su_password,
            "POSTGRES_DB": "postgres",
            # consumed by 00-bootstrap.sh (CNPG initdb: owner role + application database)
            "CNPG_OWNER": owner,
            "CNPG_OWNER_PASSWORD": owner_pw,
            "CNPG_DATABASE": info["database"],
        }

        # Check TLS availability before writing config
        has_tls = (f"{name}-server-tls" in ctx.secrets
                   and f"{name}-ca" in ctx.secrets)

        # Volumes + command
        volumes = []
        cmd = ["postgres",
               "-c", "config_file=/etc/postgresql/postgresql.conf"]

        # postgresql.conf
        pg_conf_path = self._write_pg_conf(name, info, has_tls, ctx)
        volumes.append(f"./{pg_conf_path}:/etc/postgresql/postgresql.conf:ro")

        # initdb: owner role + application database (always), postInitSQL (if any)
        volumes.extend(self._write_initdb(name, info, ctx))

        # PGDATA (PVC <cluster>-1, registered by the indexer)
        volumes.extend(convert_volume_mounts(
            [{"name": "pgdata", "mountPath": self._pgdata_mount(image)}],
            [{"name": "pgdata", "persistentVolumeClaim": {"claimName": f"{name}-1"}}],
            ctx.pvc_names, ctx.config, f"Cluster/{name}", ctx.warnings))

        # TLS mounts + command wrapper
        if has_tls:
            self._mount_tls(name, volumes, ctx)
            # PG refuses world-readable key files; the cert file on disk
            # inherits host umask (typically 0644). Copy + fix permissions
            # before starting PG. docker-entrypoint.sh handles initdb +
            # gosu to postgres user.
            cmd = [
                "bash", "-c",
                "install -o postgres -g postgres -m 600 "
                "/tmp/server.key /var/lib/postgresql/server.key && "
                "exec docker-entrypoint.sh postgres "
                "-c config_file=/etc/postgresql/postgresql.conf "
                "-c hba_file=/etc/postgresql/pg_hba.conf",
            ]

        service = {
            "image": image,
            "restart": "always",
            "environment": env,
            "volumes": volumes,
            "command": cmd,
        }

        print(f"  cnpg: generated service '{name}' "
              f"(image: {image}, tls: {has_tls})", file=sys.stderr)
        return service

    # -- Image mapping -------------------------------------------------------

    @staticmethod
    def _map_image(cnpg_image):
        """Map CNPG image to standard postgres Docker Hub image.

        ghcr.io/cloudnative-pg/postgresql:18.3 → postgres:18.3
        """
        if ":" in cnpg_image:
            tag = cnpg_image.rsplit(":", 1)[1]
            return f"postgres:{tag}"
        return "postgres:latest"

    @staticmethod
    def _pgdata_mount(image):
        """Mount point for PGDATA: the postgres image's declared VOLUME.

        postgres >=18 (and latest): VOLUME /var/lib/postgresql, PGDATA=/var/lib/postgresql/<major>/docker.
        postgres <=17: VOLUME /var/lib/postgresql/data (= PGDATA). Mounting the parent there would
        leave the image's anonymous volume shadowing PGDATA, and nothing would reach the host path.
        """
        major = re.match(r"\d+", image.rsplit(":", 1)[-1])
        if major and int(major.group()) < 18:
            return "/var/lib/postgresql/data"
        return "/var/lib/postgresql"

    # -- Credentials ---------------------------------------------------------

    @staticmethod
    def _resolve_credentials(info, ctx):
        """Resolve the owner credentials from the bootstrap secret, or auto-generate them.

        Returns (username, password). With a user secret, the username comes from it
        (CNPG requires owner == secret username).
        """
        secret_name = info["bootstrap_secret"]
        cluster_name = info["name"]
        auto_name = secret_name or f"{cluster_name}-app"

        if secret_name and secret_name in ctx.secrets:
            sec = ctx.secrets[secret_name]
            username = secret_value(sec, "username") or "app"
            password = secret_value(sec, "password") or ""
            return username, password

        # Auto-generate (idempotent: reuse if already on disk)
        secret_dir = os.path.join(ctx.output_dir, "secrets", auto_name)
        pw_file = os.path.join(secret_dir, "password")
        user_file = os.path.join(secret_dir, "username")

        if os.path.isfile(pw_file) and os.path.isfile(user_file):
            with open(user_file, encoding="utf-8") as f:
                username = f.read().strip()
            with open(pw_file, encoding="utf-8") as f:
                password = f.read().strip()
            # Pre-fix dirs (no dbname) had a hardcoded "app" user: adopt the CNPG owner, keep the password
            if not os.path.isfile(os.path.join(secret_dir, "dbname")):
                username = info["owner"]
            print(f"  cnpg: reusing credentials from "
                  f"secrets/{auto_name}/", file=sys.stderr)
        else:
            username = info["owner"]
            password = generate_password(64)
            print(f"  cnpg: generated credentials → "
                  f"secrets/{auto_name}/", file=sys.stderr)

        # CNPG-shaped app secret
        host = f"{cluster_name}-rw"
        string_data = {
            "username": username, "user": username, "password": password,
            "dbname": info["database"], "host": host, "port": "5432",
            "uri": f"postgresql://{username}:{password}@{host}:5432/{info['database']}",
        }
        os.makedirs(secret_dir, exist_ok=True)
        out_real = os.path.realpath(ctx.output_dir) + os.sep
        for key, val in string_data.items():
            out_path = os.path.join(secret_dir, key)
            if not os.path.realpath(out_path).startswith(out_real):
                continue
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(val)

        ctx.secrets[auto_name] = {
            "metadata": {"name": auto_name},
            "stringData": string_data,
        }
        ctx.generated_secrets.add(auto_name)
        return username, password

    # -- Superuser secret ----------------------------------------------------

    @staticmethod
    def _superuser_password(info, ctx):
        """Read or generate the postgres superuser password (idempotent, on disk)."""
        secret_name = f"{info['name']}-superuser"
        secret_dir = os.path.join(ctx.output_dir, "secrets", secret_name)
        pw_file = os.path.join(secret_dir, "password")

        if os.path.isfile(pw_file):
            with open(pw_file, encoding="utf-8") as f:
                su_password = f.read().strip()
            print(f"  cnpg: reusing superuser credentials from "
                  f"secrets/{secret_name}/", file=sys.stderr)
        else:
            su_password = generate_password(64)
            os.makedirs(secret_dir, exist_ok=True)
            with open(pw_file, "w", encoding="utf-8") as f:
                f.write(su_password)
            print(f"  cnpg: generated superuser credentials → "
                  f"secrets/{secret_name}/", file=sys.stderr)
        return su_password

    @staticmethod
    def _generate_superuser_secret(info, ctx, su_password):
        """Generate superuser secret emulating CNPG operator."""
        name = info["name"]
        ns = info["namespace"]
        secret_name = f"{name}-superuser"
        secret_dir = os.path.join(ctx.output_dir, "secrets", secret_name)

        host = f"{name}-rw"
        short_fqdn = f"{host}.{ns}" if ns else host
        fqdn = f"{host}.{ns}.svc.cluster.local" if ns else host

        string_data = {
            "username": "postgres",
            "user": "postgres",
            "password": su_password,
            "dbname": "*",
            "host": host,
            "port": "5432",
            "uri": f"postgresql://postgres:{su_password}@{short_fqdn}:5432/*",
            "fqdn-uri": f"postgresql://postgres:{su_password}@{fqdn}:5432/*",
            "jdbc-uri": (f"jdbc:postgresql://{short_fqdn}:5432/*?"
                         f"password={su_password}&user=postgres"),
            "pgpass": f"{host}:5432:*:postgres:{su_password}",
        }

        # Write all fields to disk
        out_real = os.path.realpath(ctx.output_dir) + os.sep
        for key, val in string_data.items():
            out_path = os.path.join(secret_dir, key)
            if not os.path.realpath(out_path).startswith(out_real):
                continue
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(val)

        ctx.secrets[secret_name] = {
            "metadata": {"name": secret_name},
            "stringData": string_data,
        }
        ctx.generated_secrets.add(secret_name)

    # -- Config files --------------------------------------------------------

    @staticmethod
    def _write_pg_conf(name, info, has_tls, ctx):
        """Write postgresql.conf. Returns relative path."""
        cm_name = f"{name}-postgresql-conf"
        cm_dir = os.path.join(ctx.output_dir, "configmaps", cm_name)
        os.makedirs(cm_dir, exist_ok=True)

        lines = ["# Generated by dekube-provider-cnpg"]
        # config_file= bypasses the image's initdb-generated conf (which sets '*'): PG would default to localhost
        if "listen_addresses" not in info["pg_parameters"]:
            lines.append("listen_addresses = '*'")
        for param, value in info["pg_parameters"].items():
            lines.append(f"{param} = '{value}'")

        if has_tls:
            lines.append("")
            lines.append("# TLS (certs from cert-manager)")
            lines.append("ssl = on")
            lines.append(
                "ssl_cert_file = '/var/lib/postgresql/server.crt'")
            lines.append(
                "ssl_key_file = '/var/lib/postgresql/server.key'")
            lines.append(
                "ssl_ca_file = '/var/lib/postgresql/ca.crt'")

        filepath = os.path.join(cm_dir, "postgresql.conf")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        ctx.generated_cms.add(cm_name)
        return f"configmaps/{cm_name}/postgresql.conf"

    @staticmethod
    def _write_initdb(name, info, ctx):
        """Write initdb scripts. Returns compose volume strings.

        00-bootstrap.sh: CNPG initdb — owner role + application database (always).
        10-post-init.sql: postInitSQL, run as superuser in `postgres` like CNPG (if any).
        The image sources non-executable .sh files, so mode bits don't matter.
        """
        cm_name = f"{name}-initdb"
        cm_dir = os.path.join(ctx.output_dir, "configmaps", cm_name)
        os.makedirs(cm_dir, exist_ok=True)

        # psql variables do the quoting: passwords with ' or $ are safe
        with open(os.path.join(cm_dir, "00-bootstrap.sh"), "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/sh\n"
                "# Generated by dekube-provider-cnpg — CNPG initdb: "
                "application owner role + database.\n"
                'psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \\\n'
                '  -v owner="$CNPG_OWNER" -v pw="$CNPG_OWNER_PASSWORD" '
                '-v db="$CNPG_DATABASE" <<\'EOSQL\'\n'
                "CREATE ROLE :\"owner\" LOGIN PASSWORD :'pw';\n"
                'CREATE DATABASE :"db" OWNER :"owner";\n'
                "EOSQL\n")
        volumes = [f"./configmaps/{cm_name}/00-bootstrap.sh"
                   ":/docker-entrypoint-initdb.d/00-bootstrap.sh:ro"]

        if info["post_init_sql"]:
            with open(os.path.join(cm_dir, "10-post-init.sql"), "w", encoding="utf-8") as f:
                for stmt in info["post_init_sql"]:
                    if not stmt:
                        continue
                    f.write(stmt.rstrip(";") + ";\n")
            volumes.append(f"./configmaps/{cm_name}/10-post-init.sql"
                           ":/docker-entrypoint-initdb.d/10-post-init.sql:ro")

        ctx.generated_cms.add(cm_name)
        return volumes

    # -- TLS -----------------------------------------------------------------

    @staticmethod
    def _mount_tls(name, volumes, ctx):
        """Mount TLS certs and write pg_hba.conf."""
        server_secret = f"{name}-server-tls"
        ca_secret = f"{name}-ca"

        volumes.extend([
            f"./secrets/{server_secret}/tls.crt"
            f":/var/lib/postgresql/server.crt:ro",
            f"./secrets/{server_secret}/tls.key:/tmp/server.key:ro",
            f"./secrets/{ca_secret}/ca.crt:/var/lib/postgresql/ca.crt:ro",
        ])

        # pg_hba.conf: require SSL for all remote connections
        cm_name = f"{name}-pg-hba"
        cm_dir = os.path.join(ctx.output_dir, "configmaps", cm_name)
        os.makedirs(cm_dir, exist_ok=True)
        filepath = os.path.join(cm_dir, "pg_hba.conf")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("# Generated by dekube-provider-cnpg\n")
            f.write("# TYPE  DATABASE  USER  ADDRESS       METHOD\n")
            f.write("local   all       all                 trust\n")
            f.write("hostssl all       all   0.0.0.0/0     scram-sha-256\n")
            f.write("hostssl all       all   ::/0          scram-sha-256\n")

        volumes.append(
            f"./configmaps/{cm_name}/pg_hba.conf"
            f":/etc/postgresql/pg_hba.conf:ro")
        ctx.generated_cms.add(cm_name)

    # -- Service registration ------------------------------------------------

    @staticmethod
    def _register_services(cluster_name, info, ctx):
        """Register cluster services for alias/FQDN generation."""
        ns = info["namespace"]
        svc_port = [{"name": "postgresql", "port": 5432,
                     "targetPort": 5432}]

        # -rw, -r, -ro are all aliases to the same compose service
        for suffix in ("rw", "r", "ro"):
            svc_name = f"{cluster_name}-{suffix}"
            ctx.alias_map[svc_name] = cluster_name
            ctx.services_by_selector[svc_name] = {
                "name": svc_name,
                "namespace": ns,
                "selector": {"cnpg.io/cluster": cluster_name},
                "type": "ClusterIP",
                "ports": svc_port,
            }

        # Register the cluster name itself for direct references
        ctx.services_by_selector[cluster_name] = {
            "name": cluster_name,
            "namespace": ns,
            "selector": {"cnpg.io/cluster": cluster_name},
            "type": "ClusterIP",
            "ports": svc_port,
        }
