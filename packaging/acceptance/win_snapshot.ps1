# 一次调用拿到「所有可见顶层窗口 + 全部进程」，以 JSON 打到 stdout（RAY-493 windows-ci-acceptance）。
#
# preview_acceptance.mjs 用它判「Windows 不出现控制台窗口」（A5）：自己按进程树筛，
# 这里只如实列出，不做判断。两份数据放在一次调用里，是因为 PowerShell 启动本身要一两秒，
# 而窗口与进程必须是同一时刻的快照才能对得上。
#
# 控制台窗口的归属：conhost 托管的窗口，GetWindowThreadProcessId 给出的是 conhost.exe 的
# pid，而 conhost 的父进程是那个控制台程序 —— 所以沿进程树往下找能覆盖到。
# Windows Terminal 托管时窗口属于 WindowsTerminal.exe，不在树里；驱动另用「启动前后新增的
# 控制台类窗口」兜这一种，并用一个故意弹出的控制台做正对照，证明检测在当前环境里看得见。
$ErrorActionPreference = "Stop"

Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class GaitWindows {
  public delegate bool EnumProc(IntPtr hwnd, IntPtr lParam);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
  [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hwnd);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr hwnd, StringBuilder s, int n);
  [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd, out Rect r);
  [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left, Top, Right, Bottom; }

  public class Info {
    public long hwnd; public uint pid; public string cls; public string title;
    public int width; public int height; public bool minimized;
  }

  public static List<Info> Visible() {
    var list = new List<Info>();
    EnumWindows((hwnd, _) => {
      if (!IsWindowVisible(hwnd)) return true;
      uint pid; GetWindowThreadProcessId(hwnd, out pid);
      var cls = new StringBuilder(256); GetClassName(hwnd, cls, cls.Capacity);
      var title = new StringBuilder(512); GetWindowText(hwnd, title, title.Capacity);
      Rect r; GetWindowRect(hwnd, out r);
      list.Add(new Info {
        hwnd = hwnd.ToInt64(), pid = pid, cls = cls.ToString(), title = title.ToString(),
        width = r.Right - r.Left, height = r.Bottom - r.Top, minimized = IsIconic(hwnd),
      });
      return true;
    }, IntPtr.Zero);
    return list;
  }
}
"@

$processes = Get-CimInstance Win32_Process | ForEach-Object {
  [pscustomobject]@{
    pid = [int]$_.ProcessId
    ppid = [int]$_.ParentProcessId
    name = $_.Name
    command = $_.CommandLine
  }
}

[pscustomobject]@{
  windows = @([GaitWindows]::Visible())
  processes = @($processes)
} | ConvertTo-Json -Depth 4 -Compress
