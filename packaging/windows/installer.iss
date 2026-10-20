; Instalador Inno Setup do OculoDiag.
; Gerar (depois do PyInstaller):  iscc packaging\windows\installer.iss
#ifndef AppVersion
  #define AppVersion "1.1"
#endif

[Setup]
AppId={{6B0E7C1A-3F4D-4C8E-9A51-0D2C7E9B4F13}
AppName=OculoDiag
AppVersion={#AppVersion}
AppPublisher=OculoDiag
DefaultDirName={autopf}\OculoDiag
DefaultGroupName=OculoDiag
DisableProgramGroupPage=yes
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=OculoDiag-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\OculoDiag.exe

[Languages]
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\..\dist\OculoDiag\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\OculoDiag"; Filename: "{app}\OculoDiag.exe"
Name: "{group}\OculoDiag (simulação)"; Filename: "{app}\OculoDiag.exe"; Parameters: "--simular"
Name: "{group}\{cm:UninstallProgram,OculoDiag}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\OculoDiag"; Filename: "{app}\OculoDiag.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\OculoDiag.exe"; Description: "{cm:LaunchProgram,OculoDiag}"; Flags: nowait postinstall skipifsilent
