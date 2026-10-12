<#
.SYNOPSIS
    Sign, pack and publish a Row-Bot release on the Windows signing machine (D4).

.DESCRIPTION
    release.yml builds every release file in CI and leaves a draft GitHub release
    with all of them except the Windows installer: Row-Bot.exe must be signed
    before Inno Setup packs it, and the Certum certificate never leaves this
    machine. This script, with one SimplySign login:

      1. downloads the Windows installer stage the release run built
         (python\, the app, version.json, Row-Bot.exe and the Inno Setup script),
      2. signs Row-Bot.exe (signtool /tr /td sha256, Certum through SimplySign),
      3. runs ISCC on the stage, as CI did for the smoke-tested installer,
      4. signs the installer and verifies both signatures,
      5. uploads the installer to the draft release,
      6. checks that the release is complete: every installer and update part,
         the update description agreeing with them, the stapled macOS DMG, and
         the notes saying when an update needs the full installer (D16),
      7. writes the SHA-256 manifest block over all final files into the draft,
      8. publishes the draft, only after you type the confirmation.

    It refuses to publish while macOS is not stapled or a file is missing. Every
    step can be run again: signed files and a built installer are kept.

    -DryRun does everything except signing, uploading, editing the release and
    publishing; it reads the other files from the run's artifacts. release.yml
    runs it on Windows for every dry run.

    Prerequisites: Inno Setup 6 (ISCC.exe), the Windows SDK's signtool.exe,
    SimplySign Desktop logged in with the Certum code signing certificate, the
    GitHub CLI (gh auth login) with write access, and Python 3.11 or newer.

.EXAMPLE
    .\scripts\release_windows.ps1 -Version 5.2.0-beta.1

.EXAMPLE
    .\scripts\release_windows.ps1 -Version 5.2.0-beta.1 -RunId 38200000000 -DryRun
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Version,
    # The release.yml run that built the release (default: its newest successful run for the tag).
    [string]$RunId,
    [switch]$DryRun,
    [string]$WorkDir,
    [string]$Repository,
    # The Certum code signing certificate (its thumbprint is public).
    [string]$CertificateSha1 = "2341B4B36A21DF948E538A88BB194FAE4D1CAE51",
    [string]$TimestampUrl = "http://time.certum.pl",
    [string]$SignTool,
    [string]$Iscc
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3.0
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptRoot
$Tag = "v$Version"
$Utf8 = New-Object System.Text.UTF8Encoding $false

function Write-Step([string]$Text) { Write-Host ""; Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Note([string]$Text) { Write-Host "   $Text" }
function Stop-Release([string]$Text) { throw "Refusing to continue: $Text" }

# Native programs: their error output is never redirected (Windows PowerShell 5.1
# turns redirected error output into terminating errors); the exit code decides.
function Invoke-Native {
    # Runs a program with its output on the console; fails on a non-zero exit code.
    param([Parameter(Mandatory = $true)][string]$File, [string[]]$Arguments = @())
    & $File @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "$([IO.Path]::GetFileName($File)) failed with exit code $LASTEXITCODE" }
}

function Get-NativeOutput {
    # Runs a program and returns its standard output; fails on a non-zero exit code.
    param([Parameter(Mandatory = $true)][string]$File, [string[]]$Arguments = @())
    $output = & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$([IO.Path]::GetFileName($File)) failed with exit code $LASTEXITCODE" }
    return $output
}

function Find-Tool([string]$Given, [string[]]$Candidates, [string]$Name) {
    if ($Given) { if (Test-Path $Given) { return (Resolve-Path $Given).Path }; throw "$Name not found at $Given" }
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path $candidate)) { return (Resolve-Path $candidate).Path }
    }
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Find-SignTool {
    $kits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    $found = @()
    if (Test-Path $kits) {
        $found = @(Get-ChildItem -Path $kits -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^\d+\.' } | Sort-Object { [version]$_.Name } -Descending |
            ForEach-Object { Join-Path $_.FullName "x64\signtool.exe" })
    }
    return Find-Tool $SignTool $found "signtool.exe"
}

