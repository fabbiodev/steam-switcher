; Keep AppId and the per-user install mode stable across releases.
#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef AppSource
  #error AppSource is required
#endif
#ifndef ReleaseOutput
  #error ReleaseOutput is required
#endif

[Setup]
AppId=SteamSDA.Desktop
AppName=Steam SDA
AppVersion={#AppVersion}
AppVerName=Steam SDA {#AppVersion}
AppPublisher=fabbiodev
AppPublisherURL=https://github.com/fabbiodev/steam-switcher
AppUpdatesURL=https://github.com/fabbiodev/steam-switcher/releases/latest
DefaultDirName={localappdata}\Programs\Steam SDA
UsePreviousAppDir=yes
DisableDirPage=no
DefaultGroupName=Steam SDA
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
SetupIconFile={#AppSource}\assets\app.ico
LicenseFile={#AppSource}\LICENSE
OutputDir={#ReleaseOutput}
OutputBaseFilename=SteamSwitcher-v{#AppVersion}-Setup-x64
Compression=lzma2/fast
SolidCompression=yes
UninstallDisplayIcon={app}\assets\app.ico
CloseApplications=no
RestartApplications=no
SetupMutex=SteamSDA.Setup

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[CustomMessages]
russian.UpdateFileBlocked=Обновление остановлено до замены файлов. Не удалось получить доступ к файлу:%n%1%n%nЗакройте Steam SDA, Steam или другую использующую файл программу и повторите установку. Если ошибка повторяется, проверьте права на папку установки. Код Windows: %2.
english.UpdateFileBlocked=Update stopped before replacing files. Cannot access:%n%1%n%nClose Steam SDA, Steam or another application using this file, then retry. If this persists, check installation folder permissions. Windows error: %2.

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Steam SDA"; Filename: "{app}\SteamSDA.exe"; WorkingDir: "{app}"; IconFilename: "{app}\assets\app.ico"
Name: "{autodesktop}\Steam SDA"; Filename: "{app}\SteamSDA.exe"; WorkingDir: "{app}"; IconFilename: "{app}\assets\app.ico"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\App Paths\SteamSDA.exe"; ValueType: string; ValueName: ""; ValueData: "{app}\SteamSDA.exe"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\App Paths\SteamSDA.exe"; ValueType: string; ValueName: "Path"; ValueData: "{app}"

[Run]
Filename: "{app}\SteamSDA.exe"; Description: "{cm:LaunchProgram,Steam SDA}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\SteamSDA.exe"; Flags: nowait runasoriginaluser; Check: RelaunchAfterUpdate

[Code]
function OpenForUpdate(FileName: String; Access, Share: Cardinal;
  Security: NativeInt; Disposition, Attributes: Cardinal; Template: THandle): THandle;
  external 'CreateFileW@kernel32.dll stdcall';
function CloseUpdateHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';
function UpdateLastError: Cardinal;
  external 'GetLastError@kernel32.dll stdcall';

function WriteError(const FileName: String): Cardinal;
var
  Handle: THandle;
begin
  Result := 0;
  // OPEN_EXISTING + GENERIC_WRITE, share read/write/delete. Never write bytes.
  Handle := OpenForUpdate(FileName, $40000000, 7, 0, 3, 0, 0);
  if Handle = THandle(-1) then
    Result := UpdateLastError
  else
    CloseUpdateHandle(Handle);
  if (Result = 2) or (Result = 3) then Result := 0;
end;

function CheckUpdateFiles(const Directory: String): String;
var
  Entry: TFindRec;
  FileName: String;
  Error: Cardinal;
begin
  Result := '';
  if not FindFirst(AddBackslash(Directory) + '*', Entry) then Exit;
  try
    repeat
      if (Entry.Name <> '.') and (Entry.Name <> '..') then begin
        FileName := AddBackslash(Directory) + Entry.Name;
        // Do not follow junctions into another installation or data directory.
        if (Entry.Attributes and $400) <> 0 then begin
          Result := FmtMessage(CustomMessage('UpdateFileBlocked'), [FileName, '4390']);
        end else if (Entry.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
          Result := CheckUpdateFiles(FileName)
        else begin
          Error := WriteError(FileName);
          if Error <> 0 then
            Result := FmtMessage(CustomMessage('UpdateFileBlocked'), [FileName, IntToStr(Error)]);
        end;
        if Result <> '' then Exit;
      end;
    until not FindNext(Entry);
  finally
    FindClose(Entry);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Attempt: Integer;
begin
  // The updater starts Setup just before its own process exits. Allow that exit
  // to finish before probing the executable and Python/Qt runtime files.
  for Attempt := 1 to 50 do begin
    if WriteError(ExpandConstant('{app}\SteamSDA.exe')) = 0 then Break;
    Sleep(200);
  end;
  Result := CheckUpdateFiles(ExpandConstant('{app}'));
  if Result <> '' then Log(Result);
end;

function RelaunchAfterUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;

// Account data is in %APPDATA%\SteamSDA and is deliberately not uninstalled.
