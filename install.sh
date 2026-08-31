#!/bin/sh

set -eu

repository="${OPENARIA_BRIDGE_REPOSITORY:-Alpenl/openaria-bridge-sdk}"
release_root="${OPENARIA_BRIDGE_RELEASE_ROOT:-https://github.com/${repository}/releases/latest/download}"
uv_install_url="${OPENARIA_BRIDGE_UV_INSTALL_URL:-https://astral.sh/uv/install.sh}"
python_version="${OPENARIA_BRIDGE_PYTHON:-3.13}"

say() {
    printf '%s\n' "$*"
}

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

download() {
    source_url="$1"
    destination="$2"

    case "$source_url" in
        https://*)
            curl --proto '=https' --tlsv1.2 -LsSf \
                --retry 3 --retry-delay 1 --connect-timeout 20 \
                "$source_url" -o "$destination"
            ;;
        file://*)
            [ "${OPENARIA_BRIDGE_ALLOW_FILE_URLS:-0}" = "1" ] || \
                fail "refusing non-HTTPS download URL: $source_url"
            curl -LsSf "$source_url" -o "$destination"
            ;;
        *)
            fail "refusing non-HTTPS download URL: $source_url"
            ;;
    esac
}

sha256_file() {
    target_file="$1"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$target_file" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$target_file" | awk '{print $1}'
    else
        fail "sha256sum or shasum is required"
    fi
}

verify_sha256() {
    expected_hash=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
    target_file="$2"
    asset_name="$3"

    [ "${#expected_hash}" -eq 64 ] || fail "invalid SHA-256 for $asset_name"
    case "$expected_hash" in
        *[!0-9a-f]*) fail "invalid SHA-256 for $asset_name" ;;
    esac

    actual_hash=$(sha256_file "$target_file" | tr '[:upper:]' '[:lower:]')
    [ "$actual_hash" = "$expected_hash" ] || \
        fail "checksum mismatch for $asset_name"
}

case "$(uname -s)" in
    Linux|Darwin) ;;
    *) fail "Open Aria Bridge currently supports Linux and macOS" ;;
esac

command -v curl >/dev/null 2>&1 || fail "curl is required"
[ -n "${HOME:-}" ] || fail "HOME is required"

tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/openaria-bridge.XXXXXX" 2>/dev/null || \
    mktemp -d -t openaria-bridge)
cleanup() {
    rm -rf -- "$tmp_dir"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

release_root=${release_root%/}
checksums="$tmp_dir/SHA256SUMS"
say "Downloading Open Aria Bridge release metadata..."
download "$release_root/SHA256SUMS" "$checksums"

wheel_records=$(awk '
    $2 ~ /^ylx_card_pipeline-[0-9][0-9A-Za-z._+]*-py3-none-any\.whl$/ {
        print $1, $2
    }
' "$checksums")
wheel_count=$(printf '%s\n' "$wheel_records" | awk 'NF { count++ } END { print count + 0 }')
[ "$wheel_count" -eq 1 ] || \
    fail "SHA256SUMS must contain exactly one Open Aria Bridge wheel"
wheel_hash=$(printf '%s\n' "$wheel_records" | awk '{ print $1 }')
wheel_name=$(printf '%s\n' "$wheel_records" | awk '{ print $2 }')

constraint_records=$(awk '$2 == "constraints.txt" { print $1, $2 }' "$checksums")
constraint_count=$(printf '%s\n' "$constraint_records" | awk 'NF { count++ } END { print count + 0 }')
[ "$constraint_count" -eq 1 ] || \
    fail "SHA256SUMS must contain exactly one constraints.txt"
constraints_hash=$(printf '%s\n' "$constraint_records" | awk '{ print $1 }')
constraints_name=$(printf '%s\n' "$constraint_records" | awk '{ print $2 }')

wheel="$tmp_dir/$wheel_name"
constraints="$tmp_dir/$constraints_name"
say "Downloading $wheel_name..."
download "$release_root/$wheel_name" "$wheel"
download "$release_root/$constraints_name" "$constraints"
verify_sha256 "$wheel_hash" "$wheel" "$wheel_name"
verify_sha256 "$constraints_hash" "$constraints" "$constraints_name"
say "Release checksums verified."

if [ -n "${OPENARIA_BRIDGE_UV_BIN:-}" ]; then
    uv_bin="$OPENARIA_BRIDGE_UV_BIN"
elif command -v uv >/dev/null 2>&1; then
    uv_bin=$(command -v uv)
elif [ -x "$HOME/.local/bin/uv" ]; then
    uv_bin="$HOME/.local/bin/uv"
else
    uv_install_dir="${OPENARIA_BRIDGE_UV_INSTALL_DIR:-$HOME/.local/bin}"
    uv_installer="$tmp_dir/uv-install.sh"
    say "Installing uv..."
    download "$uv_install_url" "$uv_installer"
    mkdir -p "$uv_install_dir"
    UV_UNMANAGED_INSTALL="$uv_install_dir" UV_NO_MODIFY_PATH=1 sh "$uv_installer"
    uv_bin="$uv_install_dir/uv"
fi

[ -x "$uv_bin" ] || fail "uv executable not found: $uv_bin"

bin_dir="${OPENARIA_BRIDGE_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$bin_dir"
say "Installing Open Aria Bridge with Python $python_version..."
UV_TOOL_BIN_DIR="$bin_dir" "$uv_bin" --no-config tool install \
    --python "$python_version" \
    --force \
    --constraints "$constraints" \
    "$wheel"

bridge="$bin_dir/openaria-bridge"
[ -x "$bridge" ] || fail "installation completed without creating $bridge"
installed_version=$($bridge --version)
say "$installed_version installed successfully."

case ":${PATH:-}:" in
    *":$bin_dir:"*) say "Run: openaria-bridge" ;;
    *)
        say "Add $bin_dir to PATH, then run: openaria-bridge"
        say "Run now: $bridge"
        ;;
esac
