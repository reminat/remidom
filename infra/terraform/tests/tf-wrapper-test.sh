#!/usr/bin/env bash
# Isolated tests for ../tf.sh log confidentiality.
#
# Runs a copy of tf.sh in a temporary directory with fake bw, bws, terraform, ssh, scp and
# rsync placed first in PATH, a temporary HOME and fictitious sentinel values only.
# No credentials, no network access, no real infrastructure tool is used.
#
# Usage: bash infra/terraform/tests/tf-wrapper-test.sh
# Exit code: 0 if all cases pass, 1 otherwise.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="${HERE}/../tf.sh"
[ -f "$WRAPPER" ] || { echo "tf.sh not found next to tests/" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required (used by tf.sh)" >&2; exit 1; }

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

# Fictitious values. Every sentinel contains "SENTINEL" or "sentinel-" so that any
# leak, even partial (a prefix), is detected by the same search.
FAKE_V_BWS=SENTINEL_V1_7f3a
FAKE_V_SESSION=SENTINEL_V2_5b77
FAKE_V_PM1=SENTINEL_V3_9c1e
FAKE_V_PM2=SENTINEL_V4_44b2
FAKE_V_PM3=SENTINEL_V5_d81f
FAKE_V_PM4='["SENTINEL_V6_a0e5"]'
LEAK_PATTERN='SENTINEL|sentinel-'

# --- Fixture -----------------------------------------------------------------

mkdir -p "$T/bin" "$T/home" "$T/tf/envs/test/pve/vm" "$T/tf/envs/test/pve/withconf"
cp "$WRAPPER" "$T/tf/tf.sh"
for d in vm withconf; do
  printf 'variable "ssh_authorized_keys" {\n  type = list(string)\n}\n' > "$T/tf/envs/test/pve/$d/variables.tf"
  touch "$T/tf/envs/test/pve/$d/terraform.tfstate"
done
# tf.conf that tries to re-enable tracing (no template build: no TEMPLATE_* variables)
printf 'set -x\nFOO=bar\n' > "$T/tf/envs/test/pve/withconf/tf.conf"

cat > "$T/bin/bw" <<EOF
#!/usr/bin/env bash
echo "bw \$1" >> "\$CALLS"
case "\$1" in
  status)
    s="\${FAKE_BW_STATUS:-unlocked}"
    [ "\${BW_SESSION:-}" = "$FAKE_V_SESSION" ] && s=unlocked
    printf '{"status":"%s"}\n' "\$s" ;;
  unlock) printf '%s' "$FAKE_V_SESSION" ;;
  login) exit 0 ;;
  get) printf '%s' "$FAKE_V_BWS" ;;
esac
EOF

cat > "$T/bin/bws" <<EOF
#!/usr/bin/env bash
echo "bws \$1 \$2" >> "\$CALLS"
[ "\${BWS_ACCESS_TOKEN:-}" = "$FAKE_V_BWS" ] || { echo "fake bws: unexpected token" >&2; exit 1; }
case "\$1 \$2" in
  "secret list")
    echo '[{"key":"proxmox_api_token_secret","id":"11111111-1111-1111-1111-111111111111"},
           {"key":"proxmox_api_token_id","id":"22222222-2222-2222-2222-222222222222"},
           {"key":"proxmox_endpoint","id":"33333333-3333-3333-3333-333333333333"},
           {"key":"ssh_authorized_keys","id":"44444444-4444-4444-4444-444444444444"}]' ;;
  "secret get")
    case "\$3" in
      1111*) v='$FAKE_V_PM1' ;;
      2222*) v='$FAKE_V_PM2' ;;
      3333*) v='$FAKE_V_PM3' ;;
      4444*) v='$FAKE_V_PM4' ;;
      *) exit 1 ;;
    esac
    [ "\${FAKE_EMPTY_SECRET:-}" = "\$3" ] && v=""
    python3 -c 'import json,sys; print(json.dumps({"id": sys.argv[1], "value": sys.argv[2]}))' "\$3" "\$v" ;;
esac
EOF

