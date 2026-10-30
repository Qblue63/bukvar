# PyInstaller: две програми (с прозорци и за команден ред) в една папка.
# Tesseract се добавя в dist\Bukvar\tesseract от build.ps1.

block_cipher = None
excludes = ["tkinter", "matplotlib", "scipy", "pandas", "IPython", "fontTools",
            "PySide6.QtWebEngineCore", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore",
            "PySide6.QtMultimedia", "PySide6.QtNetwork", "PySide6.QtPdf", "PySide6.QtSvg"]
datas = [("assets", "assets")]

gui_a = Analysis(["run_gui.py"], datas=datas, excludes=excludes, cipher=block_cipher)
cli_a = Analysis(["run_cli.py"], datas=[], excludes=excludes + ["PySide6"], cipher=block_cipher)

gui_pyz = PYZ(gui_a.pure, cipher=block_cipher)
cli_pyz = PYZ(cli_a.pure, cipher=block_cipher)

gui_exe = EXE(gui_pyz, gui_a.scripts, [], exclude_binaries=True, name="Bukvar",
              console=False, icon="assets/bukvar.ico", version="version_info.txt")
cli_exe = EXE(cli_pyz, cli_a.scripts, [], exclude_binaries=True, name="bukvar-cli",
              console=True, icon="assets/bukvar.ico", version="version_info.txt")

coll = COLLECT(gui_exe, gui_a.binaries, gui_a.datas,
               cli_exe, cli_a.binaries, cli_a.datas,
               strip=False, upx=False, name="Bukvar")
