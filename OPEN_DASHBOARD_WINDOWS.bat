@echo off
set /p URL=Paste your Render dashboard URL (https://...onrender.com): 
if "%URL%"=="" exit /b 1
start "Borhan_SK Dashboard" "%URL%"
