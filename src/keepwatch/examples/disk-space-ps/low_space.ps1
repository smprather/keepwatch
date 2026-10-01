try { $utf8 = New-Object System.Text.UTF8Encoding $false; [Console]::OutputEncoding = $utf8; $OutputEncoding = $utf8 } catch { }
# TRUE (exit 0) when the drive has less free space than min_free_gb, FALSE (exit 1) otherwise.
# Any other failure exits 2, which keepwatch reports as an error.
try {
    $drive = Get-PSDrive -Name $env:KEEPWATCH_SETTING_DRIVE -PSProvider FileSystem -ErrorAction Stop
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    Write-Output "free: $freeGb GB"
    if ($freeGb -lt [double]$env:KEEPWATCH_SETTING_MIN_FREE_GB) { exit 0 } else { exit 1 }
} catch {
    Write-Error $_
    exit 2
}
