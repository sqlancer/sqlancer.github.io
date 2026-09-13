"""Builds the DBMS registry from the SQLancer repository.

Which systems SQLancer supports is answered by the repository itself: each
supported system has a provider package under ``src/sqlancer``. Reading the git
tree makes the answer deterministic and self-updating, and the tree entry is
recorded as the evidence.

Display names and homepages cannot be derived from a directory name, so they
come from a curated table below. A provider with no table entry still lands in
the registry with a title-cased name, so a newly added DBMS is never silently
dropped -- it just gets a follow-up nudge in the pull request.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .. import config
from ..github import GitHub
from ..util import content_hash, now, slugify

COLLECTOR = "dbms_registry"
COLLECTOR_VERSION = "1.0.0"

SQLANCER_OWNER = "sqlancer"
SQLANCER_REPO = "sqlancer"
PROVIDER_ROOT = "src/sqlancer"

# Directories under src/sqlancer that are shared infrastructure, not a DBMS.
NON_PROVIDER_DIRS = {"common", "transformations"}

# Curated presentation metadata, keyed by provider directory name.
KNOWN: Dict[str, Dict[str, Optional[str]]] = {
    "citus": {"name": "Citus", "url": "https://www.citusdata.com/",
              "repository": "https://github.com/citusdata/citus"},
    "clickhouse": {"name": "ClickHouse", "url": "https://clickhouse.com/",
                   "repository": "https://github.com/ClickHouse/ClickHouse"},
    "cockroachdb": {"name": "CockroachDB", "url": "https://www.cockroachlabs.com/",
                    "repository": "https://github.com/cockroachdb/cockroach"},
    "databend": {"name": "Databend", "url": "https://www.databend.com/",
                 "repository": "https://github.com/datafuselabs/databend",
                 "github_owners": ["datafuse-extras", "databendlabs",
                                   "datafuselabs"]},
    "datafusion": {"name": "Apache DataFusion",
                   "url": "https://datafusion.apache.org/",
                   "repository": "https://github.com/apache/datafusion"},
    "doris": {"name": "Apache Doris", "url": "https://doris.apache.org/",
              "repository": "https://github.com/apache/doris"},
    # DuckDB's CI robot files what its fuzzers find in a repository of its
    # own, so that a failure does not break the build -- the keynote at DBTest
    # '22 describes it. Those reports are DuckDB bugs and belong to DuckDB.
    "duckdb": {"name": "DuckDB", "url": "https://duckdb.org/",
               "repository": "https://github.com/duckdb/duckdb",
               # DuckDB was cwida/duckdb until 2021, and those links still
               # redirect -- so the same bug arrives under two addresses.
               "github_owners": ["cwida"],
               "github_repositories": ["duckdb/duckdb-fuzzer",
                                       "duckdblabs/duckdb-fuzzer-ci"]},
    "h2": {"name": "H2", "url": "https://h2database.com/",
           "repository": "https://github.com/h2database/h2database"},
    "hive": {"name": "Apache Hive", "url": "https://hive.apache.org/",
             "repository": "https://github.com/apache/hive"},
    "hsqldb": {"name": "HSQLDB", "url": "https://hsqldb.org/", "repository": None},
    "mariadb": {"name": "MariaDB", "url": "https://mariadb.org/",
                "repository": "https://github.com/MariaDB/server"},
    "materialize": {"name": "Materialize", "url": "https://materialize.com/",
                    "repository": "https://github.com/MaterializeInc/materialize"},
    "mysql": {"name": "MySQL", "url": "https://www.mysql.com/",
              "repository": "https://github.com/mysql/mysql-server"},
    "oceanbase": {"name": "OceanBase", "url": "https://www.oceanbase.com/",
                  "repository": "https://github.com/oceanbase/oceanbase"},
    "postgres": {"name": "PostgreSQL", "url": "https://www.postgresql.org/",
                 "repository": "https://github.com/postgres/postgres",
                 "id": "postgresql"},
    "presto": {"name": "Presto", "url": "https://prestodb.io/",
               "repository": "https://github.com/prestodb/presto"},
    "questdb": {"name": "QuestDB", "url": "https://questdb.io/",
                "repository": "https://github.com/questdb/questdb"},
    "spark": {"name": "Apache Spark", "url": "https://spark.apache.org/",
              "repository": "https://github.com/apache/spark"},
    "sqlite3": {"name": "SQLite", "url": "https://sqlite.org/",
                "repository": "https://github.com/sqlite/sqlite", "id": "sqlite"},
    "tidb": {"name": "TiDB", "url": "https://www.pingcap.com/tidb/",
             "repository": "https://github.com/pingcap/tidb"},
    "yugabyte": {"name": "YugabyteDB", "url": "https://www.yugabyte.com/",
                 "repository": "https://github.com/yugabyte/yugabyte-db",
                 "id": "yugabytedb"},
}

# Systems that appear in bug or adoption records without a provider package in
# the main repository -- historic providers, and systems reached through
# SQLancer++ rather than a bespoke implementation.
ADDITIONAL: List[Dict[str, Optional[str]]] = [
    {"id": "tdengine", "name": "TDengine", "url": "https://tdengine.com/",
     "repository": "https://github.com/taosdata/TDengine",
     "aliases": ["TDEngine", "tdengine", "TDengine"]},
    {"id": "monetdb", "name": "MonetDB", "url": "https://www.monetdb.org/",
     "repository": "https://github.com/MonetDB/MonetDB", "aliases": ["MonetDB"]},
    {"id": "umbra", "name": "Umbra", "url": "https://umbra-db.com/",
     "repository": None, "aliases": ["Umbra"]},
    {"id": "dolt", "name": "Dolt", "url": "https://www.dolthub.com/",
     "repository": "https://github.com/dolthub/dolt", "aliases": ["Dolt"]},
    {"id": "cratedb", "name": "CrateDB", "url": "https://cratedb.com/",
     "repository": "https://github.com/crate/crate",
     "aliases": ["cratedb", "Crate", "CrateDB"]},
    {"id": "risingwave", "name": "RisingWave", "url": "https://risingwave.com/",
     "repository": "https://github.com/risingwavelabs/risingwave",
     "aliases": ["risingwave", "RisingWave"]},
    {"id": "neo4j", "name": "Neo4j", "url": "https://neo4j.com/",
     "repository": "https://github.com/neo4j/neo4j", "aliases": ["neo4j", "Neo4j"]},
    {"id": "firebird", "name": "Firebird", "url": "https://firebirdsql.org/",
     "repository": "https://github.com/FirebirdSQL/firebird",
     "aliases": ["Firebird"]},
    {"id": "virtuoso", "name": "Virtuoso", "url": "https://virtuoso.openlinksw.com/",
     "repository": "https://github.com/openlink/virtuoso-opensource",
     "aliases": ["Virtuoso"]},
    {"id": "redisgraph", "name": "RedisGraph", "url": "https://redis.io/",
     "repository": "https://github.com/RedisGraph/RedisGraph",
     "aliases": ["redisgraph", "RedisGraph"]},
    {"id": "agensgraph", "name": "AgensGraph", "url": "https://bitnine.net/",
     "repository": "https://github.com/bitnine-oss/agensgraph",
     "aliases": ["agensgraph", "AgensGraph"]},
    {"id": "percona", "name": "Percona Server", "url": "https://www.percona.com/",
     "repository": "https://github.com/percona/percona-server",
     "aliases": ["Percona"]},
    {"id": "cubrid", "name": "CUBRID", "url": "https://www.cubrid.org/",
     "repository": "https://github.com/CUBRID/cubrid", "aliases": ["CUBRID"]},
    {"id": "arangodb", "name": "ArangoDB", "url": "https://arangodb.com/",
     "repository": "https://github.com/arangodb/arangodb", "aliases": ["ArangoDB"]},
    {"id": "mongodb", "name": "MongoDB", "url": "https://www.mongodb.com/",
     "repository": "https://github.com/mongodb/mongo", "aliases": ["MongoDB"]},
    {"id": "cnosdb", "name": "CnosDB", "url": "https://www.cnosdb.com/",
     "repository": "https://github.com/cnosdb/cnosdb", "aliases": ["CnosDB"]},
    # Systems whose own team maintains a SQLancer fork, reached through the
    # fork list rather than through a provider package.
    # Systems reached only through their issue trackers: SQLancer has no
    # provider for them, but their bugs name it.
    {"id": "wadjet", "name": "Wadjet", "url": "https://github.com/derekmwright/wadjet",
     "repository": "https://github.com/derekmwright/wadjet", "aliases": ["Wadjet"]},
    {"id": "elasticsearch", "name": "Elasticsearch", "url": "https://www.elastic.co/",
     "repository": "https://github.com/elastic/elasticsearch",
     "aliases": ["Elasticsearch"]},
    {"id": "opensearch", "name": "OpenSearch SQL", "url": "https://opensearch.org/",
     "repository": "https://github.com/opensearch-project/sql",
     "aliases": ["OpenSearch", "OpenSearch SQL"]},
    {"id": "tikv", "name": "TiKV", "url": "https://tikv.org/",
     "repository": "https://github.com/tikv/tikv", "aliases": ["TiKV"]},
    {"id": "sparq", "name": "sparq", "url": "https://github.com/sparq-org/sparq",
     "repository": "https://github.com/sparq-org/sparq", "aliases": ["sparq"]},
    {"id": "serenedb", "name": "SereneDB", "url": "https://github.com/serenedb/serenedb",
     "repository": "https://github.com/serenedb/serenedb", "aliases": ["SereneDB"]},
    {"id": "kyzo", "name": "Kyzo", "url": "https://github.com/kyzobuild/kyzo",
     "repository": "https://github.com/kyzobuild/kyzo", "aliases": ["Kyzo"]},
    {"id": "greptimedb", "name": "GreptimeDB", "url": "https://greptime.com/",
     "repository": "https://github.com/GreptimeTeam/greptimedb",
     "aliases": ["GreptimeDB"]},
    {"id": "defradb", "name": "DefraDB", "url": "https://docs.source.network/",
     "repository": "https://github.com/sourcenetwork/defradb.rs",
     "github_owners": ["sourcenetwork"], "aliases": ["DefraDB"]},
    {"id": "bharatdbms", "name": "BharatDBMS",
     "url": "https://github.com/BharatDBPG/BharatDBMS-PG",
     "repository": "https://github.com/BharatDBPG/BharatDBMS-PG",
     "aliases": ["BharatDBMS"]},
    {"id": "stonedb", "name": "StoneDB", "url": "https://stonedb.io/",
     "repository": "https://github.com/stoneatom/stonedb", "aliases": ["StoneDB"]},
    {"id": "turso", "name": "Turso", "url": "https://turso.tech/",
     "repository": "https://github.com/tursodatabase/turso",
     "github_owners": ["tursodatabase"], "aliases": ["Turso", "limbo"]},
    {"id": "seekdb", "name": "SeekDB", "url": "https://www.oceanbase.com/",
     "repository": "https://github.com/oceanbase/seekdb", "aliases": ["SeekDB"]},
    {"id": "falkordb", "name": "FalkorDB", "url": "https://www.falkordb.com/",
     "repository": "https://github.com/FalkorDB/FalkorDB", "aliases": ["FalkorDB"]},
    {"id": "readyset", "name": "ReadySet", "url": "https://readyset.io/",
     "repository": "https://github.com/readysettech/readyset",
     "aliases": ["ReadySet", "readyset"]},
    {"id": "noisepage", "name": "NoisePage", "url": "https://noise.page/",
     "repository": "https://github.com/cmu-db/noisepage", "aliases": ["NoisePage"]},
    {"id": "tarantool", "name": "Tarantool", "url": "https://www.tarantool.io/",
     "repository": "https://github.com/tarantool/tarantool",
     "aliases": ["Tarantool"]},
    {"id": "spiceai", "name": "Spice.ai OSS", "url": "https://spice.ai/",
     "repository": "https://github.com/spiceai/spiceai", "aliases": ["Spice.ai", "spiceai"]},
    {"id": "cloudberry", "name": "Apache Cloudberry",
     "url": "https://cloudberry.apache.org/",
     "repository": "https://github.com/apache/cloudberry",
     "aliases": ["Cloudberry", "Apache Cloudberry", "cloudberrydb"]},
    {"id": "greenplum", "name": "Greenplum", "url": "https://greenplum.org/",
     "repository": "https://github.com/greenplum-db/gpdb",
     "aliases": ["Greenplum", "gpdb"]},
    {"id": "starrocks", "name": "StarRocks", "url": "https://www.starrocks.io/",
     "repository": "https://github.com/StarRocks/starrocks",
     "aliases": ["StarRocks"]},
    {"id": "opengauss", "name": "openGauss", "url": "https://opengauss.org/",
     "repository": "https://github.com/opengauss-mirror/openGauss-server",
     "aliases": ["openGauss", "opengauss"],
     # openGauss develops on Gitee; GitHub carries a mirror. The bug search
     # needs the Gitee repository, and this is the only place that survives a
     # regeneration of the registry.
     "gitee_repositories": ["opengauss/openGauss-server"]},
    {"id": "ydb", "name": "YDB", "url": "https://ydb.tech/",
     "repository": "https://github.com/ydb-platform/ydb", "aliases": ["YDB"]},
    {"id": "hazelcast", "name": "Hazelcast", "url": "https://hazelcast.com/",
     "repository": "https://github.com/hazelcast/hazelcast",
     "aliases": ["Hazelcast"]},
    {"id": "feldera", "name": "Feldera", "url": "https://feldera.com/",
     "repository": "https://github.com/feldera/feldera", "aliases": ["Feldera"]},
    {"id": "xugu", "name": "XuGu", "url": "https://www.xugudb.com/",
     "repository": "https://github.com/Xugu-Open-Source/xugu",
     "aliases": ["XuGu", "Xugu"]},
]


def _aliases(directory: str, name: str, record_id: str) -> List[str]:
    seen, out = set(), []
    for candidate in (name, directory, record_id, name.replace(" ", "")):
        lowered = candidate.lower()
        if lowered not in seen:
            seen.add(lowered)
            out.append(candidate)
    return out


def collect(gh: GitHub, timestamp: Optional[str] = None) -> List[dict]:
    """Return DBMS registry records derived from the SQLancer repository."""
    timestamp = timestamp or now()
    tree = gh.tree(SQLANCER_OWNER, SQLANCER_REPO, "main")
    tree_url = (f"https://github.com/{SQLANCER_OWNER}/{SQLANCER_REPO}/tree/"
                f"main/{PROVIDER_ROOT}")

    providers = sorted({
        entry["path"].split("/")[2]
        for entry in tree
        if entry.get("type") == "tree"
        and entry["path"].startswith(PROVIDER_ROOT + "/")
        and entry["path"].count("/") == 2
        and entry["path"].split("/")[2] not in NON_PROVIDER_DIRS
    })

    records: List[dict] = []
    for directory in providers:
        meta = KNOWN.get(directory, {})
        name = meta.get("name") or directory.replace("_", " ").title()
        record_id = meta.get("id") or slugify(directory)
        provider_path = f"{PROVIDER_ROOT}/{directory}"
        records.append({
            "id": record_id,
            "name": name,
            "aliases": _aliases(directory, name, record_id),
            "url": meta.get("url"),
            "repository": meta.get("repository"),
            "github_owners": meta.get("github_owners") or [],
            "github_repositories": meta.get("github_repositories") or [],
            "gitee_repositories": meta.get("gitee_repositories") or [],
            "supported_by_sqlancer": True,
            "support": {
                "provider_path": provider_path,
                "umbrella_tool": "sqlancer",
                "evidence": [{
                    "source_url": (f"https://github.com/{SQLANCER_OWNER}/"
                                   f"{SQLANCER_REPO}/tree/main/{provider_path}"),
                    "source_type": "github_repository",
                    "excerpt": None,
                    "note": (f"The SQLancer repository contains a testing "
                             f"implementation at {provider_path}."),
                    "content_sha256": content_hash(provider_path),
                    "retrieved_at": timestamp,
                    "first_seen": timestamp,
                    "last_verified": timestamp,
                }],
            },
        })

    known_ids = {record["id"] for record in records}
    for extra in ADDITIONAL:
        if extra["id"] in known_ids:
            continue
        records.append({
            "id": extra["id"],
            "name": extra["name"],
            "aliases": extra.get("aliases") or [extra["name"]],
            "url": extra.get("url"),
            "repository": extra.get("repository"),
            "github_owners": extra.get("github_owners") or [],
            "github_repositories": extra.get("github_repositories") or [],
            "gitee_repositories": extra.get("gitee_repositories") or [],
            "supported_by_sqlancer": False,
            "support": None,
            "notes": ("Referenced by impact records without a provider package "
                      "in the current main SQLancer repository."),
        })

    records.sort(key=lambda r: r["id"])
    return records


def drop_nulls(record: dict) -> dict:
    """Remove optional keys whose value is None, which the schemas disallow."""
    cleaned = {}
    for key, value in record.items():
        if value is None and key in ("url", "repository", "notes"):
            continue
        if key in ("github_owners", "github_repositories",
                   "gitee_repositories") and not value:
            continue
        cleaned[key] = value
    return cleaned


def build_payload(records: List[dict]) -> dict:
    return {
        "schema_version": "1.0.0",
        "dbms": [drop_nulls(record) for record in records],
    }


def main() -> int:
    from ..cache import Caches
    caches = Caches()
    gh = GitHub(caches.github)
    records = collect(gh)
    changed = config.write_json(config.DATA_FILES["dbms"], build_payload(records))
    supported = sum(1 for r in records if r["supported_by_sqlancer"])
    print(f"dbms.json: {'updated' if changed else 'unchanged'} "
          f"({len(records)} systems, {supported} supported by SQLancer)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
