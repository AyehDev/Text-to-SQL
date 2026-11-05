#!/usr/bin/env bash
# Load the BIRD mini-dev databases into PostgreSQL for SchemaGraph.
#
# The mini-dev package (NOT part of the bird-bench/mini_dev git repo; it is a
# separate download) has a PostgreSQL folder with one file, BIRD_dev.sql. BIRD's
# own instructions load that whole file into a single database called "BIRD".
# SchemaGraph wants ONE POSTGRES DATABASE PER db_id (lowercase, ^[a-z0-9_]+$),
# so this script:
#   1. finds BIRD_dev.sql        (--dump, or inside --zip, or downloads the zip)
#   2. loads it into a staging database, logging any errors instead of dying
#      on the first one (dumps often reference roles that do not exist here)
#   3. works out how BIRD laid its databases out, and ends with one database
#      per BIRD db_id:
#        A. the dump created databases itself      -> used as they are
#        B. the dump uses one schema per database  -> each schema is copied
#                                                     into its own database
#        C. every table is in staging's "public"   -> split by scripts/bird_tables.tsv
#           (the mini-dev PostgreSQL dump does this)  (db_id <TAB> table), after checking
#                                                     the file against the real tables
#        D. anything else                          -> stops and prints what it
#                                                     found, touching nothing more
#   4. runs scripts/create_roles.sql, ANALYZE, and checks sg_readonly on each
#   5. copies mini_dev_postgresql.json (the questions) next to the data, if found
#
# Usage:
#   scripts/load_bird.sh [options]
#
#   --dump PATH      BIRD_dev.sql, already extracted
#   --zip PATH       minidev.zip you downloaded (default: download it)
#   --url URL        where to download minidev.zip from
#   --staging NAME   staging database                 (default: bird_staging)
#   --from-staging   skip the download and load: split the staging database that is
#                    already there (it is kept, so you can rerun this)
#   --mapping FILE   table-to-database list for layout C (default: scripts/bird_tables.tsv)
#   --keep-staging   do not drop the staging database at the end
#   --questions DIR  copy mini_dev_postgresql.json here   (default: data/bird)
#   --skip-roles     do not create/grant sg_admin and sg_readonly
#   --force          drop and recreate staging and the per-db databases
#   -h, --help
#
# Connection: standard libpq variables (PGHOST, PGPORT, PGUSER, PGPASSWORD).
# The user must be a SUPERUSER. Unless --skip-roles, also export
#   PG_ADMIN_PASSWORD=...  PG_READONLY_PASSWORD=...

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_URL="https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip"
SYSTEM_DBS="postgres template0 template1"

DUMP=""
ZIP=""
URL="$DEFAULT_URL"
STAGING="bird_staging"
KEEP_STAGING=0
QUESTIONS_DIR="data/bird"
SKIP_ROLES=0
FORCE=0
FROM_STAGING=0
MAPPING="$SCRIPT_DIR/bird_tables.tsv"