function Find-Python {
    foreach ($candidate in @(@("python"), @("py", "-3"))) {
        $command = Get-Command $candidate[0] -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $prefix = @($candidate | Select-Object -Skip 1)
        $versionText = & $command.Source @prefix -c "import sys; print('%d.%d' % sys.version_info[:2])"
        if ($LASTEXITCODE -eq 0 -and $versionText -and ([version]$versionText -ge [version]"3.11")) {
            return @{ File = $command.Source; Prefix = $prefix }
        }
    }
    throw "Python 3.11 or newer is required (python or py -3 on PATH)"
}

function Invoke-Python([string[]]$Arguments) {
    return Get-NativeOutput -File $Python.File -Arguments (@($Python.Prefix) + @("-X", "utf8") + $Arguments)
}

function Get-Sha256([string]$Path) { return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() }

function Write-Json([string]$Path, $Value) {
    [IO.File]::WriteAllText($Path, (ConvertTo-Json -InputObject $Value -Depth 5), $Utf8)
}

function Save-Artifact([string]$Name, [string]$Into, [switch]$Optional) {
    # One artifact of the release run, into its own folder ($null if optional and missing).
    if (Test-Path $Into) { Remove-Item -LiteralPath $Into -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $Into | Out-Null
    if ($Optional) {
        $names = Get-NativeOutput -File $Gh -Arguments @("api", "repos/$Repository/actions/runs/$RunId/artifacts?per_page=100",
                 "--jq", ".artifacts[] | select(.expired | not) | .name")
        if (-not (@($names) -contains $Name)) { return $null }
    }
    Invoke-Native -File $Gh -Arguments @("run", "download", $RunId, "-R", $Repository, "-n", $Name, "-D", $Into)
    return $Into
}

function Test-OurSignature([string]$Path) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    return ($signature.Status -eq "Valid" -and $signature.SignerCertificate -and
            $signature.SignerCertificate.Thumbprint -eq $CertificateSha1.ToUpperInvariant())
}

function Assert-SigningReady {
    # SimplySign Desktop exposes the Certum certificate while you are logged in:
    # log in once, before the first signature, and both files are signed in that session.
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $certificate = Get-ChildItem -Path "Cert:\CurrentUser\My\$CertificateSha1" -ErrorAction SilentlyContinue
        if ($certificate -and $certificate.HasPrivateKey) { return }
        Write-Host "The Certum certificate $CertificateSha1 is not available." -ForegroundColor Yellow
        Read-Host "Log in to SimplySign Desktop (the code from your phone), then press Enter" | Out-Null
    }
    throw "The Certum certificate is still not available; is SimplySign Desktop logged in?"
}

function Invoke-Sign([string]$Path) {
    if (Test-OurSignature $Path) { Write-Note "$([IO.Path]::GetFileName($Path)) is already signed"; return }
    Invoke-Native -File $SignToolPath -Arguments @(
        "sign", "/sha1", $CertificateSha1, "/fd", "SHA256", "/tr", $TimestampUrl, "/td", "SHA256",
        "/d", "Row-Bot", "/du", "https://row-bot.ai", $Path)
}

function Assert-Signed([string]$Path) {
    Get-NativeOutput -File $SignToolPath -Arguments @("verify", "/pa", "/v", $Path) | Out-Null
    if (-not (Test-OurSignature $Path)) { throw "$Path is not signed with $CertificateSha1" }
    Write-Note "$([IO.Path]::GetFileName($Path)): signature verified"
}

# ── Preflight ───────────────────────────────────────────────────────────────

