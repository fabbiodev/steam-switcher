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
function RelaunchAfterUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;

// Account data is in %APPDATA%\SteamSDA and is deliberately not uninstalled.
