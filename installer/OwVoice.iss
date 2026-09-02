#ifndef AppVersion
#define AppVersion "0.1.1"
#endif

#ifndef PayloadDir
#error PayloadDir must point to the prepared OwVoice release directory.
#endif

#ifndef InstallerOutputDir
#define InstallerOutputDir "..\dist\installer"
#endif

#ifndef BuildFlavor
#define BuildFlavor "Universal"
#endif

[Setup]
AppId={{ED8252EA-3ECD-48C3-ABEE-857313F3F37A}
AppName=OwVoice
AppVersion={#AppVersion}
AppVerName=OwVoice {#AppVersion}
AppPublisher=OwVoice contributors
AppPublisherURL=https://github.com/Zed2047/OwVoice
AppSupportURL=https://github.com/Zed2047/OwVoice/issues
DefaultDirName={localappdata}\Programs\OwVoice
DefaultGroupName=OwVoice
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
SetupIconFile=..\assets\OwVoice.ico
LicenseFile=..\LICENSE
UninstallDisplayIcon={app}\assets\OwVoice.ico
OutputDir={#InstallerOutputDir}
OutputBaseFilename=OwVoice-Setup-v{#AppVersion}-{#BuildFlavor}
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Excludes: "config\voices.local.json"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\config\voices.local.json"; DestDir: "{app}\config"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\OwVoice"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\scripts\launch_frontend.py"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\OwVoice.ico"; AppUserModelID: "OwVoice.Desktop"
Name: "{userdesktop}\OwVoice"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\scripts\launch_frontend.py"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\OwVoice.ico"; AppUserModelID: "OwVoice.Desktop"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\scripts\launch_frontend.py"""; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,OwVoice}"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\runtime\gpu-site"
Type: filesandordirs; Name: "{app}\runtime\gpu-downloads"
Type: filesandordirs; Name: "{app}\runtime\.gpu-site-*.tmp"
Type: files; Name: "{app}\runtime\gpu.enabled"
Type: files; Name: "{app}\runtime\gpu.enabled.tmp"
