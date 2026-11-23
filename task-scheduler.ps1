$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    if ($request.op -ne 'list' -and $request.name -notmatch '^CodexAutoResumeGUI_[0-9a-f]{32}$') {
        throw 'Invalid scheduled task name.'
    }

    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $folder = $service.GetFolder('\')

    switch ($request.op) {
        'register' {
            $task = $service.NewTask(0)
            $task.RegistrationInfo.Description = 'Continue one local Codex session at the scheduled time.'
            $task.Principal.UserId = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
            $task.Principal.LogonType = 3 # TASK_LOGON_INTERACTIVE_TOKEN
            $task.Principal.RunLevel = 0 # TASK_RUNLEVEL_LUA

            $trigger = $task.Triggers.Create(1) # TASK_TRIGGER_TIME
            $trigger.StartBoundary = [string]$request.when
            $action = $task.Actions.Create(0) # TASK_ACTION_EXEC
            $action.Path = [string]$request.executable
            $action.Arguments = [string]$request.arguments
            $action.WorkingDirectory = [string]$request.cwd

            $settings = $task.Settings
            $settings.StartWhenAvailable = $true
            $settings.WakeToRun = [bool]$request.wake
            $settings.DisallowStartIfOnBatteries = $false
            $settings.StopIfGoingOnBatteries = $false
            $settings.ExecutionTimeLimit = 'PT13H'
            $settings.MultipleInstances = 2 # TASK_INSTANCES_IGNORE_NEW
            $null = $folder.RegisterTaskDefinition($request.name, $task, 2, $null, $null, 3, $null) # TASK_CREATE
        }
        'remove' {
            try {
                $folder.DeleteTask($request.name, 0)
            } catch {
                $cause = $_.Exception.GetBaseException()
                if ($cause.HResult -ne -2147024894) { throw } # HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND)
            }
        }
        'list' {
            $names = @($folder.GetTasks(1) | Where-Object Name -Match '^CodexAutoResumeGUI_[0-9a-f]{32}$' | ForEach-Object { $_.Name })
            [Console]::Out.WriteLine((ConvertTo-Json -InputObject $names -Compress))
        }
        default { throw 'Unknown scheduler operation.' }
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.GetBaseException().Message)
    exit 1
}
