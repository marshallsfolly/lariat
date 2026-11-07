# The method names in the herdr package's own API schema. The gateway only
# ever calls a fixed allowlist; this lets it disable any operation the pinned
# herdr does not have instead of failing at runtime.
{
  runCommand,
  jq,
  herdr,
}:
runCommand "herdr-methods.json" { nativeBuildInputs = [ jq ]; } ''
  export HOME=$TMPDIR
  ${herdr}/bin/herdr api schema --json \
    | jq -c '[.schemas.request.oneOf[].properties.method.const]' > $out
  test "$(jq length $out)" -gt 10
''
