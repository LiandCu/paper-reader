@echo off
cd /d "%~dp0"
"%~dp0runtime\python.exe" -B -c "import core,tasks; core.initialize(); tasks.save_settings({'auto':False}); tasks.stop('fetch'); print('Automatic collection stopped. Your papers are preserved.')"
pause