# Fake terraform: checks the injected variables, never prints them or its arguments.
cat > "$T/bin/terraform" <<EOF
#!/usr/bin/env bash
ok=1
[ "\${TF_VAR_pm_api_token_secret:-}" = '$FAKE_V_PM1' ] || ok=0
[ "\${TF_VAR_pm_api_token_id:-}" = '$FAKE_V_PM2' ] || ok=0
[ "\${TF_VAR_pm_endpoint:-}" = '$FAKE_V_PM3' ] || ok=0
[ "\${TF_VAR_ssh_authorized_keys:-}" = '$FAKE_V_PM4' ] || ok=0
echo "terraform vars_ok=\$ok" >> "\$CALLS"
printf '%s\n' "\$@" > "\$ARGS"
echo "fake terraform output"
exit "\${FAKE_TF_RC:-0}"
EOF

for c in ssh scp rsync; do
  printf '#!/usr/bin/env bash\necho "%s" >> "$CALLS"\nexit 0\n' "$c" > "$T/bin/$c"
done
chmod 755 "$T/bin/"*

# --- Runner ------------------------------------------------------------------

PASS=0
FAIL=0

# run <label> <expected rc: 0 | nz | N> <bash | bash-x> [VAR=value ...] -- <tf.sh args...>
# Optional checks after the run, through globals:
#   EXPECT_CALLS / FORBID_CALLS : extended regexps matched against the fake call log.
#   EXPECT_ARGS : exact arguments received by the fake terraform, one per line.
run() {
  local label="$1" exp="$2" launcher="$3" rc=0 ok=1 why=""
  shift 3
  local envs=()
  while [ "$1" != "--" ]; do envs+=("$1"); shift; done
  shift
  : > "$T/calls.log"
  : > "$T/args.txt"
  local cmd=(bash)
  [ "$launcher" = "bash-x" ] && cmd=(bash -x)
  ( cd "$T/tf" && env -i HOME="$T/home" PATH="$T/bin:/usr/bin:/bin" CALLS="$T/calls.log" ARGS="$T/args.txt" \
      ${envs[@]+"${envs[@]}"} "${cmd[@]}" ./tf.sh "$@" ) > "$T/out.txt" 2>&1 || rc=$?

  case "$exp" in
    0)  [ "$rc" -eq 0 ] || { ok=0; why+=" rc=$rc (expected 0)"; } ;;
    nz) [ "$rc" -ne 0 ] || { ok=0; why+=" rc=0 (expected non-zero)"; } ;;
    *)  [ "$rc" -eq "$exp" ] || { ok=0; why+=" rc=$rc (expected $exp)"; } ;;
  esac
  if grep -Eq "$LEAK_PATTERN" "$T/out.txt"; then ok=0; why+=" sentinel found in wrapper output"; fi
  if grep -q 'len=' "$T/out.txt"; then ok=0; why+=" length found in wrapper output"; fi
  if [ -n "${EXPECT_CALLS:-}" ] && ! grep -Eq "$EXPECT_CALLS" "$T/calls.log"; then
    ok=0; why+=" missing call /$EXPECT_CALLS/"
  fi
  if [ -n "${FORBID_CALLS:-}" ] && grep -Eq "$FORBID_CALLS" "$T/calls.log"; then
    ok=0; why+=" unexpected call /$FORBID_CALLS/"
  fi
  if [ -n "${EXPECT_ARGS:-}" ] && [ "$(cat "$T/args.txt")" != "$EXPECT_ARGS" ]; then
    ok=0; why+=" terraform arguments changed"
  fi
  EXPECT_CALLS="" FORBID_CALLS="" EXPECT_ARGS=""

  if [ "$ok" -eq 1 ]; then
    PASS=$((PASS + 1)); printf 'PASS  %s (rc=%s)\n' "$label" "$rc"
  else
    FAIL=$((FAIL + 1)); printf 'FAIL  %s:%s\n' "$label" "$why"
  fi
}

VARS_OK='terraform vars_ok=1'

# --- Cases -------------------------------------------------------------------

