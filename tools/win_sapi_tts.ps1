<#
.SYNOPSIS
    Windows SAPI5 text-to-speech helper, called by tts_backend.py.

.DESCRIPTION
    Synthesizes a UTF-8 text file into a WAV file using .NET System.Speech.

    The text is deliberately passed as a FILE rather than a command-line
    argument, for two reasons:
      1. CJK text and emoji on the command line depend on the console code
         page and are easily mangled;
      2. Danmaku content is untrusted. Passing it through the command line
         exposes it to the PowerShell parser (quotes, backticks, $ expansion),
         which is an injection surface. With a file, this script only ever
         receives paths and never parses user content.

    NOTE: this file is intentionally pure ASCII. Windows PowerShell 5.1 reads
    .ps1 files WITHOUT a BOM as ANSI (cp936 on zh-CN systems), which would
    corrupt any non-ASCII character here. Keeping it ASCII makes the script
    parse identically under any code page, with or without a BOM.

.PARAMETER TextFile
    Path to the text to speak, UTF-8 (with or without BOM).

.PARAMETER OutFile
    Output WAV path. Missing parent directories are created.

.PARAMETER Voice
    Installed voice name, e.g. "Microsoft Huihui Desktop".
    Falls back to the system default voice when not installed.

.PARAMETER Rate
    Speaking rate, -10..10, 0 is normal.

.PARAMETER Volume
    Volume, 0..100.

.PARAMETER ListVoices
    Print installed voices as "name<TAB>culture<TAB>gender" lines, no synthesis.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File win_sapi_tts.ps1 `
        -TextFile C:\tmp\t.txt -OutFile C:\tmp\o.wav -Voice "Microsoft Huihui Desktop"
#>
[CmdletBinding()]
param(
    [string] $TextFile = "",
    [string] $OutFile = "",
    [string] $Voice = "",
    [int]    $Rate = 0,
    [int]    $Volume = 100,
    [switch] $ListVoices
)

$ErrorActionPreference = "Stop"

# Emit UTF-8 on stdout/stderr so the parent process can decode reliably
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }

Add-Type -AssemblyName System.Speech

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    if ($ListVoices) {
        foreach ($v in $synth.GetInstalledVoices()) {
            if (-not $v.Enabled) { continue }
            $info = $v.VoiceInfo
            Write-Output ("{0}`t{1}`t{2}" -f $info.Name, $info.Culture.Name, $info.Gender)
        }
        return
    }

    if (-not $TextFile) { throw "missing -TextFile" }
    if (-not $OutFile) { throw "missing -OutFile" }
    if (-not (Test-Path -LiteralPath $TextFile)) { throw "text file not found: $TextFile" }

    $text = [System.IO.File]::ReadAllText($TextFile, (New-Object System.Text.UTF8Encoding($false)))
    if ([string]::IsNullOrWhiteSpace($text)) { throw "empty text" }

    $outDir = Split-Path -Parent $OutFile
    if ($outDir -and -not (Test-Path -LiteralPath $outDir)) {
        New-Item -ItemType Directory -Path $outDir -Force | Out-Null
    }

    # Match macOS `say --data-format=LEI16@22050`: 22050 Hz / 16 bit / mono
    $fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
        22050,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)
    $synth.SetOutputToWaveFile($OutFile, $fmt)

    if ($Voice) {
        $installed = @($synth.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name })
        if ($installed -contains $Voice) {
            $synth.SelectVoice($Voice)
        } else {
            [Console]::Error.WriteLine("warn: voice '$Voice' not installed, using default voice")
        }
    }

    if ($Rate -lt -10) { $Rate = -10 }
    if ($Rate -gt 10) { $Rate = 10 }
    if ($Volume -lt 0) { $Volume = 0 }
    if ($Volume -gt 100) { $Volume = 100 }
    $synth.Rate = $Rate
    $synth.Volume = $Volume

    $synth.Speak($text)
}
finally {
    $synth.Dispose()
}
