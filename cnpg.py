"""dekube provider: cnpg — Cluster, Pooler.

Converts CloudNativePG Cluster CRDs into PostgreSQL compose services.
Pooler CRDs become DNS aliases (no PgBouncer container).

TLS is delegated to cert-manager: synthetic Certificate/Issuer manifests
are injected into ctx.manifests for cert-manager to process. Without
cert-manager, TLS degrades gracefully (PG runs without SSL).
"""

import os
import secrets
import string
import sys

from dekube import (  # pylint: disable=import-error  # h2c resolves at runtime
    ConverterResult, ProviderResult,
    IndexerConverter, Provider, secret_value,
)

# Module-level state shared between CnpgIndexer and CnpgProvider
_clusters = {}


def _generate_password(length=64):
    """Generate a random password (alphanumeric)."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


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
            self._index_cluster(m, ctx)
        return ConverterResult()

    def _index_cluster(self, m, ctx):
        meta = m.get("metadata") or {}
        name = meta.get("name", "")
        ns = meta.get("namespace", "")
        spec = m.get("spec") or {}

        bootstrap = (spec.get("bootstrap") or {}).get("initdb") or {}

        _clusters[name] = {
            "name": name,
            "namespace": ns,
            "image_name": spec.get("imageName", ""),
            "bootstrap_secret": (bootstrap.get("secret") or {}).get("name", ""),
            "post_init_sql": bootstrap.get("postInitSQL") or [],
            "pg_parameters": (spec.get("postgresql") or {}).get("parameters") or {},
            "server_alt_dns_names": (spec.get("certificates") or {}).get("serverAltDNSNames") or [],
            "enable_superuser": spec.get("enableSuperuserAccess", True),
        }

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
