; Installateur Windows de BrickMote (Inno Setup 6)
;   iscc /DAppVersion=1.0.0 /DSourceDir=<dist\BrickMote> /DOutputDir=<out> packaging\windows\brickmote.iss

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\BrickMote"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\out"
#endif

[Setup]
AppId={{8F3C2A61-6B0E-4C7B-9C52-3E1F0B7D2A94}
AppName=BrickMote
AppVersion={#AppVersion}
AppVerName=BrickMote {#AppVersion}
AppPublisher=BrickMote
AppPublisherURL=https://github.com/emerick-marchese/BrickMote
AppSupportURL=https://github.com/emerick-marchese/BrickMote/issues
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\BrickMote
DefaultGroupName=BrickMote
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=BrickMote-Setup-{#AppVersion}-Windows-x64
SetupIconFile=..\brickmote.ico
UninstallDisplayIcon={app}\BrickMote.exe
UninstallDisplayName=BrickMote
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
PrivilegesRequired=admin
UsedUserAreasWarning=no
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "startup"; Description: "Lancer BrickMote au démarrage de Windows (dans la zone de notification, pour rester joignable)"; GroupDescription: "Options :"; Flags: unchecked
Name: "firewall"; Description: "Autoriser les connexions directes (TCP 7070) et la découverte (UDP 50001) dans le pare-feu, sur les réseaux privés uniquement"; GroupDescription: "Options :"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\BrickMote"; Filename: "{app}\BrickMote.exe"
Name: "{group}\Désinstaller BrickMote"; Filename: "{uninstallexe}"
Name: "{autodesktop}\BrickMote"; Filename: "{app}\BrickMote.exe"; Tasks: desktopicon
Name: "{userstartup}\BrickMote"; Filename: "{app}\BrickMote.exe"; Parameters: "--minimized"; Tasks: startup

[Run]
; Remove older rules first so that reinstalling never duplicates them
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""BrickMote (TCP 7070)"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""BrickMote (UDP 50001)"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""BrickMote (TCP 7070)"" dir=in action=allow program=""{app}\BrickMote.exe"" protocol=TCP localport=7070 profile=private"; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""BrickMote (UDP 50001)"" dir=in action=allow program=""{app}\BrickMote.exe"" protocol=UDP localport=50001 profile=private"; Flags: runhidden; Tasks: firewall
Filename: "{app}\BrickMote.exe"; Description: "{cm:LaunchProgram,BrickMote}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""BrickMote (TCP 7070)"""; Flags: runhidden; RunOnceId: "DelFirewallTcp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""BrickMote (UDP 50001)"""; Flags: runhidden; RunOnceId: "DelFirewallUdp"

[Messages]
french.WelcomeLabel2=Cet assistant va installer [name/ver] sur votre ordinateur.%n%nBrickMote est un logiciel de bureau à distance chiffré de bout en bout. Vos réglages et votre ID sont conservés lors d'une mise à jour ou d'une désinstallation.
