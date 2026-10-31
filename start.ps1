# Always launch with the repository's locked Python environment.
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython -PathType Leaf)) {
    throw 'Projektets .venv mangler. Se installationen i README.md.'
}

Push-Location -LiteralPath $PSScriptRoot
try {
    & $projectPython -X utf8 -m streamlit run '.\website\server.py' @args
    $appExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $appExitCode
