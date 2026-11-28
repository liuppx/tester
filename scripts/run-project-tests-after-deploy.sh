#!/usr/bin/env bash
set -Eeuo pipefail

if (( $# != 1 )); then
    echo 'Usage: run-project-tests-after-deploy.sh <project>' >&2
    exit 2
fi

project=$1
tester=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

if [[ "${TESTER_ARTIFACT_MODE:-0}" == 1 ]]; then
    exec "$tester/scripts/run-full-on-test-machine.sh" "$project"
fi

exec "$tester/scripts/test-tester-main.sh" --project "$project"