die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dump)         [[ $# -ge 2 ]] || die "--dump needs a path"; DUMP="$2"; shift 2 ;;
        --zip)          [[ $# -ge 2 ]] || die "--zip needs a path";  ZIP="$2";  shift 2 ;;
        --url)          [[ $# -ge 2 ]] || die "--url needs a value"; URL="$2";  shift 2 ;;
        --staging)      [[ $# -ge 2 ]] || die "--staging needs a name"; STAGING="$2"; shift 2 ;;
        --questions)    [[ $# -ge 2 ]] || die "--questions needs a folder"; QUESTIONS_DIR="$2"; shift 2 ;;
        --from-staging) FROM_STAGING=1; KEEP_STAGING=1; shift ;;
        --mapping)      [[ $# -ge 2 ]] || die "--mapping needs a file"; MAPPING="$2"; shift 2 ;;
        --keep-staging) KEEP_STAGING=1; shift ;;
        --skip-roles)   SKIP_ROLES=1;   shift ;;
        --force)        FORCE=1;        shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "unknown option: $1 (try --help)" ;;
    esac
done

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

[[ "$STAGING" =~ ^[a-z0-9_]+$ ]] || die "--staging must match ^[a-z0-9_]+$; got '$STAGING'"
command -v psql    >/dev/null || die "psql not found; install the PostgreSQL client"
command -v pg_dump >/dev/null || die "pg_dump not found; install the PostgreSQL client tools"

export PGOPTIONS="${PGOPTIONS:-} -c client_min_messages=warning"
# -w: never prompt for a password (see load_adventureworks.sh for why).
pg() { psql -X -w -q -v ON_ERROR_STOP=1 "$@"; }

is_super="$(pg -d postgres -Atc "SELECT rolsuper FROM pg_roles WHERE rolname = current_user" 2>&1)" \
    || die "cannot connect to PostgreSQL as '${PGUSER:-$USER}' on ${PGHOST:-the local socket}:${PGPORT:-5432}.
    psql said: $is_super
    Check PGHOST/PGPORT/PGUSER, and if the server needs a password, export PGPASSWORD
    (verify first with:  psql -w -d postgres -c 'select 1')"
[[ "$is_super" == "t" ]] || die "the connecting user must be a superuser; set PGUSER, e.g. PGUSER=postgres"

if [[ $SKIP_ROLES -eq 0 ]]; then
    : "${PG_ADMIN_PASSWORD:?set PG_ADMIN_PASSWORD (or pass --skip-roles)}"
    : "${PG_READONLY_PASSWORD:?set PG_READONLY_PASSWORD (or pass --skip-roles)}"
    psql_major="$(psql --version | grep -oE '[0-9]+' | head -1)"
    [[ "$psql_major" -ge 15 ]] \
        || die "create_roles.sql needs psql 15+ (\\getenv); yours is $psql_major. Upgrade the client or use --skip-roles"
    [[ -f "$SCRIPT_DIR/create_roles.sql" ]] || die "missing $SCRIPT_DIR/create_roles.sql"
fi

list_dbs() {
    pg -d postgres -Atq -c "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY 1"
}
db_exists() {
    [[ "$(pg -d postgres -Atq -v db="$1" <<<"SELECT 1 FROM pg_database WHERE datname = :'db'")" == "1" ]]
}
create_db() {   # lowercase UTF8 database; TEMPLATE template0 lets us pick the encoding
    if db_exists "$1"; then
        [[ $FORCE -eq 1 ]] || die "database '$1' already exists; use --force to drop and rebuild it"
        pg -d postgres -v db="$1" <<<'DROP DATABASE :"db" WITH (FORCE)'
    fi
    pg -d postgres -v db="$1" <<<"CREATE DATABASE :\"db\" TEMPLATE template0 ENCODING 'UTF8'"
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --------------------------------------------------------------------------
# 1. Find BIRD_dev.sql
# --------------------------------------------------------------------------

find_in() {   # first file named $2 under folder $1; one under a "postgres" folder wins
    local all; all="$(find "$1" -type f -iname "$2" 2>/dev/null | sort)"
    printf '%s\n' "$all" | grep -i postgres | head -1 || true
    [[ -n "$(printf '%s\n' "$all" | grep -i postgres || true)" ]] || printf '%s\n' "$all" | head -1
}

EXTRACTED=""
LOG="$WORK/load.log"
if [[ $FROM_STAGING -eq 1 ]]; then
    db_exists "$STAGING" || die "--from-staging: database '$STAGING' does not exist (run without --from-staging to load it)"
    : >"$LOG"
    before="$(list_dbs | sort)"
    info "using the existing staging database '$STAGING' (no download, no load)"
else
# (sections 1 and 2 below only run without --from-staging)
if [[ -z "$DUMP" ]]; then
    if [[ -z "$ZIP" ]]; then
        command -v curl >/dev/null || die "curl not found (or download minidev.zip yourself and pass --zip)"
        ZIP="$WORK/minidev.zip"
        info "downloading minidev.zip from $URL"
        curl -fL --retry 3 -o "$ZIP" "$URL" \
            || die "download failed. Download minidev.zip from the bird-bench/mini_dev README (the Aliyun link or the Google Drive link) and pass --zip PATH"
    fi
    [[ -f "$ZIP" ]] || die "zip not found: $ZIP"
    command -v unzip >/dev/null || die "unzip not found"
    EXTRACTED="$WORK/unzipped"
    info "extracting $ZIP"
    unzip -q -o "$ZIP" -d "$EXTRACTED"
    DUMP="$(find_in "$EXTRACTED" 'BIRD_dev.sql')" || true
    [[ -n "$DUMP" ]] || die "no BIRD_dev.sql inside $ZIP. Contents:
$(unzip -l "$ZIP" | head -40)"
fi
[[ -f "$DUMP" ]] || die "dump not found: $DUMP"
info "using dump: $DUMP ($(du -h "$DUMP" | cut -f1))"

# --------------------------------------------------------------------------
# 2. Load into staging
# --------------------------------------------------------------------------

before="$(list_dbs | sort)"
create_db "$STAGING"
LOG="$WORK/load.log"
info "loading the dump into '$STAGING' (errors are logged, not fatal)"
start=$SECONDS
# No ON_ERROR_STOP here: dumps made on another machine often fail on
# "ALTER ... OWNER TO <role>" and similar, which do not affect the data.
psql -X -w -q -d "$STAGING" -o /dev/null -f "$DUMP" 2>"$LOG" || true
info "load finished in $((SECONDS - start))s"
errors="$(grep -c '^psql:.*ERROR' "$LOG" || true)"
if [[ "$errors" -gt 0 ]]; then
    echo "    $errors error(s) while loading; kinds seen:"
    grep 'ERROR' "$LOG" | sed -E 's/^psql:[^:]*:[0-9]+: //; s/"[^"]*"/"..."/g' | sort | uniq -c | sort -rn | head -8 | sed 's/^/      /'
fi
fi   # end of "not --from-staging"

# --------------------------------------------------------------------------
# 3. Work out the layout
# --------------------------------------------------------------------------

after="$(list_dbs | sort)"
new_dbs="$(comm -13 <(echo "$before") <(echo "$after") | grep -vx "$STAGING" || true)"

schemas="$(pg -d "$STAGING" -Atq <<'SQL'
SELECT n.nspname
FROM pg_namespace n
WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname NOT IN ('information_schema', 'public')
  AND EXISTS (SELECT 1 FROM pg_class c WHERE c.relnamespace = n.oid AND c.relkind IN ('r', 'p'))
ORDER BY 1
SQL
)"
public_tables="$(pg -d "$STAGING" -Atq -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r','p')")"

TARGETS=()   # final database names (one per BIRD db_id)

sanitize() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/_/g; s/^_+//; s/_+$//'; }

if [[ -n "$new_dbs" ]]; then
    info "layout A: the dump created its own databases"
    while read -r d; do
        [[ "$d" =~ ^[a-z0-9_]+$ ]] \
            || die "the dump created database '$d', which is not a valid db_id (^[a-z0-9_]+$); rename it in Postgres and rerun the checks"
        TARGETS+=("$d")
    done <<<"$new_dbs"
elif [[ -n "$schemas" ]]; then
    info "layout B: one schema per BIRD database; copying each into its own database"
    declare -A seen=()
    while read -r s; do
        d="$(sanitize "$s")"
        [[ -n "$d" ]] || die "schema '$s' has no usable database name"
        [[ -z "${seen[$d]:-}" ]] || die "schemas '${seen[$d]}' and '$s' both map to database '$d'"
        seen[$d]="$s"
        create_db "$d"
        # --no-owner / --no-privileges: the owner roles from the dump do not exist here.
        pg_dump -w --no-owner --no-privileges -n "\"$s\"" "$STAGING" | psql -X -w -q -d "$d" -o /dev/null 2>>"$LOG" || true
        n="$(pg -d "$d" -Atq -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = '$s' AND c.relkind IN ('r','p')")"
        [[ "$n" -gt 0 ]] || die "copying schema '$s' into '$d' produced no tables (see errors in the load log)"
        echo "    $s -> database '$d' ($n tables)"
        TARGETS+=("$d")
    done <<<"$schemas"
elif [[ "$public_tables" -gt 0 && -f "$MAPPING" ]]; then
    info "layout C: every table is in $STAGING.public; splitting by $MAPPING"
    declare -A owner=()      # lowercase table name -> db_id
    declare -A seen_db=()
    db_order=()
    while IFS=$'\t' read -r d t _; do
        [[ -z "${d// /}" || "$d" == \#* ]] && continue
        [[ "$d" =~ ^[a-z0-9_]+$ ]] || die "$MAPPING: '$d' is not a valid db_id (^[a-z0-9_]+$)"
        [[ -n "${t:-}" ]] || die "$MAPPING: no table on the line for '$d'"
        t="$(printf '%s' "$t" | tr '[:upper:]' '[:lower:]')"
        [[ -z "${owner[$t]:-}" ]] || die "$MAPPING: table '$t' is listed under both '${owner[$t]}' and '$d'"
        owner[$t]="$d"
        if [[ -z "${seen_db[$d]:-}" ]]; then seen_db[$d]=1; db_order+=("$d"); fi
    done <"$MAPPING"

    mapfile -t actual < <(pg -d "$STAGING" -Atq -c "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r','p') ORDER BY 1")
    declare -A present=()
    unassigned=()
    for name in "${actual[@]}"; do
        low="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')"
        present[$low]=1
        [[ -n "${owner[$low]:-}" ]] || unassigned+=("$name")
    done
    missing=()
    for low in "${!owner[@]}"; do [[ -n "${present[$low]:-}" ]] || missing+=("$low"); done
    if [[ ${#unassigned[@]} -gt 0 || ${#missing[@]} -gt 0 ]]; then
        echo "STOP: $MAPPING does not match the tables in $STAGING.public. Nothing was split." >&2
        echo "  in $STAGING but not in the mapping (${#unassigned[@]}): ${unassigned[*]:-none}" >&2
        echo "  in the mapping but not in $STAGING (${#missing[@]}): ${missing[*]:-none}" >&2
        echo "  Fix the mapping file (one 'db_id<TAB>table' line per table) and rerun with --from-staging." >&2
        exit 2
    fi

    for d in "${db_order[@]}"; do
        args=()
        names=()
        for name in "${actual[@]}"; do
            low="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')"
            if [[ "${owner[$low]}" == "$d" ]]; then args+=(-t "public.\"$name\""); names+=("$name"); fi
        done
        create_db "$d"
        pg_dump -w --no-owner --no-privileges "${args[@]}" "$STAGING" | psql -X -w -q -d "$d" -o /dev/null 2>>"$LOG" || true
        # every table must have arrived with every row
        for name in "${names[@]}"; do
            want="$(pg -d "$STAGING" -Atq -c "SELECT count(*) FROM public.\"$name\"")"
            got="$(pg -d "$d" -Atq -c "SELECT count(*) FROM public.\"$name\"" 2>/dev/null)" \
                || die "table '$name' is missing from '$d' after the copy. Load log tail:
$(tail -5 "$LOG")"
            [[ "$got" == "$want" ]] || die "table '$name' in '$d' has $got rows; staging has $want"
        done
        echo "    -> database '$d' (${#names[@]} tables)"
        TARGETS+=("$d")
    done
else
    echo "STOP: layout not recognised. Nothing was split." >&2
    echo "  new databases created by the dump : none" >&2
    echo "  schemas (besides public) with tables: none" >&2
    echo "  tables in $STAGING.public           : $public_tables" >&2
    pg -d "$STAGING" -Atq -c "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY 1 LIMIT 60" | sed 's/^/    /' >&2
    echo "  The staging database '$STAGING' is left in place. Send the table list above" >&2
    echo "  (and the first 40 lines of the dump) so the splitting rule can be written." >&2
    exit 2
fi

[[ ${#TARGETS[@]} -gt 0 ]] || die "no databases to prepare"

# --------------------------------------------------------------------------
# 4. Roles, statistics, verification (per database)
# --------------------------------------------------------------------------

summary=()
for d in "${TARGETS[@]}"; do
    if [[ $SKIP_ROLES -eq 0 ]]; then
        pg -d "$d" -f "$SCRIPT_DIR/create_roles.sql" >/dev/null
    fi
    pg -d "$d" -c "ANALYZE"

    tables="$(pg -d "$d" -Atq -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'")"
    [[ "$tables" -gt 0 ]] || die "database '$d' has no tables"
    # one real table to probe with
    probe="$(pg -d "$d" -Atq -c "SELECT format('%I.%I', n.nspname, c.relname) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema' ORDER BY 1 LIMIT 1")"
    rows="$(pg -d "$d" -Atq -c "SELECT count(*) FROM $probe")"

    if [[ $SKIP_ROLES -eq 0 ]]; then
        ro() { PGPASSWORD="$PG_READONLY_PASSWORD" psql -X -w -q -U sg_readonly -d "$d" "$@"; }
        ro_rows="$(ro -v ON_ERROR_STOP=1 -Atq -c "SELECT count(*) FROM $probe")" \
            || die "sg_readonly cannot read $probe in '$d' (check PG_READONLY_PASSWORD and pg_hba.conf)"
        [[ "$ro_rows" == "$rows" ]] || die "sg_readonly sees $ro_rows rows in $probe, expected $rows"
        if ro -c "CREATE TABLE sg_probe (x int)" >/dev/null 2>&1; then
            die "SAFETY: sg_readonly was able to CREATE a table in '$d'"
        fi
        if ro -c "DELETE FROM $probe WHERE false" >/dev/null 2>&1; then
            die "SAFETY: sg_readonly has DELETE privilege on $probe in '$d'"
        fi
    fi
    summary+=("$(printf '%-28s %4s tables' "$d" "$tables")")
done

if [[ $KEEP_STAGING -eq 0 ]]; then
    case " $SYSTEM_DBS " in *" $STAGING "*) die "refusing to drop system database $STAGING" ;; esac
    # Layout A may have used the staging db itself as a target; never drop a target.
    keep=0; for d in "${TARGETS[@]}"; do [[ "$d" == "$STAGING" ]] && keep=1; done
    if [[ $keep -eq 0 ]]; then
        pg -d postgres -v db="$STAGING" <<<'DROP DATABASE IF EXISTS :"db" WITH (FORCE)'
        info "dropped staging database '$STAGING'"
    fi
fi

# --------------------------------------------------------------------------
# 5. Questions file
# --------------------------------------------------------------------------

if [[ -n "$EXTRACTED" ]]; then
    q="$(find_in "$EXTRACTED" 'mini_dev_postgresql.json')" || true
    if [[ -n "$q" ]]; then
        mkdir -p "$QUESTIONS_DIR"
        cp "$q" "$QUESTIONS_DIR/mini_dev_postgresql.json"
        info "questions copied to $QUESTIONS_DIR/mini_dev_postgresql.json"
    else
        echo "note: mini_dev_postgresql.json was not in the zip; copy it into $QUESTIONS_DIR yourself" >&2
    fi
elif [[ $FROM_STAGING -eq 1 ]]; then
    echo "note: --from-staging does not look for the questions file. Put mini_dev_postgresql.json in $QUESTIONS_DIR (see the README)." >&2
    echo "      The staging database '$STAGING' was kept; drop it when you are happy:  psql -d postgres -c 'DROP DATABASE $STAGING'" >&2
else
    echo "note: --dump was used, so the questions file was not looked for. Copy mini_dev_postgresql.json into $QUESTIONS_DIR" >&2
fi

echo
printf '%s\n' "${summary[@]}"
echo
info "done. Use these as db_id values:"
printf '    %s\n' "${TARGETS[@]}"
echo "    If the pipeline needs a database list, add them to config.yml / run:"
echo "      python -m schemagraph.extraction.extract --db <db_id>"