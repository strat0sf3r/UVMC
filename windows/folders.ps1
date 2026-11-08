# The "Start UVMC" window: choose which folders the agent can see this time.
# Shown by the UVMC Panel shortcut before the panel opens. It only edits shares.txt;
# the sharing itself is done by share.ps1 afterwards.
#   -Force   show the window even when "Don't ask again" is set
# Exit code 0 = start, 1 = cancelled.
param([switch]$Force)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName Microsoft.VisualBasic

$Repo = Split-Path -Parent $PSScriptRoot
$List = Join-Path $Repo 'shares.txt'
if (-not (Test-Path $List)) { Copy-Item (Join-Path $Repo 'shares.example.txt') $List -ErrorAction SilentlyContinue }
$Skip = Join-Path $Repo '.uvmc-skip-folder-window'

$shift = ([System.Windows.Forms.Control]::ModifierKeys -band [System.Windows.Forms.Keys]::Shift) -ne 0
if ((Test-Path $Skip) -and -not $Force -and -not $shift) { exit 0 }

# --- read shares.txt: comment header, then entries (on, or "#off" = remembered) -------
$header = New-Object System.Collections.Generic.List[string]
$entries = New-Object System.Collections.Generic.List[object]
$inHeader = $true
if (Test-Path $List) {
    foreach ($raw in Get-Content $List) {
        $line = $raw.Trim()
        $on = $true
        if ($line -match '^#off\s+(.*)$') { $line = $Matches[1]; $on = $false }
        elseif ($line.StartsWith('#') -or -not $line) {
            if ($inHeader) { $header.Add($raw) }
            continue
        }
        $parts = $line.Split('=', 2)
        if ($parts.Count -ne 2) { continue }
        $inHeader = $false
        $entries.Add([pscustomobject]@{ Name = $parts[0].Trim(); Path = $parts[1].Trim(); On = $on })
    }
}

function Show-Path($p) { if ([IO.Path]::IsPathRooted($p)) { $p } else { Join-Path $Repo $p } }
function Clean-Name($text) {
    $n = ($text.ToLower() -replace '[^a-z0-9_-]', '-').Trim('-')
    if ($n.Length -gt 40) { $n = $n.Substring(0, 40) }
    if (-not $n -or $n -notmatch '^[a-z0-9]') { $n = "folder-$n".Trim('-') }
    return $n
}

# --- the window ---------------------------------------------------------------------------
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Start UVMC'
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.ClientSize = New-Object System.Drawing.Size(560, 380)
$form.Font = New-Object System.Drawing.Font('Segoe UI', 10)

$title = New-Object System.Windows.Forms.Label
$title.Text = 'Folders the agent can see this time'
$title.Font = New-Object System.Drawing.Font('Segoe UI', 11, [System.Drawing.FontStyle]::Bold)
$title.Location = New-Object System.Drawing.Point(16, 14)
$title.AutoSize = $true
$form.Controls.Add($title)

$hint = New-Object System.Windows.Forms.Label
$hint.Text = "Read-only for the agent. It can ask to change a file; you approve each change in the panel."
$hint.Location = New-Object System.Drawing.Point(16, 40)
$hint.Size = New-Object System.Drawing.Size(530, 22)
$hint.ForeColor = [System.Drawing.Color]::DimGray
$form.Controls.Add($hint)

$box = New-Object System.Windows.Forms.CheckedListBox
$box.Location = New-Object System.Drawing.Point(16, 68)
$box.Size = New-Object System.Drawing.Size(528, 190)
$box.CheckOnClick = $true
$box.HorizontalScrollbar = $true
$form.Controls.Add($box)

function Refresh-List {
    $box.Items.Clear()
    foreach ($e in $entries) {
        $i = $box.Items.Add(("{0,-14}  {1}" -f $e.Name, (Show-Path $e.Path)))
        $box.SetItemChecked($i, [bool]$e.On)
    }
}
Refresh-List

