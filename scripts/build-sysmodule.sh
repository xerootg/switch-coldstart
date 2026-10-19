#!/usr/bin/env bash
# Build sys-botbase from source at a pinned revision with devkitPro.
#
#   sysmodule/src/                          the clone (git, pinned, detached)
#   sysmodule/out/430000000000000B/exefs.nsp
#
# Pin: ae13548c587b75abf54f32032ad8d52f51eda05e ("Missed a metadata check (#109)"),
# one commit after the v2.5 tag (45b6a64). It still reports version "2.5"; it is
# the revision reference/sys-botbase-src/ was taken from (main.c, commands.c and
# util.c are byte-identical), and differs from v2.5 only by a getMetaData check.
# Override with SYS_BOTBASE_REV=<tag or sha> (e.g. v2.5).
set -euo pipefail

REPO_URL="https://github.com/olliz0r/sys-botbase"
REV="${SYS_BOTBASE_REV:-ae13548c587b75abf54f32032ad8d52f51eda05e}"
PROGRAM_ID="430000000000000B"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="${here}/sysmodule/src"
out="${here}/sysmodule/out/${PROGRAM_ID}"

die() { echo "build-sysmodule: $*" >&2; exit 1; }

export DEVKITPRO="${DEVKITPRO:-/opt/devkitpro}"
[ -d "${DEVKITPRO}/devkitA64" ] || die "devkitA64 not found under DEVKITPRO=${DEVKITPRO}; install devkitPro (switch-dev) first"
[ -d "${DEVKITPRO}/libnx" ] || die "libnx not found under DEVKITPRO=${DEVKITPRO}; install devkitPro's switch-dev group"
[ -f "${DEVKITPRO}/libnx/switch_rules" ] || die "${DEVKITPRO}/libnx/switch_rules missing; libnx install looks broken"
command -v git >/dev/null || die "git not found"
command -v make >/dev/null || die "make not found"
export PATH="${DEVKITPRO}/tools/bin:${DEVKITPRO}/devkitA64/bin:${PATH}"

if [ ! -d "${src}/.git" ]; then
    echo "cloning ${REPO_URL} into ${src}"
    git clone --quiet "${REPO_URL}" "${src}"
fi
if ! git -C "${src}" cat-file -e "${REV}^{commit}" 2>/dev/null; then
    git -C "${src}" fetch --quiet --tags origin
fi
git -C "${src}" -c advice.detachedHead=false checkout --quiet --force "${REV}"
echo "sys-botbase at $(git -C "${src}" rev-parse HEAD) ($(git -C "${src}" describe --tags --always))"

jobs="${JOBS:-8}"
make -C "${src}" clean >/dev/null 2>&1 || true
make -C "${src}" -j"${jobs}"

nsp="${src}/sys-botbase/sys-botbase.nsp"
[ -f "${nsp}" ] || die "build finished but ${nsp} is missing"
mkdir -p "${out}"
cp "${nsp}" "${out}/exefs.nsp"
echo "built ${out}/exefs.nsp ($(wc -c < "${out}/exefs.nsp") bytes)"
echo "next: bin/coldstart install            (prints the plan; --yes to upload)"
