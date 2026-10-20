# Generate synthetic release fixtures; no microphone audio is recorded.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$target = Join-Path (Split-Path $PSScriptRoot -Parent) 'logs'
New-Item -ItemType Directory -Path $target -Force | Out-Null
$format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(
    16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
foreach ($language in @('zh', 'en')) {
    $speaker = [System.Speech.Synthesis.SpeechSynthesizer]::new()
    try {
        $voice = $speaker.GetInstalledVoices() |
            Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq $language } |
            Select-Object -First 1
        if (-not $voice) { throw "Install a Windows SAPI $language voice before generating fixtures." }
        $speaker.SelectVoice($voice.VoiceInfo.Name)
        $speaker.SetOutputToWaveFile((Join-Path $target "regression_$language.wav"), $format)
        $prompt = [System.Speech.Synthesis.PromptBuilder]::new($voice.VoiceInfo.Culture)
        if ($language -eq 'zh') {
            $prompt.AppendText('今天我们先检查麦克风。')
            $prompt.AppendBreak([TimeSpan]::FromMilliseconds(700))
            $prompt.AppendText('稍后再讨论实验结果。请等我把这句话说完，再翻译成英文。')
        } else {
            $prompt.AppendText('Please let me finish this sentence before translating it. We will check the microphone first and discuss the experiment later.')
        }
        $speaker.Speak($prompt)
    } finally {
        $speaker.Dispose()
    }
}
Write-Output "Synthetic fixtures generated in $target"
