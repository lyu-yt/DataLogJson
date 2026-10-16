#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
conda run --no-capture-output -n datalogjson python -m experiment_logger