if ($Version -notmatch '^\d+\.\d+\.\d+(-(alpha|beta|rc)\.\d+)?$') { throw "Version must look like 5.2.0 or 5.2.0-beta.1" }
$Gh = Find-Tool $null @() "gh"
if (-not $Gh) { throw "The GitHub CLI (gh) is required: https://cli.github.com, then gh auth login" }
if (-not $Repository) { $Repository = if ($env:GH_REPO) { $env:GH_REPO } else { "siddsachar/row-bot" } }
$Python = Find-Python
$IsccPath = Find-Tool $Iscc @("C:\Program Files (x86)\Inno Setup 6\ISCC.exe", "C:\Program Files\Inno Setup 6\ISCC.exe") "iscc.exe"
if (-not $IsccPath) { throw "Inno Setup 6 (ISCC.exe) is required: https://jrsoftware.org/isdl.php" }
$Tar = Join-Path $env:SystemRoot "System32\tar.exe"
$SignToolPath = $null
if (-not $DryRun) {
    $SignToolPath = Find-SignTool
    if (-not $SignToolPath) { throw "signtool.exe is required (Windows SDK, Signing Tools for Desktop Apps)" }
}
if (-not $WorkDir) {
    $base = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { Join-Path $env:LOCALAPPDATA "Row-Bot-Release" }
    $WorkDir = Join-Path $base $Version
}
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$WorkDir = (Resolve-Path $WorkDir).Path

Write-Step "Row-Bot $Version$(if ($DryRun) { ' (dry run: no signing, no upload, no publishing)' })"
Write-Note "Repository: $Repository"
Write-Note "Work folder: $WorkDir"

# ── The release run ─────────────────────────────────────────────────────────

Write-Step "Release run"
$title = if ($DryRun) { "Release $Version (dry run)" } else { "Release $Version" }
if (-not $RunId) {
    $listArgs = @("run", "list", "-R", $Repository, "--workflow", "release.yml", "--status", "success", "--limit", "50",
                  "--json", "databaseId,displayTitle,headBranch")
    $runs = (Get-NativeOutput -File $Gh -Arguments $listArgs) -join "`n" | ConvertFrom-Json
    $match = @($runs | Where-Object { $_.displayTitle -eq $title -and ($DryRun -or $_.headBranch -eq $Tag) }) | Select-Object -First 1
    if (-not $match) { throw "No successful release.yml run named '$title'$(if (-not $DryRun) { " from $Tag" }); pass -RunId" }
    $RunId = [string]$match.databaseId
}
$run = (Get-NativeOutput -File $Gh -Arguments @("run", "view", $RunId, "-R", $Repository, "--json",
        "displayTitle,headBranch,headSha,status,conclusion,url")) -join "`n" | ConvertFrom-Json
Write-Note "$($run.displayTitle) - $($run.url)"
if ($run.displayTitle -ne $title) { Stop-Release "run $RunId is '$($run.displayTitle)', not '$title'" }
if (-not $DryRun) {
    if ($run.headBranch -ne $Tag) { Stop-Release "run $RunId built $($run.headBranch), not the tag $Tag" }
    if ($run.conclusion -ne "success") { Stop-Release "run $RunId did not succeed ($($run.status) $($run.conclusion))" }
}

# ── 1. The installer stage ──────────────────────────────────────────────────

