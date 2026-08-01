#!/bin/sh
set -eu

tracker db upgrade
exec "$@"
