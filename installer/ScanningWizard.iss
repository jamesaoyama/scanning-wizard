; Inno Setup script for the one-click installer (built by .github/workflows/build-windows.yml).
; Installs for the current user (no admin prompt), adds a desktop icon, and opens the program.
; Keep AppVersion the same as APP_VERSION in wizard_core.py (a test checks this).

#define AppVersion "4.0.0"

[Setup]
AppId={{037CC1C7-0033-480E-B781-AADCA64F092D}
AppName=Scanning Wizard
AppVersion={#AppVersion}
AppVerName=Scanning Wizard {#AppVersion}
DefaultDirName={autopf}\Scanning Wizard
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ScanningWizardSetup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\Scanning Wizard.exe
UninstallDisplayName=Scanning Wizard
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
MinVersion=6.3
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Put a Scanning Wizard icon on the desktop"

[Files]
Source: "..\dist\Scanning Wizard\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; files from an older version that the new one no longer has
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\Scanning Wizard"; Filename: "{app}\Scanning Wizard.exe"
Name: "{autodesktop}\Scanning Wizard"; Filename: "{app}\Scanning Wizard.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Scanning Wizard.exe"; Description: "Open Scanning Wizard now"; Flags: nowait postinstall skipifsilent
; Updates from inside the program run silently with /RELAUNCH=1: open it again afterwards.
Filename: "{app}\Scanning Wizard.exe"; Flags: nowait; Check: ShouldRelaunch

[Code]
function ShouldRelaunch(): Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;
