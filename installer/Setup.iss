; Install-Panel.ps1 keeps interpreter generations used by existing tasks.
[Setup]
AppId={{AEBEF550-045B-4845-AC8B-EC59E4E451F3}
AppName=Codex Auto Resume
AppVersion={#AppVersion}
AppPublisher=Codex Auto Resume contributors
AppPublisherURL=https://github.com/PanGucheng/Codex-Auto-Resume
DefaultDirName={localappdata}\CodexAutoResumeGUI\setup
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
MinVersion=10.0
DisableDirPage=yes
DisableProgramGroupPage=yes
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Codex Auto Resume

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#ProjectRoot}\Install-Panel.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\Start-SharedCodex.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Payload}\*"; DestDir: "{tmp}\CodexAutoResumePayload"; Flags: ignoreversion recursesubdirs createallsubdirs deleteafterinstall

[CustomMessages]
Finish=Reopen Codex. In a saved local chat, use Show tabs (Ctrl+Shift+B), then New tab, Open, and Codex Auto Resume.

[Code]
procedure RunInstallerScript(Arguments: String);
var
  Output: TExecOutput;
  ExitCode: Integer;
  I: Integer;
  Detail: String;
begin
  if not ExecAndCaptureOutput(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
      ExpandConstant('{app}\Install-Panel.ps1') + '" ' + Arguments,
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ExitCode, Output) then
    RaiseException(SysErrorMessage(ExitCode));

  for I := 0 to GetArrayLength(Output.StdOut) - 1 do
    Log(Output.StdOut[I]);
  for I := 0 to GetArrayLength(Output.StdErr) - 1 do begin
    Log(Output.StdErr[I]);
    if Detail = '' then Detail := Output.StdErr[I];
  end;
  if ExitCode <> 0 then begin
    if Detail = '' then Detail := 'Codex Auto Resume setup failed (exit ' + IntToStr(ExitCode) + ').';
    RaiseException(Detail);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    RunInstallerScript('-BundledPayload "' + ExpandConstant('{tmp}\CodexAutoResumePayload') + '"');
end;

procedure InitializeWizard();
begin
  WizardForm.FinishedLabel.Caption := CustomMessage('Finish');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RunInstallerScript('-UninstallBundle');
end;