Write-Step "1. The Windows installer stage"
$Stage = Join-Path $WorkDir "stage"
$IssFile = Join-Path $Stage "installer\row_bot_setup.iss"
if (-not (Test-Path $IssFile)) {
    $download = Save-Artifact "Row-Bot-Windows-stage" (Join-Path $WorkDir "stage-download")
    $tarFile = Get-ChildItem -Path $download -Filter "*.tar" | Select-Object -First 1
    if (-not $tarFile) { throw "The Row-Bot-Windows-stage artifact holds no .tar" }
    New-Item -ItemType Directory -Force -Path $Stage | Out-Null
    Invoke-Native -File $Tar -Arguments @("-xf", $tarFile.FullName, "-C", $Stage)
    Remove-Item -LiteralPath $download -Recurse -Force
}
$stageVersion = (Get-Content (Join-Path $Stage "installer\build\version.json") -Raw | ConvertFrom-Json).version
$issVersion = (Select-String -Path $IssFile -Pattern '#define\s+MyAppVersion\s+"([^"]+)"').Matches[0].Groups[1].Value
if ($stageVersion -ne $Version -or $issVersion -ne $Version) { Stop-Release "the stage is $stageVersion (setup script $issVersion), not $Version" }
$ciInno = (Get-Content (Join-Path $Stage "installer\build\inno-version.txt") -Raw).Trim()
$localInno = (Get-Item $IsccPath).VersionInfo.ProductVersion
if ($ciInno -ne $localInno) { Write-Host "   Inno Setup here is $localInno; CI built the smoke-tested installer with $ciInno" -ForegroundColor Yellow }
$Launcher = Join-Path $Stage "installer\build\Row-Bot.exe"
Write-Note "Stage of $Version ready ($Stage)"

# ── 2-4. Sign Row-Bot.exe, pack, sign the installer ─────────────────────────

$Installer = Join-Path $Stage "dist\Row-Bot-$Version-Windows-x64.exe"
if ($DryRun) {
    Write-Step "2. Row-Bot.exe: not signed (dry run)"
} else {
    Write-Step "2. Sign Row-Bot.exe"
    Assert-SigningReady
    Invoke-Sign $Launcher
    Assert-Signed $Launcher
}

Write-Step "3. Pack the installer (ISCC)"
$packed = (Test-Path $Installer) -and ((Get-Item $Installer).LastWriteTimeUtc -gt (Get-Item $Launcher).LastWriteTimeUtc)
if ($packed) {
    Write-Note "$([IO.Path]::GetFileName($Installer)) is newer than Row-Bot.exe; kept"
} else {
    Invoke-Native -File $IsccPath -Arguments @("/Qp", "/DAppStageDir=$(Join-Path $Stage 'installer\build\app')", $IssFile)
}
if (-not (Test-Path $Installer)) { throw "ISCC did not write $Installer" }

if ($DryRun) {
    Write-Step "4. The installer: not signed (dry run)"
} else {
    Write-Step "4. Sign the installer and verify both signatures"
    Invoke-Sign $Installer
    Assert-Signed $Launcher
    Assert-Signed $Installer
}

# ── 5-6. The other release files and the checks ─────────────────────────────

Write-Step "5. The release's other files"
$Files = Join-Path $WorkDir "files"
New-Item -ItemType Directory -Force -Path $Files | Out-Null
$parts = Save-Artifact "Row-Bot-update-parts" (Join-Path $WorkDir "update-parts")
$checksums = Save-Artifact "Row-Bot-release-sha256" (Join-Path $WorkDir "checksums")
$ciHashes = @{}
foreach ($line in Get-Content (Join-Path $checksums "release-artifact-sha256.txt")) {
    if ($line -match '^([0-9a-f]{64})\s+\*?(.+)$') { $ciHashes[$Matches[2].Trim()] = $Matches[1] }
}
$UpdateName = "row-bot-update-$Version.json"
$listing = New-Object System.Collections.ArrayList
$body = ""
$macosReady = $false

