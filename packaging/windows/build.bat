@echo off
rem Gera dist\OculoDiag\OculoDiag.exe e, se o Inno Setup estiver instalado,
rem o instalador dist\OculoDiag-<versao>-setup.exe.
rem Requer Python 3.10+ (64 bits, de python.org, com Tcl/Tk).
setlocal
cd /d "%~dp0\..\.."

if not exist .venv-win (py -3 -m venv .venv-win || exit /b 1)
call .venv-win\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller || exit /b 1

pyinstaller --noconfirm --clean packaging\windows\oculodiag.spec || exit /b 1

for /f "tokens=2 delims==" %%v in ('findstr /b /c:"VERSION = " oculo_diag.py') do set VER=%%~v
set VER=%VER: =%
set VER=%VER:"=%

set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
  %ISCC% /DAppVersion=%VER% packaging\windows\installer.iss || exit /b 1
  echo Instalador: dist\OculoDiag-%VER%-setup.exe
) else (
  echo Inno Setup 6 nao encontrado - so o executavel foi gerado: dist\OculoDiag\OculoDiag.exe
  echo Instale com:  winget install JRSoftware.InnoSetup
)
