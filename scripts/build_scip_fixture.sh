#!/usr/bin/env bash
# Build tests/fixtures/m3typed/index.scip.
#
# Preferred path: a real `scip-java` binary compiles the fixture and emits an
# authentic SCIP index. The binary is never downloaded by this script — it
# must already be on PATH or pointed to via SCIP_JAVA_BIN, since this repo
# does not hardcode any download URL (keeps the public repo free of internal
# network references and avoids depending on a specific release mirror).
#
# Fallback: no binary is available (e.g. offline CI), so this script runs
# scripts/gen_deterministic_scip_fixture.py, which hand-encodes the same
# scip.proto wire format directly from the fixture's known source layout.
# Both paths produce tests/fixtures/m3typed/index.scip; the checked-in file
# was produced by the fallback (see docs/adr/0003-typed-overlay.md).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE_DIR="${ROOT_DIR}/tests/fixtures/m3typed"
OUT_FILE="${FIXTURE_DIR}/index.scip"

SCIP_JAVA_BIN="${SCIP_JAVA_BIN:-}"
if [[ -z "${SCIP_JAVA_BIN}" ]] && command -v scip-java >/dev/null 2>&1; then
  SCIP_JAVA_BIN="$(command -v scip-java)"
fi

if [[ -n "${SCIP_JAVA_BIN}" && -x "${SCIP_JAVA_BIN}" ]]; then
  echo "Using scip-java binary: ${SCIP_JAVA_BIN}"
  CLASSES_DIR="$(mktemp -d)"
  trap 'rm -rf "${CLASSES_DIR}"' EXIT

  mapfile -t JAVA_FILES < <(find "${FIXTURE_DIR}" -name '*.java' | sort)
  javac -d "${CLASSES_DIR}" "${JAVA_FILES[@]}"

  pushd "${FIXTURE_DIR}" >/dev/null
  "${SCIP_JAVA_BIN}" index-classpath \
    --classpath "${CLASSES_DIR}" \
    --output "${OUT_FILE}" \
    "${JAVA_FILES[@]#"${FIXTURE_DIR}/"}"
  popd >/dev/null

  echo "Wrote real scip-java index: ${OUT_FILE}"
else
  echo "No scip-java binary found (set SCIP_JAVA_BIN or add it to PATH)."
  echo "Falling back to the deterministic fixture generator."
  python3 "${ROOT_DIR}/scripts/gen_deterministic_scip_fixture.py"
fi
