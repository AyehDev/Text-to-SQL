#!/usr/bin/env bash
# Load the AdventureWorks OLTP sample into PostgreSQL for SchemaGraph.
#
# It wraps lorint/AdventureWorks-for-Postgres, whose README does this by hand:
#   1. get Microsoft's CSV files        (downloaded here if missing)
#   2. ruby update_csvs.rb              (makes the CSVs loadable by Postgres)
#   3. create the database, psql < install.sql
# and then does the SchemaGraph-specific parts:
#   4. creates the database in lowercase UTF8 (db_id must match ^[a-z0-9_]+$;
#      the upstream README creates "Adventureworks", which would not)
#   5. runs scripts/create_roles.sql (sg_admin / sg_readonly)
#   6. ANALYZE, so the extraction's row-count estimates exist
#   7. checks the result, including that sg_readonly really cannot write
#
# Usage:
#   scripts/load_adventureworks.sh --repo PATH [options]
#
#   --repo PATH      clone of lorint/AdventureWorks-for-Postgres (required)
#   --db NAME        database to create            (default: adventureworks)
#   --zip PATH       use this AdventureWorks-oltp-install-script.zip instead of
#                    downloading it
#   --skip-convert   the CSVs were already converted with update_csvs.rb
#   --skip-roles     do not create/grant the sg_admin and sg_readonly roles
#   --force          drop and recreate the database if it already exists
#   -h, --help
#
# Connection: standard libpq variables (PGHOST, PGPORT, PGUSER, PGPASSWORD).
# The user must be a SUPERUSER: install.sql runs CREATE EXTENSION (uuid-ossp,
# tablefunc) and create_roles.sql creates roles.
#
# Unless --skip-roles, also export the two passwords (create_roles.sql reads
# them, so they never appear on a command line):
#   export PG_ADMIN_PASSWORD=...  PG_READONLY_PASSWORD=...

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPECTED_TABLES=68          # per the upstream README: 68 tables in 5 schemas
MIN_CSV_FILES=60            # the Microsoft zip has 69

REPO=""
DB="adventureworks"
ZIP=""
SKIP_CONVERT=0
SKIP_ROLES=0
FORCE=0

