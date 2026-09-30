$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
# Load env vars
$dotenv = Get-Content "$PSScriptRoot\.env" | Out-String
foreach ($line in ($dotenv -split "`n")) {
    if ($line -match '^DEEPSEEK_API_KEY=(.+)') {
        $env:DEEPSEEK_API_KEY = $Matches[1].Trim()
    }
    if ($line -match '^DEEPSEEK_BASE_URL=(.+)') {
        $env:DEEPSEEK_BASE_URL = $Matches[1].Trim()
    }
}
cd $PSScriptRoot
uv run langgraph dev --host 127.0.0.1 --port 2025
