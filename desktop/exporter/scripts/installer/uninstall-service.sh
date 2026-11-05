#!/bin/sh
set -eu
[ "$(/usr/bin/id -u)" = 0 ] || { echo 'Run through an administrator-controlled terminal' >&2; exit 1; }
/bin/launchctl bootout system/dev.alpen.openaria-exporter 2>/dev/null || true
/bin/rm -f /Library/LaunchDaemons/dev.alpen.openaria-exporter.plist \
 /Library/PrivilegedHelperTools/dev.alpen.openaria-exporter.helper \
 /Library/PrivilegedHelperTools/dev.alpen.openaria-exporter.uid \
 /var/run/dev.alpen.openaria-exporter.sock \
 /Library/Logs/OpenAriaExporter/service.log \
 /Library/Logs/OpenAriaExporter/service.log.1
/bin/rmdir /Library/Logs/OpenAriaExporter 2>/dev/null || true
# Deliberately preserve the App, user exports and user logs.
