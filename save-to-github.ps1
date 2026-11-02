$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$githubCli = Join-Path $PSScriptRoot 'downloads\github-cli\bin\gh.exe'
$gitRoot = $PSScriptRoot.Replace('\', '/')

function Invoke-GitChecked {
    param([string[]]$Arguments)
    & git -c "safe.directory=$gitRoot" @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Git failed. Nothing will be force-pushed.' }
}

try {
    & $githubCli auth status
    if ($LASTEXITCODE -ne 0) { throw 'GitHub login is unavailable in this Windows session. Run login-github.cmd first.' }
    $accountJson = & $githubCli api user
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read the authenticated GitHub account.' }
    $account = $accountJson | ConvertFrom-Json
    if ($account.login -ne 'lverl') { throw "Expected GitHub account lverl; found $($account.login). No upload performed." }
    $repository = "$($account.login)/skin-research-intelligence"
    $remoteUrl = "https://github.com/$repository.git"

    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.git'))) {
        Invoke-GitChecked -Arguments @('init', '--initial-branch=main')
    }
    $branch = & git -c "safe.directory=$gitRoot" branch --show-current
    if ($LASTEXITCODE -ne 0 -or $branch -ne 'main') { throw 'Expected the local main branch. Stopped without changing branches.' }
    $remoteNames = & git -c "safe.directory=$gitRoot" remote
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect Git remotes.' }
    $existingRemote = $null
    if ($remoteNames -contains 'origin') {
        $existingRemote = & git -c "safe.directory=$gitRoot" remote get-url origin
        if ($LASTEXITCODE -ne 0) { throw 'Cannot read origin.' }
    }
    if ($existingRemote -and $existingRemote.TrimEnd('/') -ne $remoteUrl -and $existingRemote -ne "git@github.com:$repository.git") {
        throw 'An origin for a different repository already exists. It was not changed.'
    }

    $authorName = & git -c "safe.directory=$gitRoot" config --get user.name
    if (-not $authorName) { Invoke-GitChecked -Arguments @('config', 'user.name', $account.login) }
    $authorEmail = & git -c "safe.directory=$gitRoot" config --get user.email
    if (-not $authorEmail) {
        Invoke-GitChecked -Arguments @('config', 'user.email', "$($account.id)+$($account.login)@users.noreply.github.com")
    }

    $projectFiles = @('.dockerignore','.env.example','.gitignore','Dockerfile','PRD.md','README.md','STATUS.md',
        'bot','collector','db','digest.py','docker-compose.yml','docker-compose.local.yml','docker-compose.web.yml','docs','evidence.py','llm.py','main.py','net.py',
        'requirements.txt','research.py','skin_research_sources_30.md','sources.json','tests','web','worker',
        'login-github.cmd','save-to-github.cmd','save-to-github.ps1')
    Invoke-GitChecked -Arguments (@('add','--') + $projectFiles)
    $staged = & git -c "safe.directory=$gitRoot" diff --cached --name-only
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect staged files.' }
    foreach ($entry in $staged) {
        if ($entry -match '(^|/)(\.env|\.venv|\.local-postgres|downloads|upstream|\.codex)(/|$)') {
            throw "A local or secret file is staged: $entry. Upload stopped."
        }
    }
    Invoke-GitChecked -Arguments @('diff','--cached','--check')
    Invoke-GitChecked -Arguments @('diff','--cached','--stat')
    if ($staged) { Invoke-GitChecked -Arguments @('commit','-m','Build Skin Research Intelligence with daily digests and evidence tracking') }

    # List only repositories owned by this account. An existing repo is never overwritten.
    $repositoryJson = & $githubCli repo list $account.login --limit 1000 --json name,isPrivate
    if ($LASTEXITCODE -ne 0) { throw 'Cannot check existing GitHub repositories.' }
    $existing = @($repositoryJson | ConvertFrom-Json) | Where-Object { $_.name -ieq 'skin-research-intelligence' }
    if ($existing -and -not $existing.isPrivate) { throw 'skin-research-intelligence already exists and is public. Upload stopped.' }
    if (-not $existing) {
        & $githubCli repo create $repository --private --description 'Skin Research Intelligence: evidence-based skincare research and daily digests'
        if ($LASTEXITCODE -ne 0) { throw 'Cannot create the private repository.' }
    }
    if (-not $existingRemote) { Invoke-GitChecked -Arguments @('remote','add','origin',$remoteUrl) }
    # Use the authorized GitHub CLI for HTTPS credentials without printing or copying tokens.
    $credentialHelper = '!' + '"' + $githubCli.Replace('\','/') + '" auth git-credential'
    Invoke-GitChecked -Arguments @('-c','http.sslBackend=openssl','-c','credential.helper=',
        '-c',"credential.helper=$credentialHelper",'push','-u','origin','main')
    Write-Host "Saved successfully: https://github.com/$repository" -ForegroundColor Green
    exit 0
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
