[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location 'C:\GSMPSWechatBot'
& '.\.venv\Scripts\python.exe' -u main.py 2>&1 | ForEach-Object {
    $line = $_.ToString()
    [Console]::Out.WriteLine($line)
    Add-Content -LiteralPath 'C:\GSMPSWechatBot\run.log' -Value $line -Encoding UTF8
}
exit $LASTEXITCODE
