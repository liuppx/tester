#!/usr/bin/env bash
# Run browsers without access to the host's GPU devices or sysfs discovery paths.
set -euo pipefail

if (( $# == 0 )); then
    echo 'Usage: browser-runtime.sh command [args...]' >&2
    exit 2
fi

export TESTER_CPU_BROWSER=1
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe

if [[ "$(uname -s)" != Linux ]]; then
    exec "$@"
fi

if ! command -v bwrap >/dev/null 2>&1; then
    echo 'Tester browser isolation requires Bubblewrap on Linux; browser not started.' >&2
    exit 3
fi

display_command=()
if [[ -z "${DISPLAY:-}" ]]; then
    if ! command -v xvfb-run >/dev/null 2>&1; then
        echo 'Tester browser isolation requires Xvfb when DISPLAY is unset.' >&2
        exit 3
    fi
    display_command=(xvfb-run -a -s '-screen 0 1920x1080x24')
fi

# Mask all of /sys, including /sys/devices: hiding only the PCI/DRM symlinks
# still lets libudev read the real device's uevent and block in the kernel.
# --dev creates a minimal /dev with no /dev/dri or /dev/nvidia* passthrough.
# Network and checkout paths stay available for real E2E and MV3 extensions.
exec "${display_command[@]}" bwrap --die-with-parent \
    --bind / / --dev /dev --proc /proc \
    --tmpfs /sys --remount-ro /sys \
    --tmpfs /run/udev \
    -- /bin/bash -c '
        if [[ -n "$(ls -A /sys)" || -e /dev/dri || -e /dev/nvidiactl ]]; then
            echo "Tester browser isolation check failed; browser not started." >&2
            exit 3
        fi
        exec "$@"
    ' tester-browser-runtime "$@"
