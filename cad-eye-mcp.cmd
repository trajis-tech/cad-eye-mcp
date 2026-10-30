@echo off
rem Launches the cad-eye MCP server (stdio) with the private runtime from install.ps1.
"%~dp0runtime\python\python.exe" -I -m cad_eye_mcp %*
