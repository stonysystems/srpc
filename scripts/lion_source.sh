# Lion's bytes at a commit, for the scripts that need them outside Cargo:
# scripts/verify_lion.sh and the --compare modes of scripts/run_rpc_echo_bench.sh
# and scripts/run_microbench.sh. Source it; it defines functions only.
#
# SRPC takes Lion as a Cargo git dependency (Cargo.toml, `rev = ...`), so a
# build needs no copy of Lion: Cargo fetches the commit into CARGO_HOME. These
# helpers serve the two cases Cargo does not: Lion's own CI run on the pinned
# commit, and building an older SRPC ref whose Cargo.toml still reached Lion by
# path through the `third-party/lion` submodule (21f2320 and older).
#
# Bytes always come from `git archive` of a commit in a bare clone of
# LION_GIT_URL, never from a working tree, so a modified checkout cannot change
# them. A missing commit is fetched on demand: first from Cargo's own git
# database (offline, when Cargo has fetched Lion before), then from the URL.

# Lion's repository, as Cargo.toml's git dependencies name it.
LION_GIT_URL="https://github.com/stonysystems/lion"

# lion_cache_default: the bare clone the benchmark scripts share.
lion_cache_default() {
    printf '%s\n' "${XDG_CACHE_HOME:-$HOME/.cache}/srpc/lion.git"
}

# lion_locked_commit <repo>: the one commit <repo>/Cargo.lock resolves every
# package from LION_GIT_URL to. Fails unless there is at least one such package
# and they all agree on a full commit hash.
lion_locked_commit() {
    python3 -I - "$1/Cargo.lock" "$LION_GIT_URL" <<'PY'
import re
import sys
import tomllib

path, url = sys.argv[1], sys.argv[2]
with open(path, 'rb') as stream:
    packages = tomllib.load(stream).get('package', [])
commits = {}
for package in packages:
    source = package.get('source') or ''
    if source.startswith(f'git+{url}?') or source.startswith(f'git+{url}#'):
        commits.setdefault(source.rsplit('#', 1)[-1], []).append(package['name'])
if not commits:
    sys.exit(f'{path} resolves no package from {url}')
if len(commits) != 1:
    sys.exit(f'{path} resolves Lion packages to more than one commit: {commits}')
(commit, names), = commits.items()
if not re.fullmatch(r'[0-9a-f]{40}', commit):
    sys.exit(f'{path} records Lion commit {commit!r}, not a full commit hash')
print(commit)
PY
}

# lion_fetch <cache> <sha>: make sure the bare clone <cache> has commit <sha>,
# under refs/srpc/lion/<sha> so that `git gc` keeps it.
lion_fetch() {
    local cache="$1" sha="$2" db
    if [ ! -f "$cache/HEAD" ]; then
        mkdir -p "$cache"
        git init --bare --quiet "$cache"
    fi
    if git -C "$cache" cat-file -e "$sha^{commit}" 2>/dev/null; then
        return 0
    fi
    for db in "${CARGO_HOME:-$HOME/.cargo}"/git/db/lion-*; do
        [ -d "$db" ] || continue
        if git -c "safe.directory=$db" -C "$cache" fetch --quiet --no-tags "$db" \
                "+$sha:refs/srpc/lion/$sha" 2>/dev/null; then
            echo "lion_source: commit $sha from Cargo's git database $db" >&2
            return 0
        fi
    done
    echo "lion_source: fetching commit $sha from $LION_GIT_URL into $cache" >&2
    if ! git -C "$cache" fetch --quiet --no-tags "$LION_GIT_URL" "+$sha:refs/srpc/lion/$sha"; then
        echo "lion_source: cannot fetch Lion commit $sha from $LION_GIT_URL" >&2
        return 1
    fi
    git -C "$cache" cat-file -e "$sha^{commit}"
}

# lion_archive <cache> <sha> <dest>: extract Lion at <sha> into the existing
# directory <dest>.
lion_archive() {
    local cache="$1" sha="$2" dest="$3"
    lion_fetch "$cache" "$sha" || return 1
    git -C "$cache" archive --format=tar "$sha" | tar -x -C "$dest"
}