EXPECT_CALLS="$VARS_OK"; FORBID_CALLS='rsync'
run "plan, nominal"                          0 bash                         -- test/pve/vm plan
EXPECT_CALLS="$VARS_OK"
run "TFWRAP_DEBUG=1 is ignored"              0 bash TFWRAP_DEBUG=1          -- test/pve/vm plan
EXPECT_CALLS="$VARS_OK"
run "inherited trace: bash -x"               0 bash-x                       -- test/pve/vm plan
EXPECT_CALLS="$VARS_OK"
run "inherited trace: SHELLOPTS=xtrace"      0 bash SHELLOPTS=xtrace        -- test/pve/vm plan
EXPECT_CALLS="$VARS_OK"
run "tf.conf re-enables set -x"              0 bash                         -- test/pve/withconf plan
EXPECT_CALLS="bw unlock"
run "locked vault (bw unlock)"               0 bash FAKE_BW_STATUS=locked   -- test/pve/vm plan
EXPECT_CALLS="rsync"
run "bash -x + TFWRAP_DEBUG + locked, apply" 0 bash-x FAKE_BW_STATUS=locked TFWRAP_DEBUG=1 -- test/pve/vm apply
FORBID_CALLS='bws|terraform'
run "not logged in, no API key"              nz bash FAKE_BW_STATUS=unauthenticated -- test/pve/vm plan
EXPECT_CALLS="$VARS_OK"; EXPECT_ARGS=$'plan\n-var=x=SENTINEL_ARG_e2c9\n-out=SENTINEL_FILE_3d10'
run "sentinels in terraform arguments"       0 bash -- test/pve/vm plan -var=x=SENTINEL_ARG_e2c9 -out=SENTINEL_FILE_3d10
EXPECT_CALLS="$VARS_OK"; EXPECT_ARGS='sentinel-only-lower-case'
run "first terraform arg: lowercase sentinel" 0 bash -- test/pve/vm sentinel-only-lower-case
# Not an env directory: no cd, no ssh_authorized_keys; terraform is still called.
EXPECT_CALLS='terraform vars_ok='; EXPECT_ARGS=$'sentinel-not-an-env\nplan'
run "first wrapper arg: lowercase sentinel"  0 bash -- sentinel-not-an-env plan
FORBID_CALLS='bw|bws|terraform'
run "TF_LOG=DEBUG refused"                   nz bash TF_LOG=DEBUG           -- test/pve/vm plan
FORBID_CALLS='bw|bws|terraform'
run "TF_LOG_CORE=TRACE refused"              nz bash TF_LOG_CORE=TRACE      -- test/pve/vm plan
FORBID_CALLS='bw|bws|terraform'
run "TF_LOG_PROVIDER=DEBUG refused"          nz bash TF_LOG_PROVIDER=DEBUG  -- test/pve/vm plan
FORBID_CALLS='bw|bws|terraform'
run "TF_LOG value is not printed"            nz bash TF_LOG=SENTINEL_TFLOG_1c2d TF_LOG_PATH=/nonexistent -- test/pve/vm plan
FORBID_CALLS='bw|bws|terraform'
run "TF_LOG + TFWRAP_DEBUG + bash -x"        nz bash-x TF_LOG=DEBUG TFWRAP_DEBUG=1 -- test/pve/vm plan
EXPECT_CALLS="$VARS_OK"
run "empty TF_LOG is accepted"               0 bash TF_LOG=                 -- test/pve/vm plan
FORBID_CALLS='terraform'
run "empty secret stops before terraform"    nz bash FAKE_EMPTY_SECRET=11111111-1111-1111-1111-111111111111 -- test/pve/vm plan
EXPECT_CALLS="$VARS_OK"; FORBID_CALLS='rsync'
run "terraform fails in plan: rc kept"       3 bash FAKE_TF_RC=3            -- test/pve/vm plan
EXPECT_CALLS="$VARS_OK"
run "apply succeeds: variables passed"       0 bash                         -- test/pve/vm apply
EXPECT_CALLS="rsync"
run "apply succeeds: state copied"           0 bash                         -- test/pve/vm apply
FORBID_CALLS='rsync'
run "terraform fails in apply: no copy"      5 bash FAKE_TF_RC=5            -- test/pve/vm apply

echo
echo "passed: ${PASS}, failed: ${FAIL}"
[ "$FAIL" -eq 0 ]
