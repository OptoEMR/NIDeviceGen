@echo off
:: Regenerate Python gRPC stubs from the proto file.
:: Run this once after cloning or whenever instrument_test.proto changes.

setlocal
set PROTO_DIR=%~dp0proto
set OUT_DIR=%~dp0generated

if not exist "%OUT_DIR%" mkdir "%OUT_DIR%"
if not exist "%OUT_DIR%\__init__.py" type nul > "%OUT_DIR%\__init__.py"

echo Generating gRPC stubs ...
python -m grpc_tools.protoc ^
    -I "%PROTO_DIR%" ^
    --python_out="%OUT_DIR%" ^
    --grpc_python_out="%OUT_DIR%" ^
    "%PROTO_DIR%\instrument_test.proto"

if %ERRORLEVEL% == 0 (
    echo Done.  Stubs written to: %OUT_DIR%
) else (
    echo ERROR: grpc_tools.protoc failed.  Make sure grpcio-tools is installed:
    echo        pip install grpcio-tools
)
endlocal
