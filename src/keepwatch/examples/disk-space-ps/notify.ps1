param([string]$What)
try { $utf8 = New-Object System.Text.UTF8Encoding $false; [Console]::OutputEncoding = $utf8; $OutputEncoding = $utf8 } catch { }
# Report a change; the output goes to the keepwatch log. Replace it with a toast or an email as you like.
Write-Output ("drive {0}: {1} (watch {2}, poll {3})" -f $env:KEEPWATCH_SETTING_DRIVE, $What, $env:KEEPWATCH_WATCH, $env:KEEPWATCH_POLL_ID)