if ($DryRun) {
    foreach ($name in @("Row-Bot-Linux", "Row-Bot-macOS", "Row-Bot-update")) {
        $folder = Save-Artifact $name (Join-Path $WorkDir "artifact-$name") -Optional
        if ($folder) { Get-ChildItem -Path $folder -File | Copy-Item -Destination $Files -Force }
    }
    Get-ChildItem -Path $parts -File | Where-Object { $_.Name -match '^row-bot-(code|assets|pylock)-' } | Copy-Item -Destination $Files -Force
    foreach ($file in Get-ChildItem -Path $Files -File) {
        [void]$listing.Add([ordered]@{ name = $file.Name; size = $file.Length; sha256 = (Get-Sha256 $file.FullName) })
    }
    $notarization = Save-Artifact "Row-Bot-macOS-notarization" (Join-Path $WorkDir "notarization")
    $readiness = Invoke-Python @("-c", "import json, sys; sys.path.insert(0, sys.argv[2]); import macos_notarize as m; print(m.readiness(json.load(open(sys.argv[1]))))",
        (Join-Path $notarization "notarization.json"), $ScriptRoot)
    $readiness = ($readiness | Select-Object -Last 1).Trim()
    $macosReady = $readiness -in @("ready", "app-only")
    Write-Note "macOS notarization: $readiness$(if ($readiness -eq 'app-only') { ' (a dry run notarizes the app only)' })"
    Invoke-Python @((Join-Path $ScriptRoot "build_update_parts.py"), "release-notes", "--version", $Version,
        "--notes", (Join-Path $ProjectRoot "RELEASE_NOTES.md"), "--wheel-check", (Join-Path $parts "wheel-check.json"),
        "--title-out", (Join-Path $WorkDir "title.txt"), "--body-out", (Join-Path $WorkDir "body.md")) | Out-Null
    $body = [IO.File]::ReadAllText((Join-Path $WorkDir "body.md"))
    $updateJson = Join-Path $Files $UpdateName
} else {
    $releaseId = (Get-NativeOutput -File $Gh -Arguments @("api", "repos/$Repository/releases?per_page=100", "--paginate",
                  "--jq", ".[] | select(.tag_name == `"$Tag`") | .id") | Select-Object -First 1)
    if (-not $releaseId) { Stop-Release "there is no draft release $Tag (release.yml creates it)" }
    $release = (Get-NativeOutput -File $Gh -Arguments @("api", "repos/$Repository/releases/$releaseId")) -join "`n" | ConvertFrom-Json
    if (-not $release.draft) { Stop-Release "$Tag is already published; a published release is never changed" }
    $body = [string]$release.body
    foreach ($asset in $release.assets) {
        if ($asset.name -eq [IO.Path]::GetFileName($Installer)) { continue }  # replaced by the installer built here
        $digest = if ($asset.PSObject.Properties["digest"] -and $asset.digest -match '^sha256:([0-9a-f]{64})$') { $Matches[1] } else { $null }
        if (-not $digest -or $asset.name -eq $UpdateName) {
            Invoke-Native -File $Gh -Arguments @("release", "download", $Tag, "-R", $Repository, "-p", $asset.name, "-D", $Files, "--clobber")
            $local = Get-Sha256 (Join-Path $Files $asset.name)
            if ($digest -and $digest -ne $local) { Stop-Release "$($asset.name) does not match GitHub's digest" }
            $digest = $local
        }
        if ($ciHashes.ContainsKey($asset.name) -and $ciHashes[$asset.name] -ne $digest) {
            Stop-Release "$($asset.name) on the draft is not the file release run $RunId built"
        }
        [void]$listing.Add([ordered]@{ name = $asset.name; size = [long]$asset.size; sha256 = $digest })
    }
    # Only a stapled DMG is ever attached (release.yml or notarize-finish.yml).
    $macosReady = [bool]($release.assets | Where-Object { $_.name -eq "Row-Bot-$Version-macOS-arm64.dmg" })
    Write-Note "macOS: $(if ($macosReady) { 'the stapled DMG is on the draft' } else { 'no stapled DMG on the draft yet (see notarize-finish.yml)' })"
    $updateJson = Join-Path $Files $UpdateName
}
$installerHash = Get-Sha256 $Installer
[void]$listing.Add([ordered]@{ name = [IO.Path]::GetFileName($Installer); size = (Get-Item $Installer).Length; sha256 = $installerHash })
$ListingFile = Join-Path $WorkDir "listing.json"
Write-Json $ListingFile @($listing)

Write-Step "6. Check the release"
$problems = @()
if (-not (Test-Path $updateJson)) { $problems += "$UpdateName is missing" } else {
    & $Python.File @($Python.Prefix) -X utf8 (Join-Path $ScriptRoot "build_update_parts.py") verify-release `
        --version $Version --listing $ListingFile --update-json $updateJson
    if ($LASTEXITCODE -ne 0) { $problems += "the release files are incomplete or disagree with the update description" }
}
if (-not $macosReady) { $problems += "macOS is not stapled yet" }
$wheelCheck = Get-Content (Join-Path $parts "wheel-check.json") -Raw | ConvertFrom-Json
if ($wheelCheck.needs_full_installer -and $body -notmatch "(?i)this update needs the full installer") {
    $problems += "a changed library has no wheel, so the notes must say 'This update needs the full installer'"
}
foreach ($item in $listing) { Write-Note ("{0,-46} {1,14:N0}  {2}" -f $item.name, $item.size, $item.sha256) }
if ($problems.Count -gt 0) {
    foreach ($problem in $problems) { Write-Host "   - $problem" -ForegroundColor Red }
    Stop-Release "the release is not ready to publish"
}
Write-Note "Every file is present and agrees with the update description."

# ── 5 (upload) and 7. The installer and the SHA-256 block ───────────────────

$BodyFile = Join-Path $WorkDir "body-current.md"
$NewBody = Join-Path $WorkDir "body-with-manifest.md"
[IO.File]::WriteAllText($BodyFile, $body, $Utf8)
Invoke-Python @((Join-Path $ScriptRoot "append_sha_manifest.py"), "--listing", $ListingFile, "--body-file", $BodyFile, "--write-body", $NewBody) | Out-Null

if ($DryRun) {
    Write-Step "7. The SHA-256 block (dry run: not written to a release)"
    $text = [IO.File]::ReadAllText($NewBody)
    Write-Host $text.Substring($text.IndexOf("<!-- row-bot-update-manifest -->"))
    Write-Step "Dry run complete: $Installer built, $($listing.Count) release files checked, nothing signed or published"
    exit 0
}

Write-Step "5. Upload the installer to the draft"
Invoke-Native -File $Gh -Arguments @("release", "upload", $Tag, $Installer, "-R", $Repository, "--clobber")
$uploaded = (Get-NativeOutput -File $Gh -Arguments @("api", "repos/$Repository/releases/$releaseId/assets?per_page=100",
             "--jq", ".[] | select(.name == `"$([IO.Path]::GetFileName($Installer))`") | .digest") | Select-Object -First 1)
if ($uploaded -and $uploaded -ne "sha256:$installerHash") { Stop-Release "the uploaded installer does not match the signed one" }

Write-Step "7. Write the SHA-256 block into the draft"
Invoke-Native -File $Gh -Arguments @("release", "edit", $Tag, "-R", $Repository, "--notes-file", $NewBody)
Write-Note "The block lists all $($listing.Count) files."

# ── 8. Publish ──────────────────────────────────────────────────────────────

Write-Step "8. Publish $Tag"
$prerelease = $Version -match '-'
Write-Host "   Publishing makes $Tag the $(if ($prerelease) { 'newest beta' } else { 'latest release' }): Row-Bot" -ForegroundColor Yellow
Write-Host "   installs then offer it and download it. A published release is never changed." -ForegroundColor Yellow
$answer = Read-Host "   Type 'publish $Version' to publish now (anything else keeps the draft)"
if ($answer -ne "publish $Version") {
    Write-Note "Not published. The draft is complete; run this script again to publish it."
    exit 0
}
$publish = @("release", "edit", $Tag, "-R", $Repository, "--draft=false")
if ($prerelease) { $publish += @("--prerelease", "--latest=false") } else { $publish += "--latest" }
Invoke-Native -File $Gh -Arguments $publish
Write-Step "Published $Tag"
Write-Note "update-manifest.yml now checks the block (it never overwrites it); container.yml builds the images"
Write-Note "and adds row-bot-container-$Version.json to the block."
