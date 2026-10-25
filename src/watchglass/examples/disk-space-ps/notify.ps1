param([string]$What)
try { $utf8 = New-Object System.Text.UTF8Encoding $false; [Console]::OutputEncoding = $utf8; $OutputEncoding = $utf8 } catch { }
# Report a change; the output goes to the watchglass log. Replace it with a toast or an email as you like.
Write-Output ("drive {0}: {1} (watch {2}, poll {3})" -f $env:WATCHGLASS_SETTING_DRIVE, $What, $env:WATCHGLASS_WATCH, $env:WATCHGLASS_POLL_ID)
