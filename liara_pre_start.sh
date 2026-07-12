#!/usr/bin/env bash
set -Eeuo pipefail

python manage.py migrate_with_advisory_lock
python manage.py migrate --check
