#ifndef AppVersion
  #define AppVersion "0.4.2"
#endif
[Setup]
AppId={{C40C9B61-B31D-46BE-A841-E38A208F784E}
AppName=纸屿 Paper Isles
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Paper Isles
DefaultGroupName=纸屿 Paper Isles
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release\windows
OutputBaseFilename=PaperIsles-{#AppVersion}-Windows-x64-Setup
SetupIconFile=PaperIsles.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
[Files]
Source: "..\.build\windows\dist\PaperIsles\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\纸屿 Paper Isles"; Filename: "{app}\PaperIsles.exe"
Name: "{autodesktop}\纸屿 Paper Isles"; Filename: "{app}\PaperIsles.exe"; Tasks: desktopicon
[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: checkedonce
[Run]
Filename: "{app}\PaperIsles.exe"; Description: "Launch Paper Isles"; Flags: nowait postinstall skipifsilent
