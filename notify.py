"""Windows 알림(토스트/풍선) — 발행 실패 등 조용한 사고를 즉시 알린다.

외부 모듈 없이 System.Windows.Forms.NotifyIcon 으로 트레이 풍선을 띄운다
(Win10/11 에선 알림 센터 토스트로 표시됨). 비차단(백그라운드)으로 실행.

사용:
  python notify.py "제목" "내용"        # 수동 테스트
  from notify import notify; notify(...)
"""
from __future__ import annotations

import subprocess
import sys

_PS = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Warning
$n.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning
$n.BalloonTipTitle = $env:NT_TITLE
$n.BalloonTipText = $env:NT_TEXT
$n.Visible = $true
$n.ShowBalloonTip(12000)
Start-Sleep -Seconds 8
$n.Dispose()
"""


def notify(title: str, message: str) -> bool:
    """트레이 풍선 알림을 띄운다(비차단). 성공적으로 '실행'하면 True."""
    try:
        env = {"NT_TITLE": str(title)[:120], "NT_TEXT": str(message)[:250]}
        import os
        full = {**os.environ, **env}
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-Command", _PS],
            env=full,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except Exception as e:
        print("알림 실패:", e)
        return False


if __name__ == "__main__":
    t = sys.argv[1] if len(sys.argv) > 1 else "SNS Agent 테스트"
    m = sys.argv[2] if len(sys.argv) > 2 else "알림이 정상 작동합니다."
    notify(t, m)
    print("알림 전송(트레이 확인).")
