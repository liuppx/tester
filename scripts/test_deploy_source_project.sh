#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
tmp=$(mktemp -d /tmp/tester-source-adapter.XXXXXX)
trap 'python3 -c '\''import shutil, sys; shutil.rmtree(sys.argv[1])'\'' "$tmp"' EXIT

mkdir -p "$tmp/seed" "$tmp/source" "$tmp/tester/scripts" "$tmp/bin"
git -C "$tmp/seed" init -q -b main
git -C "$tmp/seed" config user.name Tester
git -C "$tmp/seed" config user.email tester@example.invalid
printf 'old\n' > "$tmp/seed/example.md"
git -C "$tmp/seed" add example.md
git -C "$tmp/seed" commit -qm old
git clone -q --bare "$tmp/seed" "$tmp/origin.git"
git --git-dir="$tmp/origin.git" symbolic-ref HEAD refs/heads/main
git -C "$tmp/seed" remote add origin "$tmp/origin.git"
git clone -q "$tmp/origin.git" "$tmp/source/book"

printf 'new\n' > "$tmp/seed/example.md"
git -C "$tmp/seed" commit -qam new
git --git-dir="$tmp/origin.git" fetch -q --force "$tmp/seed" main:refs/heads/main
target=$(git -C "$tmp/seed" rev-parse HEAD)

sed '/^export PATH=/d' "$script_dir/deploy-and-test-project.sh" > "$tmp/tester/scripts/deploy-and-test-project.sh"
cat > "$tmp/bin/flock" <<'FLOCK'
#!/usr/bin/env bash
exit 0
FLOCK
chmod +x "$tmp/bin/flock" "$tmp/tester/scripts/deploy-and-test-project.sh"
cat > "$tmp/tester/scripts/test-tester-main.sh" <<'RUNNER'
#!/usr/bin/env bash
printf '%s %s %s\n' "$2" "$TESTER_TARGET_SHA" "$BOOK_REPO_PATH" > "$TESTER_RUN_OUTPUT"
RUNNER
chmod +x "$tmp/tester/scripts/"*.sh

PATH="$tmp/bin:$PATH" \
TESTER_SOURCE_ROOT="$tmp/source" \
TESTER_LOCK_FILE="$tmp/test.lock" \
TESTER_PRIVATE_REPORT_DIR="$tmp/private" \
TESTER_STUCK_CHROME_LIMIT=100 \
TESTER_RUN_OUTPUT="$tmp/ran.txt" \
    "$tmp/tester/scripts/deploy-and-test-project.sh" book "$target"

[[ "$(git -C "$tmp/source/book" rev-parse HEAD)" == "$target" ]]
[[ "$(cat "$tmp/ran.txt")" == "book $target $tmp/source/book" ]]

# A locally edited source tree must never be overwritten by a later main.
printf 'local edit\n' > "$tmp/source/book/example.md"
printf 'third\n' > "$tmp/seed/example.md"
git -C "$tmp/seed" commit -qam third
git --git-dir="$tmp/origin.git" fetch -q --force "$tmp/seed" main:refs/heads/main
new_target=$(git -C "$tmp/seed" rev-parse HEAD)
if PATH="$tmp/bin:$PATH" \
   TESTER_SOURCE_ROOT="$tmp/source" \
   TESTER_LOCK_FILE="$tmp/test.lock" \
   TESTER_PRIVATE_REPORT_DIR="$tmp/private" \
   TESTER_STUCK_CHROME_LIMIT=100 \
   "$tmp/tester/scripts/deploy-and-test-project.sh" book "$new_target" > "$tmp/rejected.log" 2>&1; then
    echo 'Dirty source tree was unexpectedly updated' >&2
    exit 1
fi
[[ "$(git -C "$tmp/source/book" rev-parse HEAD)" == "$target" ]]
grep -q 'tracked local changes' "$tmp/rejected.log"
echo 'source adapter: fast-forward, target SHA, test path, and dirty-tree guard passed'
