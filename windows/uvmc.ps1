# uvmc: use UVMC from a terminal or a script. Start it with uvmc.cmd.
#   uvmc ask "task" [-Model name] [-New] [-NoAsk] [-Json] [-Timeout seconds]
#       runs a task and shows its steps live; approvals are asked here (or refused with -NoAsk).
#       -Json waits quietly and prints the result as JSON (for scripts). Exit code 0 = done.
#   uvmc status | stop | new | models | model <name> | open | url
#   uvmc mcp    the relay Claude Code / Claude Desktop start to use UVMC as a tool
#               (connect-claude-apps sets that up; it must be switched on in the panel)
#   Set UVMC_URL and UVMC_TOKEN to skip looking them up through Multipass.
param(
    [Parameter(Position = 0)][string]$Command = 'help',
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest,
    [string]$Model,
    [switch]$New,
    [switch]$NoAsk,
    [switch]$Json,
    [int]$Timeout = 1800
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
. (Join-Path $PSScriptRoot 'timeouts.ps1')
$Name = 'uvmc'
$Port = 8080

$script:InRelay = $false
function Fail($text, $code = 2) {
    [Console]::Error.WriteLine("uvmc: $text")
    if ($script:InRelay) { throw $text }   # the relay keeps running and answers with an error
    exit $code
}
function Find-Multipass {
    $c = Get-Command multipass.exe -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Join-Path $env:ProgramFiles 'Multipass\bin\multipass.exe'
    if (Test-Path $p) { return $p }
    return $null
}
function Connect {
    # UVMC_URL and UVMC_TOKEN skip the lookup (e.g. from another machine, or in scripts).
    if ($env:UVMC_URL -and $env:UVMC_TOKEN) { return @{ Base = $env:UVMC_URL.TrimEnd('/'); Token = $env:UVMC_TOKEN } }
    $mp = Find-Multipass
    if (-not $mp) { Fail 'Multipass is not installed. Run install.cmd first.' }
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & $mp start $Name *> $null
    $ErrorActionPreference = $old
    # Quick lookups with a time limit on both sides (see timeouts.ps1): one that never answers
    # would leave the Claude apps waiting forever, and a Multipass client spinning a CPU core.
    $who = if ($script:InRelay) { 'uvmc mcp (relay)' } else { "uvmc $Command" }
    $r = Invoke-VmQuick $mp $Name 20 @('hostname', '-I') $who
    if ($r.TimedOut) { Fail $r.Message }
    $ip = ("$($r.Out)".Trim() -split '\s+')[0]
    $r = Invoke-VmQuick $mp $Name 20 @('sudo', 'cat', '/etc/uvmc/panel_token') $who
    if ($r.TimedOut) { Fail $r.Message }
    $token = "$($r.Out)".Trim()
    if (-not $ip -or -not $token) { Fail 'Could not reach the UVMC machine. Is it installed and running?' }
    return @{ Base = "http://${ip}:$Port"; Token = $token }
}
function Api($conn, $path, $body = $null, $timeoutSec = 60) {
    $headers = @{ Authorization = "Bearer $($conn.Token)" }
    try {
        if ($null -eq $body) {
            return Invoke-RestMethod -Uri ($conn.Base + $path) -Headers $headers -TimeoutSec $timeoutSec
        }
        $bytes = [Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Compress -Depth 5))
        return Invoke-RestMethod -Uri ($conn.Base + $path) -Method Post -Headers $headers -Body $bytes `
            -ContentType 'application/json; charset=utf-8' -TimeoutSec $timeoutSec
    } catch {
        $msg = $_.ErrorDetails.Message
        if ($msg) { try { $msg = ($msg | ConvertFrom-Json).error } catch {} }
        if (-not $msg) { $msg = $_.Exception.Message }
        Fail $msg
    }
}
function Short($text, $n = 160) {
    $t = ("$text" -replace '\s+', ' ').Trim()
    if ($t.Length -gt $n) { return $t.Substring(0, $n - 3) + '...' }
    return $t
}
function Describe($args_) {
    if ($null -eq $args_) { return '' }
    foreach ($k in 'command', 'path', 'instructions', 'packages') {
        $v = $args_.$k
        if ($v) { return Short (@($v) -join ' ') 120 }
    }
    return ''
}

function Mcp-Relay {
    # stdio MCP <-> POST /mcp. Only JSON goes to stdout.
    $script:InRelay = $true
    [Console]::InputEncoding = [Text.Encoding]::UTF8
    $out = [Console]::Out
    $conn = $null
    while ($null -ne ($line = [Console]::In.ReadLine())) {
        $line = $line.Trim()
        if (-not $line) { continue }
        $mid = $null
        try { $mid = ($line | ConvertFrom-Json).id } catch { continue }
        $reply = $null
        foreach ($attempt in 1, 2) {   # the machine's address can change: look it up again once
            try {
                if ($null -eq $conn) { $conn = Connect }
                $req = [Net.HttpWebRequest]::Create($conn.Base + '/mcp')
                $req.Method = 'POST'
                $req.ContentType = 'application/json'
                $req.Accept = 'application/json, text/event-stream'
                $req.Headers.Add('Authorization', "Bearer $($conn.Token)")
                $req.Timeout = 1000000
                $req.ReadWriteTimeout = 1000000
                $bytes = [Text.Encoding]::UTF8.GetBytes($line)
                $s = $req.GetRequestStream(); $s.Write($bytes, 0, $bytes.Length); $s.Close()
                try { $resp = $req.GetResponse() } catch [Net.WebException] { $resp = $_.Exception.Response; if (-not $resp) { throw } }
                # Locking UVMC changes the panel's key: look it up again (the catch below)
                if ([int]$resp.StatusCode -eq 401) { $resp.Close(); throw "the panel's key changed (UVMC was locked)" }
                $reader = New-Object IO.StreamReader($resp.GetResponseStream(), [Text.Encoding]::UTF8)
                $reply = $reader.ReadToEnd().Trim()
                $reader.Close(); $resp.Close()
                break
            } catch {
                $conn = $null
                if ($attempt -eq 2 -and $null -ne $mid) {
                    $reply = (@{ jsonrpc = '2.0'; id = $mid; error = @{ code = -32002; message = "Could not reach the UVMC machine: $($_.Exception.Message)" } } | ConvertTo-Json -Compress -Depth 4)
                }
            }
        }
        if ($reply) { $out.WriteLine(($reply -replace "`r?`n", ' ')); $out.Flush() }
    }
}

switch ($Command) {
    'ask' {
        $text = ($Rest -join ' ').Trim()
        if (-not $text) { Fail 'Usage: uvmc ask "what to do" [-Model name] [-New] [-NoAsk] [-Json]' }
        $conn = Connect
        if ($Json) {
            $body = @{ text = $text; new = [bool]$New; no_ask = [bool]$NoAsk; timeout = $Timeout }
            if ($Model) { $body.model = $Model }
            $r = Api $conn '/api/run' $body ($Timeout + 60)
            $r | ConvertTo-Json -Depth 6
            if ($r.done) { exit 0 } else { exit 1 }
        }
        if ($New) { Api $conn '/api/new' @{} | Out-Null }
        if ($Model) { Api $conn '/api/model' @{ name = $Model } | Out-Null }

        # Follow the live events, start the task, and print its steps until it ends.
        $req = [Net.HttpWebRequest]::Create($conn.Base + '/api/events')
        $req.Headers.Add('Authorization', "Bearer $($conn.Token)")
        $req.Timeout = 30000
        $req.ReadWriteTimeout = 60000   # the supervisor pings every 15 s
        $resp = $req.GetResponse()
        $reader = New-Object IO.StreamReader($resp.GetResponseStream(), [Text.Encoding]::UTF8)
        $started = Api $conn '/api/task' @{ text = $text }
        $after = $started.after   # events after this belong to our task
        $conv = $started.conv     # and only those of our conversation (several can work at once)
        $exit = 1
        $deadline = (Get-Date).AddSeconds($Timeout)
        try {
            while ($true) {
                $line = $reader.ReadLine()
                if ($null -eq $line) { Fail 'The connection to UVMC closed.' }
                if ((Get-Date) -gt $deadline) {
                    Write-Host 'Time is up; stopping the task.' -ForegroundColor Yellow
                    Api $conn '/api/stop' @{ conv = $conv } | Out-Null
                    $deadline = (Get-Date).AddYears(1)
                }
                if (-not $line.StartsWith('data: ')) { continue }
                $ev = $line.Substring(6) | ConvertFrom-Json
                if ($null -eq $ev.seq -or $ev.seq -le $after) { continue }   # earlier events of this conversation
                if ($conv -and $ev.conv -and $ev.conv -ne $conv) { continue }   # another conversation's
                $indent = if ($ev.parent) { '    ' } else { '' }
                switch ($ev.kind) {
                    'reply' {
                        foreach ($b in @($ev.content)) {
                            if ($b.type -eq 'text' -and $b.text) { Write-Host "$indent$($b.text.Trim())" }
                            elseif ($b.type -eq 'tool_use') { Write-Host "$indent> $($b.name) $(Describe $b.input)" -ForegroundColor Cyan }
                        }
                    }
                    'result' {
                        $first = (("$($ev.text)" -split "`n")[0])
                        $color = if ($ev.is_error) { 'Red' } else { 'DarkGray' }
                        Write-Host "$indent  $(Short $first 140)" -ForegroundColor $color
                    }
                    'approval' {
                        $what = if ($ev.type -eq 'site') { "reach $($ev.host)" } elseif ($ev.type -eq 'file') { "change $($ev.share)/$($ev.path)" } else { "$($ev.tool) $(Describe $ev.args)" }
                        if ($NoAsk) {
                            Api $conn '/api/approve' @{ id = $ev.id; answer = 'deny' } | Out-Null
                            Write-Host "$indent  refused (-NoAsk): $what" -ForegroundColor Yellow
                        } else {
                            $choices = if ($ev.type -eq 'site') { '[y]es for this task / [a]lways / [N]o' } else { '[y]es / [N]o' }
                            $a = Read-Host "$indent  Allow: $what  $choices"
                            $answer = switch -Regex ($a) { '^[yY]' { 'allow' } '^[aA]' { if ($ev.type -eq 'site') { 'always' } else { 'allow' } } default { 'deny' } }
                            try { Api $conn '/api/approve' @{ id = $ev.id; answer = $answer } | Out-Null } catch {}
                        }
                    }
                    'error' { Write-Host "$indent$($ev.text)" -ForegroundColor Red }
                    'notice' { Write-Host "$indent$($ev.text)" -ForegroundColor DarkGray }
                    'end' {
                        if (-not $ev.parent) {
                            $color = if ($ev.reason -eq 'done') { 'Green' } else { 'Yellow' }
                            Write-Host "[$($ev.reason)]" -ForegroundColor $color
                            if ($ev.reason -eq 'done') { $exit = 0 }
                        }
                    }
                }
                if ($ev.kind -eq 'end' -and -not $ev.parent) { break }
            }
        } finally {
            $reader.Close(); $resp.Close()
        }
        exit $exit
    }
    'status' {
        $s = Api (Connect) '/api/state'
        Write-Host "UVMC $($s.version): $($s.state), model $($s.model_label)$(if ($s.worker) { ", worker $($s.worker)" })"
        if (@($s.pending).Count) { Write-Host "Waiting for $(@($s.pending).Count) approval(s) in the panel." -ForegroundColor Yellow }
    }
    'stop' { Api (Connect) '/api/stop' @{} | Out-Null; Write-Host 'Stopped.' }
    'new' { Api (Connect) '/api/new' @{} | Out-Null; Write-Host 'New conversation.' }
    'models' {
        $s = Api (Connect) '/api/state'
        foreach ($m in $s.models) {
            $mark = if ($m.name -eq $s.model) { '*' } else { ' ' }
            $key = if ($m.has_key) { '' } elseif ($m.plan) { ' (not signed in)' } else { ' (no key)' }
            Write-Host ("{0} {1,-18} {2}{3}" -f $mark, $m.name, $m.label, $key)
        }
    }
    'model' {
        if (-not $Rest) { Fail 'Usage: uvmc model <name>   (see uvmc models)' }
        Api (Connect) '/api/model' @{ name = $Rest[0] } | Out-Null
        Write-Host "Model: $($Rest[0])"
    }
    'mcp' { Mcp-Relay; exit 0 }
    'open' { & (Join-Path $PSScriptRoot 'panel.ps1') }
    'url' {
        $c = Connect
        Write-Host "Address: $($c.Base)"
        Write-Host "Token:   $($c.Token)"
        Write-Host "Use it as: Authorization: Bearer <token>. API reference: $($c.Base)/docs"
    }
    default {
        Write-Host 'uvmc: use UVMC from a terminal or a script.'
        Write-Host ''
        Write-Host '  uvmc ask "task" [-Model name] [-New] [-NoAsk] [-Json] [-Timeout seconds]'
        Write-Host '      Runs a task and shows its steps; approvals are asked here.'
        Write-Host '      -NoAsk refuses anything needing approval. -Json waits and prints the result as JSON.'
        Write-Host '  uvmc status          what UVMC is doing'
        Write-Host '  uvmc stop            stop the current task'
        Write-Host '  uvmc new             start a new conversation'
        Write-Host '  uvmc models          list the models;  uvmc model <name> to switch'
        Write-Host '  uvmc open            open the panel'
        Write-Host '  uvmc url             the address and token, for your own scripts'
    }
}
