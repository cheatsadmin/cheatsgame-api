#!/bin/sh
set -eu

python manage.py migrate_with_advisory_lock
python manage.py migrate --check
