; Inno Setup — инсталатор на Буквар
#define AppName "Буквар"
#define AppVersion GetEnv("BUKVAR_VERSION")
#if AppVersion == ""
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{35ABFB02-4911-42B3-932B-74F74E197342}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Bukvar
DefaultDirName={autopf}\Bukvar
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=Bukvar-Setup-{#AppVersion}
SetupIconFile=assets\bukvar.ico
UninstallDisplayIcon={app}\Bukvar.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Може да се инсталира и без администраторски права (само за текущия потребител)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ChangesAssociations=yes

[Languages]
#if FileExists(CompilerPath + "Languages\Bulgarian.isl")
Name: "bg"; MessagesFile: "compiler:Languages\Bulgarian.isl"
#endif
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Икона на работния плот"; GroupDescription: "Допълнително:"
Name: "contextmenu"; Description: "„Разпознай с Буквар“ в менюто на десния бутон (PDF и изображения)"; GroupDescription: "Допълнително:"

[Files]
Source: "dist\Bukvar\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\Bukvar.exe"
Name: "{group}\Деинсталиране на {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Bukvar.exe"; Tasks: desktopicon

[Registry]
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\Bukvar"; ValueType: string; ValueName: ""; ValueData: "Разпознай с Буквар"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\Bukvar"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\Bukvar.exe"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\Bukvar\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Bukvar.exe"" ""%1"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.png\shell\Bukvar"; ValueType: string; ValueName: ""; ValueData: "Разпознай с Буквар"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.png\shell\Bukvar"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\Bukvar.exe"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.png\shell\Bukvar\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Bukvar.exe"" ""%1"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.jpg\shell\Bukvar"; ValueType: string; ValueName: ""; ValueData: "Разпознай с Буквар"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.jpg\shell\Bukvar"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\Bukvar.exe"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.jpg\shell\Bukvar\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Bukvar.exe"" ""%1"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.jpeg\shell\Bukvar"; ValueType: string; ValueName: ""; ValueData: "Разпознай с Буквар"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.jpeg\shell\Bukvar"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\Bukvar.exe"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.jpeg\shell\Bukvar\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Bukvar.exe"" ""%1"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.tif\shell\Bukvar"; ValueType: string; ValueName: ""; ValueData: "Разпознай с Буквар"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.tif\shell\Bukvar"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\Bukvar.exe"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.tif\shell\Bukvar\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Bukvar.exe"" ""%1"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.tiff\shell\Bukvar"; ValueType: string; ValueName: ""; ValueData: "Разпознай с Буквар"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.tiff\shell\Bukvar"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\Bukvar.exe"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.tiff\shell\Bukvar\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Bukvar.exe"" ""%1"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.bmp\shell\Bukvar"; ValueType: string; ValueName: ""; ValueData: "Разпознай с Буквар"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.bmp\shell\Bukvar"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\Bukvar.exe"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.bmp\shell\Bukvar\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Bukvar.exe"" ""%1"""; Tasks: contextmenu

[Run]
Filename: "{app}\Bukvar.exe"; Description: "Стартирай {#AppName}"; Flags: nowait postinstall skipifsilent
