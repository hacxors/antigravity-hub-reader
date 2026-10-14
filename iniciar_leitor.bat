@echo off
chcp 65001 >nul
title Antigravity Hub - Leitor de Conversas & Continuidade
cls

echo ======================================================================
echo    ✦ ANTIGRAVITY HUB - LEITOR DE CONVERSAS E CONTINUIDADE ✦
echo ======================================================================
echo.
echo Iniciando servidor local na porta 8765...
echo O seu navegador sera aberto automaticamente em instantes.
echo.

where py >nul 2>&1
if %errorlevel% equ 0 (
    py "%~dp0antigravity_reader.py"
    goto end
)

where python >nul 2>&1
if %errorlevel% equ 0 (
    python "%~dp0antigravity_reader.py"
    goto end
)

echo [ERRO] Python ou Launcher (py) nao foi localizado no PATH do sistema.
echo Por favor, verifique a instalacao do Python.
pause

:end