die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)         [[ $# -ge 2 ]] || die "--repo needs a path"; REPO="$2"; shift 2 ;;
        --db)           [[ $# -ge 2 ]] || die "--db needs a name";   DB="$2";   shift 2 ;;
        --zip)          [[ $# -ge 2 ]] || die "--zip needs a path";  ZIP="$2";  shift 2 ;;
        --skip-convert) SKIP_CONVERT=1; shift ;;
        --skip-roles)   SKIP_ROLES=1;   shift ;;
        --force)        FORCE=1;        shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "unknown option: $1 (try --help)" ;;
    esac
done

# --------------------------------------------------------------------------
# Preflight: fail early, before touching anything
# --------------------------------------------------------------------------

[[ -n "$REPO" ]] || die "--repo is required (path to your AdventureWorks-for-Postgres clone)"
[[ -d "$REPO" ]] || die "repo folder not found: $REPO"
REPO="$(cd "$REPO" && pwd)"
[[ -f "$REPO/install.sql" && -f "$REPO/update_csvs.rb" ]] \
    || die "$REPO has no install.sql / update_csvs.rb; is it the lorint/AdventureWorks-for-Postgres clone?"

[[ "$DB" =~ ^[a-z0-9_]+$ ]] \
    || die "--db must match ^[a-z0-9_]+$ (SchemaGraph's db_id rule); got '$DB'"

command -v psql >/dev/null || die "psql not found; install the PostgreSQL client"

# Everything below runs psql quietly, stopping at the first error, with notices muted.
# -w: never prompt for a password. The script makes many psql calls; prompting on
# each would be tedious and hides a missing PGPASSWORD until a call fails midway.
export PGOPTIONS="${PGOPTIONS:-} -c client_min_messages=warning"
pg() { psql -X -w -q -v ON_ERROR_STOP=1 "$@"; }

is_super="$(pg -d postgres -Atc "SELECT rolsuper FROM pg_roles WHERE rolname = current_user" 2>&1)" \
    || die "cannot connect to PostgreSQL as '${PGUSER:-$USER}' on ${PGHOST:-the local socket}:${PGPORT:-5432}.
    psql said: $is_super
    Check PGHOST/PGPORT/PGUSER, and if the server needs a password, export PGPASSWORD
    (verify first with:  psql -w -d postgres -c 'select 1')"
[[ "$is_super" == "t" ]] \
    || die "the connecting user must be a superuser (CREATE EXTENSION and CREATE ROLE need it); set PGUSER, e.g. PGUSER=postgres"

if [[ $SKIP_ROLES -eq 0 ]]; then
    : "${PG_ADMIN_PASSWORD:?set PG_ADMIN_PASSWORD (or pass --skip-roles)}"
    : "${PG_READONLY_PASSWORD:?set PG_READONLY_PASSWORD (or pass --skip-roles)}"
    psql_major="$(psql --version | grep -oE '[0-9]+' | head -1)"
    [[ "$psql_major" -ge 15 ]] \
        || die "create_roles.sql needs psql 15+ (\\getenv); yours is $psql_major. Upgrade the client or use --skip-roles"
    [[ -f "$SCRIPT_DIR/create_roles.sql" ]] || die "missing $SCRIPT_DIR/create_roles.sql"
fi

db_exists() {
    [[ "$(pg -d postgres -Atq -v db="$DB" <<<"SELECT 1 FROM pg_database WHERE datname = :'db'")" == "1" ]]
}

already_loaded=0
if db_exists; then
    if [[ $FORCE -eq 1 ]]; then
        info "--force: dropping existing database '$DB'"
    else
        has_data="$(pg -d "$DB" -Atq <<<"SELECT to_regclass('sales.salesorderheader') IS NOT NULL")"
        [[ "$has_data" == "t" ]] \
            || die "database '$DB' exists but has no AdventureWorks tables; use --force to rebuild it"
        already_loaded=1
        info "database '$DB' already has AdventureWorks; skipping the load (use --force to rebuild)"
    fi
fi

# --------------------------------------------------------------------------
# 1-3. CSVs -> converted CSVs -> database
# --------------------------------------------------------------------------

if [[ $already_loaded -eq 0 ]]; then
    shopt -s nullglob
    csvs=("$REPO"/*.csv)
    shopt -u nullglob

    if [[ ${#csvs[@]} -lt $MIN_CSV_FILES ]]; then
        if [[ -z "$ZIP" ]]; then
            command -v curl  >/dev/null || die "curl not found (or download the zip yourself and pass --zip)"
            ZIP="$(mktemp -d)/AdventureWorks-oltp-install-script.zip"
            trap 'rm -rf "$(dirname "$ZIP")"' EXIT
            info "downloading Microsoft's AdventureWorks CSV files (~17 MB)"
            curl -fsSL -o "$ZIP" \
                "https://github.com/Microsoft/sql-server-samples/releases/download/adventureworks/AdventureWorks-oltp-install-script.zip" \
                || die "download failed. Download the zip from the link in the repo README and pass --zip PATH"
        fi
        [[ -f "$ZIP" ]] || die "zip not found: $ZIP"
        command -v unzip >/dev/null || die "unzip not found"
        info "extracting CSV files into $REPO"
        unzip -q -o "$ZIP" '*.csv' -d "$REPO"
        shopt -s nullglob; csvs=("$REPO"/*.csv); shopt -u nullglob
        [[ ${#csvs[@]} -ge $MIN_CSV_FILES ]] \
            || die "expected at least $MIN_CSV_FILES CSV files after extracting, found ${#csvs[@]}"
        rm -f "$REPO/.csvs_converted"      # fresh files: they need converting
    fi

    # update_csvs.rb rewrites every ./*.csv in place and is NOT safe to run twice,
    # so remember that it ran.
    if [[ $SKIP_CONVERT -eq 1 ]]; then
        info "--skip-convert: assuming the CSVs were already converted"
    elif [[ -f "$REPO/.csvs_converted" ]]; then
        info "CSVs already converted (marker file found)"
    else
        compgen -G "$REPO/*.csv.xyz" >/dev/null \
            && die "found leftover *.csv.xyz files: a previous conversion was interrupted. Delete the CSVs and *.xyz files, then rerun so they are extracted fresh"
        command -v ruby >/dev/null || die "ruby not found (update_csvs.rb needs it); install Ruby, or convert the CSVs elsewhere and use --skip-convert"
        info "converting CSVs for Postgres (ruby update_csvs.rb)"
        ( cd "$REPO" && ruby update_csvs.rb >/dev/null )
        touch "$REPO/.csvs_converted"
    fi

    # ----------------------------------------------------------------------
    # 4. Create the database (lowercase, UTF8) and load it
    # ----------------------------------------------------------------------
    if db_exists; then
        pg -d postgres -v db="$DB" <<<'DROP DATABASE :"db" WITH (FORCE)'
    fi
    info "creating database '$DB' (UTF8)"
    # TEMPLATE template0 lets us choose the encoding regardless of the cluster default.
    pg -d postgres -v db="$DB" <<<"CREATE DATABASE :\"db\" TEMPLATE template0 ENCODING 'UTF8'"

    info "loading install.sql (tables, data, keys, views); this takes a minute or two"
    start=$SECONDS
    # install.sql uses \copy from ./X.csv, so psql must run inside the repo folder.
    # (-o /dev/null hides the table listing install.sql prints; errors still show.)
    ( cd "$REPO" && pg -d "$DB" -o /dev/null -f install.sql )
    info "load finished in $((SECONDS - start))s"
fi

# --------------------------------------------------------------------------
# 5-6. Roles and statistics
# --------------------------------------------------------------------------

if [[ $SKIP_ROLES -eq 0 ]]; then
    info "creating roles and grants (scripts/create_roles.sql)"
    pg -d "$DB" -f "$SCRIPT_DIR/create_roles.sql" >/dev/null
fi

info "running ANALYZE (so row-count estimates exist)"
pg -d "$DB" -c "ANALYZE"

# --------------------------------------------------------------------------
# 7. Verify
# --------------------------------------------------------------------------

info "verifying"

tables="$(pg -d "$DB" -Atq <<'SQL'
SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname IN ('humanresources', 'person', 'production', 'purchasing', 'sales')
SQL
)"
[[ "$tables" -eq $EXPECTED_TABLES ]] \
    || die "expected $EXPECTED_TABLES tables in the 5 schemas, found $tables; the load is incomplete"

orders="$(pg -d "$DB" -Atq -c "SELECT count(*) FROM sales.salesorderheader")"
[[ "$orders" -gt 0 ]] || die "sales.salesorderheader is empty; the CSV load did not run"

echo
pg -d "$DB" <<'SQL'
\pset format aligned
SELECT n.nspname AS schema, count(*) AS tables
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname IN ('humanresources', 'person', 'production', 'purchasing', 'sales')
GROUP BY 1 ORDER BY 1;
SQL
echo "total tables: $tables   sales.salesorderheader rows: $orders"

if [[ $SKIP_ROLES -eq 0 ]]; then
    # Use the read-only role the way the pipeline will, and try to break it.
    ro() { PGPASSWORD="$PG_READONLY_PASSWORD" psql -X -w -q -U sg_readonly -d "$DB" "$@"; }
    ro_count="$(ro -v ON_ERROR_STOP=1 -Atq -c "SELECT count(*) FROM sales.salesorderheader")" \
        || die "sg_readonly cannot read sales.salesorderheader (check the password variable and pg_hba.conf)"
    [[ "$ro_count" == "$orders" ]] || die "sg_readonly sees $ro_count orders, expected $orders"

    if ro -c "CREATE TABLE sg_probe (x int)" >/dev/null 2>&1; then
        die "SAFETY: sg_readonly was able to CREATE a table"
    fi
    if ro -c "UPDATE sales.salesorderheader SET status = status WHERE false" >/dev/null 2>&1; then
        die "SAFETY: sg_readonly has UPDATE privilege on sales.salesorderheader"
    fi
    echo "sg_readonly: can SELECT; CREATE and UPDATE are refused"
fi

echo
info "done. AdventureWorks is in database '$DB' (use db_id: $DB)"