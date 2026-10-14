@echo off
REM ============================================
REM 一键打包成 Windows EXE(无需安装Python环境即可运行)
REM 用法: 双击本文件(install_run.bat), 在菜单中选择
REM   1 = 只运行程序(需先装好Python)
REM   2 = 打包成exe
REM ============================================
chcp 65001 >nul
echo.
echo ==========================================
echo   南昌零碳园区储能仿真模型
echo ==========================================
echo.
echo   [1] 直接运行 (需已安装Python)
echo   [2] 打包成 EXE (双击即可运行, 无需Python)
echo.
set /p CHOICE=请输入数字后回车: 

if "%CHOICE%"=="1" goto run
if "%CHOICE%"=="2" goto pack
echo 输入无效, 退出。
pause
exit /b

:run
echo.
echo 检查依赖...
pip install -r requirements.txt
echo 开始运行...
python run_simulation.py
echo.
echo 运行完成! 图与报告已生成在 out 文件夹。
pause
exit /b

:pack
echo.
echo 检查依赖...
pip install -r requirements.txt
echo 安装打包工具 pyinstaller...
pip install pyinstaller
echo 开始打包...
pyinstaller -F -w ^
  --name "零碳储能仿真模型" ^
  run_simulation.py
echo.
echo 打包完成! 生成的exe在 dist 文件夹:
echo   dist\零碳储能仿真模型.exe
echo 请把 ems_sim 文件夹 和 out 文件夹 复制到exe旁边, 双击即可运行。
pause
exit /b
