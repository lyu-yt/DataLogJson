# Run from any directory in a Conda-enabled PowerShell session.
Set-Location -LiteralPath $PSScriptRoot
conda run --no-capture-output -n datalogjson python -m experiment_logger
