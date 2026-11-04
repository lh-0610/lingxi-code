; 灵犀Code 安装程序（Inno Setup 6）。先用 PyInstaller 打出 dist\灵犀Code\，再编译这个脚本：
;   python installer\build.py                    两步一起做，版本号取 src\__init__.py
;   ISCC /DAppVersion=1.0.0 installer\lingxi.iss  只做安装程序
;
; 默认装到当前用户（%LOCALAPPDATA%\Programs\灵犀Code），不弹管理员权限。配置、会话、日志存在
; %APPDATA%\灵犀（src/paths.py），不在程序目录里：覆盖升级、卸载都不动用户数据。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "灵犀Code"
#define AppExe "灵犀Code.exe"

[Setup]
; AppId 定了就不要再改：覆盖升级、卸载都靠它认出是同一个程序
AppId={{7F15B274-7F42-420F-B524-1DDC64F14347}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
AppPublisherURL=https://github.com/lh-0610/lingxi-code
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=LingxiCode-Setup-{#AppVersion}
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 灵犀开着时覆盖安装：先提示关掉它，不然文件被占用装不上
CloseApplications=yes
RestartApplications=no

[Languages]
; 官方仓库 jrsoftware/issrc 的 Files/Languages/ChineseSimplified.isl（提交 6ef3219），6.7.3 发行版里还没带
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; 覆盖升级前先清掉旧的 _internal：新版本去掉的依赖不会留下来和新文件混用
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: dirifempty; Name: "{app}"
