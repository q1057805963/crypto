# 手动启动脚本（不会自动运行）：重启电脑后执行本脚本拉起监控
# 端口需与 config.yaml 的 dashboard.port 保持一致
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$port = 8765

# 已有实例在监听时不再重复启动，避免双实例争抢 Telegram 轮询
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    exit 0
}

Start-Process -FilePath 'C:\Program Files\Python313\python.exe' `
    -ArgumentList 'main.py' `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $root 'monitor.out') `
    -RedirectStandardError (Join-Path $root 'monitor.err')
