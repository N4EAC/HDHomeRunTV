#define MyAppName "HDHomeRun TV Player"
#define MyAppVersion "1.0.10"
#define MyAppPublisher "Eduardo A. de Carvalho"
#define MyAppExeName "HDHomeRunTV.exe"

[Setup]
AppId={{99DCE056-4A59-47F8-AD6A-9982DE536F30}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\HDHomeRun TV Player
DefaultGroupName={#MyAppName}
PrivilegesRequired=admin
OutputDir=installer
OutputBaseFilename=HDHomeRun_TV_Player_v1.0.10_Setup
SetupIconFile=assets\hdhomerun_tv.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "HDHomeRunTV.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "assets\hdhomerun_tv.ico"; DestDir: "{app}\assets"; Flags: ignoreversion
Source: "engine\*"; DestDir: "{app}\engine"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\hdhomerun_tv.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\hdhomerun_tv.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
