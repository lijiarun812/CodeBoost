$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath 'D:\CodeBoost'
& 'D:\Python\Python 3.14.2\python.exe' 'D:\CodeBoost\experiment\run_effibench_v2.py' @args
exit $LASTEXITCODE