$add = New-Object System.Windows.Forms.Button
$add.Text = 'Add folder...'
$add.Location = New-Object System.Drawing.Point(16, 268)
$add.Size = New-Object System.Drawing.Size(120, 30)
$add.Add_Click({
    $pick = New-Object System.Windows.Forms.FolderBrowserDialog
    $pick.Description = 'Choose a folder for the agent to see (read-only)'
    if ($pick.ShowDialog($form) -ne 'OK') { return }
    $full = $pick.SelectedPath.TrimEnd('\')
    $home_ = [Environment]::GetFolderPath('UserProfile').TrimEnd('\')
    if ($full -match '^[A-Za-z]:$' -or $full -eq $home_) {
        [System.Windows.Forms.MessageBox]::Show($form, 'Sharing a whole drive or your user folder is not allowed. Pick a more specific folder.', 'Start UVMC') | Out-Null
        return
    }
    $name = Clean-Name (Split-Path $full -Leaf)
    $name = [Microsoft.VisualBasic.Interaction]::InputBox('Short name for this folder (the agent sees /mnt/shared/<name>):', 'Folder name', $name)
    if (-not $name) { return }
    $name = Clean-Name $name
    $base = $name; $n = 2
    while ($entries | Where-Object { $_.Name -eq $name }) { $name = "$base-$n"; $n++ }
    $stored = $full
    if ($full.StartsWith($Repo + '\', [StringComparison]::OrdinalIgnoreCase)) { $stored = $full.Substring($Repo.Length + 1) }
    for ($i = 0; $i -lt $entries.Count; $i++) { $entries[$i].On = $box.GetItemChecked($i) }
    $entries.Add([pscustomobject]@{ Name = $name; Path = $stored; On = $true })
    Refresh-List
})
$form.Controls.Add($add)

$remove = New-Object System.Windows.Forms.Button
$remove.Text = 'Remove'
$remove.Location = New-Object System.Drawing.Point(144, 268)
$remove.Size = New-Object System.Drawing.Size(90, 30)
$remove.Add_Click({
    $i = $box.SelectedIndex
    if ($i -lt 0) { return }
    for ($j = 0; $j -lt $entries.Count; $j++) { $entries[$j].On = $box.GetItemChecked($j) }
    $entries.RemoveAt($i)
    Refresh-List
})
$form.Controls.Add($remove)

$note = New-Object System.Windows.Forms.Label
$note.Text = 'Remove only forgets the folder here; nothing is deleted.'
$note.Location = New-Object System.Drawing.Point(244, 275)
$note.AutoSize = $true
$note.ForeColor = [System.Drawing.Color]::DimGray
$form.Controls.Add($note)

$ask = New-Object System.Windows.Forms.CheckBox
$ask.Text = "Don't ask again (hold Shift when starting to see this window)"
$ask.Location = New-Object System.Drawing.Point(16, 310)
$ask.AutoSize = $true
$ask.Checked = (Test-Path $Skip)
$form.Controls.Add($ask)

$cancel = New-Object System.Windows.Forms.Button
$cancel.Text = 'Cancel'
$cancel.Location = New-Object System.Drawing.Point(352, 340)
$cancel.Size = New-Object System.Drawing.Size(90, 30)
$cancel.DialogResult = 'Cancel'
$form.Controls.Add($cancel)
$form.CancelButton = $cancel

$start = New-Object System.Windows.Forms.Button
$start.Text = 'Start'
$start.Location = New-Object System.Drawing.Point(454, 340)
$start.Size = New-Object System.Drawing.Size(90, 30)
$start.DialogResult = 'OK'
$form.Controls.Add($start)
$form.AcceptButton = $start

$form.Topmost = $true
$result = $form.ShowDialog()
if ($result -ne 'OK') { exit 1 }

# --- save choices --------------------------------------------------------------------------
for ($i = 0; $i -lt $entries.Count; $i++) { $entries[$i].On = $box.GetItemChecked($i) }
$out = New-Object System.Collections.Generic.List[string]
foreach ($h in $header) { $out.Add($h) }
if ($out.Count -and $out[$out.Count - 1].Trim()) { $out.Add('') }
foreach ($e in $entries) {
    $line = "{0} = {1}" -f $e.Name, $e.Path
    if (-not $e.On) { $line = "#off $line" }
    $out.Add($line)
}
$utf8 = New-Object System.Text.UTF8Encoding $false
[IO.File]::WriteAllText($List, ($out -join "`r`n") + "`r`n", $utf8)
if ($ask.Checked) { Set-Content -Path $Skip -Value 'The UVMC Panel shortcut skips the folder window. Delete this file (or hold Shift) to see it again.' }
elseif (Test-Path $Skip) { Remove-Item -LiteralPath $Skip }
exit 0
